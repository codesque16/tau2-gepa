/**
 * GEPA run outputs browser.
 * Local (monorepo): http://localhost:8000/gepa/examples/run_output_viewer/index.html
 * Local (standalone fork): http://localhost:8000/examples/run_output_viewer/index.html
 * GitHub Pages: https://<user>.github.io/<repo>/
 * Manifest: uv run python examples/run_output_viewer/gen_outputs_manifest.py (from fork root)
 */

const marked = globalThis.marked;
const mermaid = globalThis.mermaid;

function repoRootPrefix() {
  let p = window.location.pathname;
  p = p.replace(/\/index\.html?$/i, "");
  p = p.replace(/\/+$/, "");
  const monorepoViewer = "/gepa/examples/run_output_viewer";
  if (p.endsWith(monorepoViewer)) {
    const root = p.slice(0, -monorepoViewer.length);
    return root === "" ? "" : root;
  }
  // Viewer is the whole site (GitHub Pages project page: /repo/ or /repo/index.html)
  const segs = p.split("/").filter(Boolean);
  if (segs.length >= 1) return `/${segs[0]}`;
  return "";
}

function outputsBase() {
  const root = repoRootPrefix();
  return `${root}/outputs`;
}

/** @param {string} basePath e.g. viz_outputs (legacy manifests may use outputs) */
/** @param {string} runName directory name */
function runDataUrl(basePath, runName, file) {
  const root = repoRootPrefix();
  const base = basePath.replace(/^\/+|\/+$/g, "");
  const q = encodeURIComponent(runName);
  return `${root}/${base}/${q}/${file}`;
}

function parseManifest(data) {
  const entries = [];
  if (data.entries && Array.isArray(data.entries)) {
    for (const e of data.entries) {
      if (e && typeof e.basePath === "string" && typeof e.name === "string") {
        entries.push({ basePath: e.basePath, name: e.name });
      }
    }
    return entries;
  }
  if (data.runs && Array.isArray(data.runs)) {
    for (const name of data.runs) {
      if (typeof name === "string") entries.push({ basePath: "outputs", name });
    }
  }
  return entries;
}

/** Tab separates basePath from run name (names rarely contain \\t). */
function runSelectValue(entry) {
  return `${entry.basePath}\t${entry.name}`;
}

function parseRunSelectValue(val) {
  if (!val) return null;
  const i = val.indexOf("\t");
  if (i < 0) return { basePath: "outputs", name: val };
  return { basePath: val.slice(0, i), name: val.slice(i + 1) };
}

let policyMarkdown = "";
let candidatesData = [];
let mermaidReady = false;

/** @type {'plain' | 'diff'} */
let compareViewMode = "plain";
/** @type {{ createTwoFilesPatch: Function, diff2html: Function } | null} */
let diffLibs = null;

function setTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("active", p.id === `panel-${name}`);
  });
}

function candidateTextAt(index) {
  const row = candidatesData[index];
  if (!row || typeof row !== "object") return "";
  if (typeof row.current_candidate === "string") return row.current_candidate;
  const vals = Object.values(row).filter((v) => typeof v === "string");
  return vals[0] || "";
}

