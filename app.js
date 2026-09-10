new ResizeObserver((entries) =>
  document.documentElement.style.setProperty(
    "--toolbar-height",
    entries[0].target.offsetHeight + "px",
  ),
).observe(document.querySelector(".toolbar"));
const $ = (id) => document.getElementById(id);
let state,
  drafts = {},
  key,
  busy = false,
  stale = false,
  polling = false;
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
function status(text, error = false) {
  $("status").textContent = text;
  $("status").className = error ? "error" : "";
}
async function api(url, options) {
  const r = await fetch(url, options);
  const body = await r.json();
  if (!r.ok) throw Error(body.error || "Request failed");
  return body;
}
function save() {
  try {
    localStorage.setItem(
      key,
      JSON.stringify({ tip: state.tip, edits: drafts }),
    );
  } catch (e) {
    status(
      "Draft storage is unavailable. Keep this page open to retain edits.",
      true,
    );
  }
}
function counts() {
  const n = Object.keys(drafts).length;
  const invalid = Object.values(drafts).some(
    (m) => !m.trim() || m.includes("\0"),
  );
  $("count").textContent = `${n} pending edit${n === 1 ? "" : "s"}`;
  $("review").textContent = `Rewrite ${n} commit message${n === 1 ? "" : "s"}`;
  $("review").disabled = !n || invalid || busy || stale;
  $("discard").disabled = busy;
}
function validation(area, note) {
  if (!area.value.trim() || area.value.includes("\0")) {
    note.textContent = "Message cannot be empty or contain NUL characters.";
    note.className = "validation error";
    area.setAttribute("aria-invalid", "true");
  } else {
    const length = Array.from(area.value.split("\n")[0]).length;
    note.textContent =
      length > 72
        ? `Long subject: ${length} characters (recommended maximum: 72). You can still apply this edit.`
        : "";
    note.className = "validation warning";
    area.removeAttribute("aria-invalid");
  }
}
function navigation() {
  const nav = $("navigation");
  nav.replaceChildren();
  let visible = 0;
  for (const c of state.commits) {
    const card = document.getElementById("commit-" + c.oid);
    if (!card || card.hidden) continue;
    visible++;
    const link = el(
      "a",
      (drafts[c.oid] ?? c.message).split("\n")[0] || "(empty subject)",
      "nav-link",
    );
    link.href = "#commit-" + c.oid;
    link.classList.toggle("pending", c.oid in drafts);
    link.append(
      el("small", c.oid.slice(0, 12) + (c.oid in drafts ? " · edited" : "")),
    );
    nav.append(link);
  }
  $("noMatches").classList.toggle("hidden", visible > 0);
}
function filter() {
  const q = $("search").value.toLowerCase();
  for (const card of $("commits").children) {
    if (!card.dataset.oid) continue;
    const c = state.commits.find((c) => c.oid === card.dataset.oid);
    card.hidden =
      ($("only").checked && !(c.oid in drafts)) ||
      !`${c.oid} ${c.author} ${drafts[c.oid] ?? c.message}`
        .toLowerCase()
        .includes(q);
  }
  navigation();
}
function resizeTextarea(area) {
  area.style.height = "auto";
  area.style.height = area.scrollHeight + "px";
}
function render() {
  $("commits").replaceChildren();
  for (const c of state.commits) {
    const card = el("article");
    card.dataset.oid = c.oid;
    card.id = "commit-" + c.oid;
    card.classList.toggle("edited", c.oid in drafts);
    card.append(el("div", `${c.oid.slice(0, 12)} · ${c.author}`, "meta"));
    const area = el("textarea");
    area.value = drafts[c.oid] ?? c.message;
    area.disabled = busy || stale;
    area.setAttribute("aria-label", `Message for commit ${c.oid.slice(0, 12)}`);
    area.spellcheck = false;
    const note = el("div", undefined, "validation");
    note.id = `v-${c.oid}`;
    area.setAttribute("aria-describedby", note.id);
    validation(area, note);
    resizeTextarea(area);
    area.oninput = () => {
      resizeTextarea(area);
      if (area.value === c.message) delete drafts[c.oid];
      else drafts[c.oid] = area.value;
      card.classList.toggle("edited", c.oid in drafts);
      validation(area, note);
      save();
      counts();
      filter();
    };
    const diff = el("details");
    diff.append(el("summary", "View full formatted diff"), el("pre", c.diff));
    card.append(area, note, diff);
    $("commits").append(card);
  }
  counts();
  filter();
}
async function load() {
  try {
    state = await api("/api/state");
    key = "commit-rewriter:" + state.identity;
    drafts = {};
    stale = false;
    $("repo").textContent =
      state.repository + " · main @ " + state.tip.slice(0, 12);
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "null");
      const completed = await api("/api/progress");
      if (
        saved &&
        completed.status === "complete" &&
        saved.tip === completed.original_tip &&
        state.tip === completed.tip
      ) {
        saved.edits = Object.fromEntries(
          Object.entries(saved.edits || {})
            .filter(([oid, m]) => completed.edits[oid] !== m)
            .map(([oid, m]) => [completed.mapping[oid] || oid, m]),
        );
        saved.tip = state.tip;
        localStorage.setItem(key, JSON.stringify(saved));
      }
      if (saved && saved.edits && typeof saved.edits === "object") {
        drafts = Object.fromEntries(
          Object.entries(saved.edits).filter(
            ([oid, m]) => typeof m === "string",
          ),
        );
        if (saved.tip !== state.tip && Object.keys(drafts).length) {
          stale = true;
          status(
            "main has changed since these drafts were saved. Copy any draft text you need, then discard the stale drafts to continue.",
            true,
          );
        } else {
          const ids = new Set(state.commits.map((c) => c.oid));
          drafts = Object.fromEntries(
            Object.entries(drafts).filter(([oid]) => ids.has(oid)),
          );
        }
      }
    } catch (e) {
      status(
        "Saved drafts could not be loaded. Browser storage may be unavailable.",
        true,
      );
    }
    render();
    if (stale) {
      const details = el("details");
      details.open = true;
      details.append(el("summary", "Saved drafts from previous history"));
      for (const [oid, m] of Object.entries(drafts))
        details.append(el("pre", oid + "\n" + m));
      $("commits").prepend(details);
    }
  } catch (e) {
    status(e.message, true);
  }
}
$("search").oninput = filter;
$("only").onchange = filter;
$("discard").onclick = () => {
  if (confirm("Discard all saved message drafts?")) {
    drafts = {};
    stale = false;
    save();
    status("Drafts discarded.");
    render();
  }
};
$("review").onclick = () => {
  $("changes").replaceChildren();
  for (const c of state.commits.filter((c) => c.oid in drafts)) {
    const section = el("section");
    section.append(el("h3", c.oid.slice(0, 12)));
    const grid = el("div", undefined, "comparison");
    for (const [label, message] of [
      ["Original", c.message],
      ["Proposed", drafts[c.oid]],
    ]) {
      const col = el("div");
      col.append(el("strong", label), el("pre", message));
      grid.append(col);
    }
    section.append(grid);
    $("changes").append(section);
  }
  $("dialog").showModal();
};
$("cancel").onclick = () => $("dialog").close();
$("reload").onclick = () => location.reload();
async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      const job = await api("/api/progress");
      if (job.status === "idle") break;
      busy = job.status === "running";
      $("progressBox").classList.remove("hidden");
      $("bar").max = job.total || 1;
      $("bar").value = job.done;
      $("progressLabel").textContent = job.total
        ? `${job.done} / ${job.total} affected commits rebuilt`
        : "Validating edits and preparing backup…";
      counts();
      if (job.status === "complete") {
        drafts = {};
        await load();
        status(
          `Rewrite complete. Backup branch: ${job.backup}\n${job.signatures_removed} signature headers removed.`,
        );
        const details = el("details");
        details.append(
          el("summary", "Old → new commit hashes"),
          el(
            "pre",
            Object.entries(job.mapping)
              .map(([a, b]) => `${a} → ${b}`)
              .join("\n"),
          ),
        );
        $("status").append(details);
        break;
      }
      if (job.status === "failed") {
        status(
          `Rewrite failed: ${job.error}` +
            (job.backup ? `\nBackup branch: ${job.backup}` : ""),
          true,
        );
        $("reload").classList.remove("hidden");
        render();
        break;
      }
      await new Promise((r) => setTimeout(r, 250));
    }
  } catch (e) {
    status(
      "Connection lost. Reload to check the rewrite status. " + e.message,
      true,
    );
    $("reload").classList.remove("hidden");
  } finally {
    polling = false;
  }
}
$("apply").onclick = async () => {
  $("dialog").close();
  busy = true;
  render();
  status("");
  try {
    await api("/api/rewrite", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": TOKEN },
      body: JSON.stringify({ tip: state.tip, edits: drafts }),
    });
    await poll();
  } catch (e) {
    busy = false;
    render();
    status(e.message, true);
  }
};
(async () => {
  await load();
  if (state) {
    const job = await api("/api/progress");
    if (job.status === "running") {
      busy = true;
      render();
      await poll();
    } else if (job.status === "complete" && stale) {
      await poll();
    }
  }
})().catch((e) => status(e.message, true));
