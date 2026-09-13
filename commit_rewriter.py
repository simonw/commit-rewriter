"""Edit Git commit messages locally. Usage: commit-rewriter [repository-folder]."""
import argparse
import asyncio
import hashlib
import secrets
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route


class GitError(Exception):
    pass


class Repository:
    ref = "refs/heads/main"

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.git("rev-parse", "--git-dir")
        self.tip()
        self.identity = hashlib.sha256(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")).hexdigest()

    def git(self, *args, data=None):
        result = subprocess.run(["git", "--no-replace-objects", "-C", str(self.path), *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise GitError(result.stderr.decode(errors="replace").strip() or "Git command failed")
        return result.stdout

    def tip(self):
        return self.git("rev-parse", "--verify", self.ref).decode().strip()

    def raw(self, oid):
        return self.git("cat-file", "commit", oid)

    def diff(self, oid):
        """Return Git's complete, colorless patch presentation for a commit."""
        return self.git("show", "--format=fuller", "--patch", "--binary", "--no-ext-diff", "--no-color", oid).decode(errors="replace")

    def commits(self, include_diffs=True):
        commits = []
        for oid in self.git("log", "-100", "--format=%H", self.ref).decode().splitlines():
            header, message = self.raw(oid).split(b"\n\n", 1)
            fields = header.splitlines()
            author = next(line[7:] for line in fields if line.startswith(b"author "))
            committer = next(line[10:] for line in fields if line.startswith(b"committer "))
            encoding = next((line[9:].decode("ascii") for line in fields if line.startswith(b"encoding ")), "utf-8")
            try:
                decoded = message.decode(encoding)
            except (LookupError, UnicodeError) as exc:
                raise GitError(f"Cannot decode message for {oid[:12]} using {encoding}") from exc
            commit = {"oid": oid, "message": decoded, "author": author.decode(errors="replace"), "committer": committer.decode(errors="replace")}
            if include_diffs:
                commit["diff"] = self.diff(oid)
            commits.append(commit)
        return commits

    def check(self, expected):
        if self.tip() != expected:
            raise GitError("main changed. Reload and review your drafts before rewriting.")
        if self.git("rev-parse", "--is-shallow-repository").strip() == b"true":
            raise GitError("A complete clone is required; this repository is shallow.")
        if self.git("status", "--porcelain").strip():
            raise GitError("The working tree must be clean, including untracked files.")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG"):
            path = self.git("rev-parse", "--git-path", marker).decode().strip()
            if (self.path / path).exists():
                raise GitError("Finish the active Git operation before rewriting.")
        # Other worktrees could have their own in-progress operations or changes.
        records = self.git("worktree", "list", "--porcelain").decode().split("\n\n")
        current = self.git("rev-parse", "--show-toplevel").decode().strip()
        for record in records:
            lines = record.splitlines()
            if f"branch {self.ref}" in lines and lines and Path(lines[0][9:]).resolve() != Path(current).resolve():
                raise GitError("main is checked out in another worktree. Run this server there.")

    def prepare(self, expected, edits):
        self.check(expected)
        if not isinstance(edits, dict) or not edits:
            raise GitError("Provide at least one message edit.")
        allowed = {c["oid"]: c for c in self.commits(include_diffs=False)}
        result = {}
        for oid, message in edits.items():
            if oid not in allowed or not isinstance(message, str):
                raise GitError("Edits must refer to the latest 100 commits on main.")
            if not message.strip() or "\x00" in message:
                raise GitError("Commit messages cannot be empty or contain NUL characters.")
            if message != allowed[oid]["message"]:
                header = self.raw(oid).split(b"\n\n", 1)[0]
                encoding = next((line[9:].decode("ascii") for line in header.splitlines() if line.startswith(b"encoding ")), "utf-8")
                try:
                    result[oid] = message.encode(encoding)
                except (LookupError, UnicodeError) as exc:
                    raise GitError(f"Message cannot be encoded as {encoding}.") from exc
        if not result:
            raise GitError("No changed messages to rewrite.")
        return result

    def rewrite(self, expected, edits, report):
        self.check(expected)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-%f")
        backup = f"commit-message-backup/{stamp}-{secrets.token_hex(3)}"
        self.git("update-ref", f"refs/heads/{backup}", expected, "0" * len(expected))
        report(backup=backup)
        mapping = {}
        rows = self.git("rev-list", "--topo-order", "--reverse", "--parents", expected).decode().splitlines()
        affected = set()
        work = []
        for row in rows:
            oid, *parents = row.split()
            if oid in edits or any(parent in affected for parent in parents):
                affected.add(oid)
                work.append(oid)
        report(total=len(work))
        signatures_removed = 0
        for index, oid in enumerate(work):
            header, message = self.raw(oid).split(b"\n\n", 1)
            output = []
            skip = False
            for line in header.split(b"\n"):
                if line.startswith(b" "):
                    if not skip:
                        output.append(line)
                    continue
                # Signatures authenticate the old object/parents and cannot be retained.
                skip = line.startswith((b"gpgsig ", b"gpgsig-sha256 ", b"mergetag "))
                if skip:
                    signatures_removed += 1
                elif line.startswith(b"parent "):
                    parent = line[7:].decode()
                    output.append(b"parent " + mapping.get(parent, parent).encode())
                else:
                    output.append(line)
            raw = b"\n".join(output) + b"\n\n" + edits.get(oid, message)
            mapping[oid] = self.git("hash-object", "-t", "commit", "-w", "--stdin", data=raw).decode().strip()
            report(done=index + 1)
        # Compare-and-swap protects against concurrent branch updates; the tree is unchanged.
        self.check(expected)
        new_tip = mapping[expected]
        self.git("update-ref", "-m", f"commit-rewriter: backup {backup}", self.ref, new_tip, expected)
        report(status="complete", tip=new_tip, mapping=mapping, signatures_removed=signatures_removed)


def create_app(path):
    repo = Repository(path)
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    job = {"status": "idle", "done": 0, "total": 0}
    tasks = set()

    def report(**fields):
        with lock:
            job.update(fields)

    async def home(request):
        return HTMLResponse(HTML.replace("__TOKEN__", token), headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"})

    async def javascript(request):
        return PlainTextResponse(Path(__file__).with_name("app.js").read_text().replace("__TOKEN__", token), media_type="application/javascript", headers={"Cache-Control": "no-store"})

    async def state(request):
        try:
            tip = await asyncio.to_thread(repo.tip)
            commits = await asyncio.to_thread(repo.commits)
            if await asyncio.to_thread(repo.tip) != tip:
                raise GitError("main changed during loading. Reload the page.")
            return JSONResponse({"repository": str(repo.path), "identity": repo.identity, "tip": tip, "commits": commits}, headers={"Cache-Control": "no-store"})
        except GitError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def progress(request):
        with lock:
            snapshot = dict(job)
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})

    async def execute(expected, edits):
        try:
            prepared = await asyncio.to_thread(repo.prepare, expected, edits)
            await asyncio.to_thread(repo.rewrite, expected, prepared, report)
        except Exception as exc:
            report(status="failed", error=str(exc))

    async def rewrite(request: Request):
        if request.headers.get("x-csrf-token") != token:
            return JSONResponse({"error": "Invalid request token. Reload this page."}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("tip"), str) or not isinstance(body.get("edits"), dict):
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({"error": "Expected a tip and an edits object."}, status_code=400)
        with lock:
            if job["status"] == "running":
                return JSONResponse({"error": "A rewrite is already running."}, status_code=409)
            job.clear()
            job.update(status="running", done=0, total=0, original_tip=body["tip"], edits=body["edits"])
        task = asyncio.create_task(execute(body["tip"], body["edits"]))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return JSONResponse({"status": "running"}, status_code=202)

    app = Starlette(routes=[Route("/", home), Route("/app.js", javascript), Route("/api/state", state), Route("/api/progress", progress), Route("/api/rewrite", rewrite, methods=["POST"])])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    return app


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>commit-rewriter</title>
<style>
:root{font-family:system-ui,sans-serif;color:#202b3b;background:#f5f7fa;color-scheme:light}*{box-sizing:border-box}body{margin:0}main{max-width:1360px;margin:auto;padding:40px 24px}h1{font-size:36px;letter-spacing:-1.5px;margin:8px 0}p{color:#5d6a7e;line-height:1.6}.eyebrow{color:#227254;font-size:12px;letter-spacing:2px;font-weight:700}.toolbar{position:sticky;top:0;background:#f5f7fa;padding:18px 0;z-index:2;border-bottom:1px solid #d8dfe8}.row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}button,input,textarea{font:inherit;border-radius:7px;border:1px solid #c1cbd8;padding:11px;background:#ffffff;color:inherit}button{cursor:pointer}button.primary{background:#16734e;color:#ffffff;font-weight:750;border:0;padding:15px 24px}button:disabled{opacity:.45;cursor:not-allowed}input[type=search]{flex:1;min-width:180px}label{font-size:14px}.count{font-weight:700;margin-right:auto}article{background:#ffffff;border:1px solid #d8dfe8;border-radius:10px;padding:20px;margin:18px 0}article.edited{border-color:#16734e}.meta{font:12px ui-monospace,monospace;color:#5d6a7e;margin-bottom:12px;overflow-wrap:anywhere}textarea{width:100%;resize:vertical;min-height:105px;line-height:1.5;background:#ffffff}.warning{color:#8b5700}.error{color:#b42318}.validation{min-height:23px;font-size:13px;margin-top:7px}.hidden{display:none!important}progress{width:100%;height:18px;accent-color:#16734e}#status{white-space:pre-wrap;overflow-wrap:anywhere}dialog{max-width:900px;width:95%;max-height:85vh;background:#ffffff;border:1px solid #c1cbd8;border-radius:12px;color:inherit}dialog::backdrop{background:#000a}.comparison{display:grid;grid-template-columns:1fr 1fr;gap:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f6;padding:12px;font-size:13px}details{margin:16px 0}footer{font-size:12px;color:#637086;margin-top:30px}@media(max-width:600px){.comparison{grid-template-columns:1fr}main{padding:24px 14px}h1{font-size:30px}}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:24px;align-items:start}.sidebar{position:sticky;top:calc(var(--toolbar-height, 140px) + 12px);border:1px solid #d8dfe8;border-radius:10px;background:white;margin-top:18px;padding:14px}.sidebar summary{font-weight:700;cursor:pointer}.sidebar nav{max-height:calc(100vh - var(--toolbar-height, 140px) - 110px);overflow:auto;margin-top:12px}.nav-link{display:block;padding:10px 8px;border-radius:6px;color:#334155;text-decoration:none;font-size:13px;border-left:3px solid transparent;line-height:1.4}.nav-link:hover,.nav-link:focus{background:#eef5f1}.nav-link.pending{border-left-color:#16734e}.nav-link small{display:block;color:#64748b;font:11px ui-monospace,monospace;margin-top:4px}article{scroll-margin-top:calc(var(--toolbar-height, 140px) + 18px)}article:target{outline:2px solid #16734e;outline-offset:3px}.empty{color:#64748b;font-size:13px}@media(max-width:760px){.layout{grid-template-columns:1fr;gap:0}.sidebar{position:static}.sidebar nav{max-height:180px}article{scroll-margin-top:calc(var(--toolbar-height, 200px) + 18px)}}
</style></head><body><main><div class="eyebrow">LOCAL GIT HISTORY EDITOR</div><h1>commit-rewriter</h1><p id="repo">Loading repository…</p><p>Edit the latest 100 commits on <strong>main</strong>. Author and committer identities and dates stay intact. A timestamped backup branch is created before every rewrite.</p>
<div class="toolbar"><div class="row"><span class="count" id="count" aria-live="polite">0 pending edits</span><button id="discard">Discard drafts</button><button class="primary" id="review" disabled>Rewrite 0 commit messages</button></div><div class="row" style="margin-top:14px"><input id="search" type="search" aria-label="Search commits" placeholder="Search message, author, or hash"><label><input id="only" type="checkbox"> Edited only</label></div><div id="progressBox" class="hidden"><p id="progressLabel" aria-live="polite"></p><progress id="bar" max="1" value="0" aria-label="Rewrite progress"></progress></div></div>
<p id="status" role="status"></p><button id="reload" class="hidden">Reload current history</button><div class="layout"><aside class="sidebar"><details open style="margin:0"><summary>Navigate commits</summary><nav id="navigation" aria-label="Commit navigation"></nav><p id="noMatches" class="empty hidden">No matching commits</p></details></aside><section id="commits" aria-label="Commit editors"></section></div><footer>Drafts are stored in this browser’s localStorage. Rewrites change descendant hashes and remove invalidated commit signatures. No changes are pushed.</footer>
<dialog id="dialog"><h2>Review message changes</h2><p>main will be updated after a backup branch is created. File contents, author information, and both dates are preserved.</p><div id="changes"></div><div class="row"><button id="cancel">Keep editing</button><button id="apply" class="primary">Back up & rewrite</button></div></dialog>
<script>const TOKEN='__TOKEN__';</script><script src="/app.js"></script>
</script></main></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".", help="Repository directory (default: current directory)")
    parser.add_argument("-p", "--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        app = create_app(args.path)
    except GitError as exc:
        parser.error(str(exc))
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