async function ensureDiffLibs() {
  if (diffLibs) return diffLibs;
  const load = async (urls) => {
    let lastErr;
    for (const [dUrl, hUrl] of urls) {
      try {
        const [diffMod, d2hMod] = await Promise.all([import(dUrl), import(hUrl)]);
        return {
          createTwoFilesPatch: diffMod.createTwoFilesPatch,
          diff2html: d2hMod.html,
        };
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr ?? new Error("diff import failed");
  };
  diffLibs = await load([
    [
      "https://cdn.jsdelivr.net/npm/diff@5.2.0/+esm",
      "https://cdn.jsdelivr.net/npm/diff2html@3.4.52/+esm",
    ],
    ["https://esm.sh/diff@5.2.0", "https://esm.sh/diff2html@3.4.52"],
  ]);
  return diffLibs;
}

function setCompareViewMode(mode) {
  compareViewMode = mode;
  const plainOn = mode === "plain";
  document.getElementById("btn-compare-plain").classList.toggle("on", plainOn);
  document.getElementById("btn-compare-diff").classList.toggle("on", !plainOn);
  document.getElementById("compare-plain-wrap").hidden = !plainOn;
  document.getElementById("compare-diff-wrap").hidden = plainOn;
  document.getElementById("compare-diff-hint").hidden = plainOn;
  if (!plainOn) {
    renderHighlightedDiff().catch((e) => {
      console.error(e);
      const out = document.getElementById("compare-diff-out");
      out.innerHTML = `<p class="err" style="padding:16px">Could not build diff (${e.message}). Try Plain view or check the network (diff libraries load from CDN).</p>`;
    });
  }
}

function fillCompareSelects() {
  const left = document.getElementById("left-idx");
  const right = document.getElementById("right-idx");
  const n = candidatesData.length;
  left.innerHTML = "";
  right.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const label = `idx ${i}`;
    const oL = document.createElement("option");
    oL.value = String(i);
    oL.textContent = label;
    left.appendChild(oL);
    const oR = document.createElement("option");
    oR.value = String(i);
    oR.textContent = label;
    right.appendChild(oR);
  }
  if (n > 1) {
    left.value = "0";
    right.value = "1";
  } else if (n === 1) {
    left.value = "0";
    right.value = "0";
  }
  renderCompareTexts();
}

function renderCompareTexts() {
  const li = parseInt(document.getElementById("left-idx").value, 10);
  const ri = parseInt(document.getElementById("right-idx").value, 10);
  document.getElementById("left-text").textContent = candidateTextAt(li);
  document.getElementById("right-text").textContent = candidateTextAt(ri);
  if (compareViewMode === "diff") {
    renderHighlightedDiff().catch((e) => console.warn("diff refresh:", e));
  }
}

async function renderHighlightedDiff() {
  const li = parseInt(document.getElementById("left-idx").value, 10);
  const ri = parseInt(document.getElementById("right-idx").value, 10);
  const left = candidateTextAt(li);
  const right = candidateTextAt(ri);
  const out = document.getElementById("compare-diff-out");
  out.innerHTML = '<p class="muted" style="padding:16px;margin:0">Loading diff…</p>';

  const { createTwoFilesPatch, diff2html } = await ensureDiffLibs();
  const patch = createTwoFilesPatch(
    `candidate_${li}.md`,
    `candidate_${ri}.md`,
    left,
    right,
    "",
    "",
    { context: 1_000_000 }
  );
  out.innerHTML = diff2html(patch, {
    outputFormat: "side-by-side",
    drawFileList: false,
    matching: "words",
    diffStyle: "word",
  });
}

async function initMermaid() {
  if (mermaidReady) return;
  mermaid.initialize({ startOnLoad: false, theme: "neutral", securityLevel: "strict" });
  mermaidReady = true;
}

async function renderMarkdownPreview(md, el) {
  await initMermaid();
  const html = marked.parse(md || "", { breaks: true, gfm: true });
  el.innerHTML = html;
  el.querySelectorAll("pre code.language-mermaid").forEach((code) => {
    const pre = code.parentElement;
    const graph = code.textContent;
    pre.replaceChildren();
    pre.className = "mermaid";
    pre.textContent = graph;
  });
  try {
    await mermaid.run({ querySelector: "#policy-preview pre.mermaid" });
  } catch (e) {
    console.warn("mermaid run:", e);
  }
}

function setPolicyView(mode) {
  const preview = document.getElementById("policy-preview");
  const raw = document.getElementById("policy-raw");
  const bp = document.getElementById("btn-preview");
  const br = document.getElementById("btn-raw");
  const isRaw = mode === "raw";
  preview.hidden = isRaw;
  raw.hidden = !isRaw;
  bp.classList.toggle("on", !isRaw);
  br.classList.toggle("on", isRaw);
  raw.value = policyMarkdown;
  if (!isRaw) renderMarkdownPreview(policyMarkdown, preview);
}

async function loadRun(selectVal) {
  const errEl = document.getElementById("policy-error");
  errEl.hidden = true;
  policyMarkdown = "";
  candidatesData = [];
  document.getElementById("policy-preview").innerHTML = "";
  document.getElementById("policy-raw").value = "";
  document.getElementById("compare-diff-out").innerHTML = "";
  setCompareViewMode("plain");

  const loc = parseRunSelectValue(selectVal);
  if (!loc) return;

  const policyRes = await fetch(runDataUrl(loc.basePath, loc.name, "best_policy.md"));
  if (policyRes.ok) {
    policyMarkdown = await policyRes.text();
    setPolicyView(document.getElementById("btn-raw").classList.contains("on") ? "raw" : "preview");
  } else {
    errEl.textContent = "Could not load best_policy.md for this run.";
    errEl.hidden = false;
  }

  const candRes = await fetch(runDataUrl(loc.basePath, loc.name, "candidates.json"));
  if (candRes.ok) {
    try {
      candidatesData = await candRes.json();
      if (!Array.isArray(candidatesData)) candidatesData = [];
    } catch {
      candidatesData = [];
    }
  }
  fillCompareSelects();

  const treeUrlFull = runDataUrl(loc.basePath, loc.name, "candidate_tree.html");
  const head = await fetch(treeUrlFull, { method: "HEAD" }).catch(() => null);
  const hasTree = head && head.ok;
  document.getElementById("tree-wrap").hidden = !hasTree;
  document.getElementById("tree-missing").hidden = hasTree;
  document.getElementById("compare-tree-wrap").hidden = !hasTree;
  document.getElementById("compare-tree-missing").hidden = hasTree;
  if (hasTree) {
    document.getElementById("tree-frame").src = treeUrlFull;
    document.getElementById("compare-tree-frame").src = treeUrlFull;
  } else {
    document.getElementById("tree-frame").removeAttribute("src");
    document.getElementById("compare-tree-frame").removeAttribute("src");
  }
}

async function loadManifest() {
  const hint = document.getElementById("run-hint");
  const sel = document.getElementById("run-select");
  const root = repoRootPrefix();
  const candidates = [
    `${root}/viz_outputs/manifest.json`,
    `${root}/gepa/viz_outputs/manifest.json`,
    `${root}/outputs/manifest.json`,
  ];
  let data = null;
  let loadedFrom = "";
  for (const url of candidates) {
    const res = await fetch(url).catch(() => null);
    if (res && res.ok) {
      try {
        data = await res.json();
        loadedFrom = url;
        break;
      } catch {
        /* try next */
      }
    }
  }
  if (!data) {
    hint.innerHTML =
      `No manifest. Run <code>uv run python examples/run_output_viewer/gen_outputs_manifest.py</code> — indexes <code>viz_outputs/</code> only (or <code>gepa/viz_outputs/</code> when that holds the runs).`;
    return;
  }
  const manifestEntries = parseManifest(data);
  const byName = {};
  for (const e of manifestEntries) {
    byName[e.name] = (byName[e.name] || 0) + 1;
  }
  sel.innerHTML = '<option value="">— Select run —</option>';
  manifestEntries.forEach((e) => {
    const o = document.createElement("option");
    o.value = runSelectValue(e);
    o.textContent = byName[e.name] > 1 ? `${e.name} (${e.basePath})` : e.name;
    sel.appendChild(o);
  });
  const src = loadedFrom.replace(/^.*\//, "");
  hint.textContent = `${manifestEntries.length} run(s) · manifest ${src}`;
}

document.querySelectorAll(".tabs button").forEach((b) => {
  b.addEventListener("click", () => setTab(b.dataset.tab));
});

document.getElementById("btn-preview").addEventListener("click", () => setPolicyView("preview"));
document.getElementById("btn-raw").addEventListener("click", () => setPolicyView("raw"));
document.getElementById("btn-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(policyMarkdown);
    const btn = document.getElementById("btn-copy");
    const t = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => {
      btn.textContent = t;
    }, 1500);
  } catch (e) {
    alert("Copy failed: " + e.message);
  }
});

document.getElementById("run-select").addEventListener("change", (e) => {
  loadRun(e.target.value);
});

document.getElementById("left-idx").addEventListener("change", renderCompareTexts);
document.getElementById("right-idx").addEventListener("change", renderCompareTexts);

document.getElementById("btn-compare-plain").addEventListener("click", () => setCompareViewMode("plain"));
document.getElementById("btn-compare-diff").addEventListener("click", () => setCompareViewMode("diff"));

window.addEventListener("message", (ev) => {
  const d = ev.data;
  if (!d || d.source !== "gepa-candidate-tree" || d.type !== "diff-pick") return;
  const idx = d.idx;
  if (typeof idx !== "number" || idx < 0) return;
  setTab("compare");
  const sel = d.side === "right" ? document.getElementById("right-idx") : document.getElementById("left-idx");
  if (!candidatesData.length) return;
  if (idx >= candidatesData.length) return;
  sel.value = String(idx);
  renderCompareTexts();
});

loadManifest();
