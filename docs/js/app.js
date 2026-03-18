/**
 * GEPA docs viewer — same deployment model as tau2-bench/docs (static /docs folder).
 */
function parseJsonl(text) {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  return lines.map((l) => JSON.parse(l));
}

function basePrefix() {
  const base = document.querySelector("base");
  if (base && base.href) {
    try {
      const u = new URL(base.href);
      return u.pathname.replace(/\/?$/, "/");
    } catch {
      /* ignore */
    }
  }
  const path = window.location.pathname.replace(/\/index\.html?$/, "/");
  if (path.endsWith("/")) return path;
  const i = path.lastIndexOf("/");
  return i >= 0 ? path.slice(0, i + 1) : "/";
}

async function fetchText(path) {
  const prefix = basePrefix();
  const url = prefix + path.replace(/^\//, "");
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.text();
}

function setStatus(el, msg, isError) {
  el.textContent = msg || "";
  el.className = "status" + (isError ? " error" : "");
}

async function loadRun(runId) {
  const status = document.getElementById("status");
  const prefix = `visualizer_dump/${runId}/`;

  try {
    setStatus(status, "Loading…");
    const [acceptedT, rejectedT, paretoT, valsetT] = await Promise.all([
      fetchText(`${prefix}candidate_accepted.jsonl`).catch(() => ""),
      fetchText(`${prefix}candidate_rejected.jsonl`).catch(() => ""),
      fetchText(`${prefix}pareto_front_updated.jsonl`).catch(() => ""),
      fetchText(`${prefix}valset_evaluated.jsonl`).catch(() => ""),
    ]);

    const accepted = acceptedT ? parseJsonl(acceptedT) : [];
    const rejected = rejectedT ? parseJsonl(rejectedT) : [];
    const pareto = paretoT ? parseJsonl(paretoT) : [];
    const valset = valsetT ? parseJsonl(valsetT) : [];

    document.getElementById("accepted-count").textContent = String(accepted.length);
    document.getElementById("rejected-count").textContent = String(rejected.length);
    document.getElementById("pareto-count").textContent = String(pareto.length);
    document.getElementById("valset-count").textContent = String(valset.length);

    const preview = document.getElementById("accepted-preview");
    if (accepted.length) {
      preview.textContent = JSON.stringify(accepted[accepted.length - 1], null, 2);
    } else {
      preview.textContent = "(no accepted records)";
    }

    setStatus(status, "");
  } catch (e) {
    setStatus(status, String(e.message || e), true);
    document.getElementById("accepted-count").textContent = "—";
    document.getElementById("rejected-count").textContent = "—";
    document.getElementById("pareto-count").textContent = "—";
    document.getElementById("valset-count").textContent = "—";
    document.getElementById("accepted-preview").textContent = "";
  }
}

async function init() {
  const select = document.getElementById("run-select");
  const status = document.getElementById("status");

  try {
    const runsText = await fetchText("visualizer_dump/runs.json");
    const { runs } = JSON.parse(runsText);
    if (!runs || !runs.length) {
      setStatus(status, "No runs in visualizer_dump/runs.json — run export_gepa_viz_for_pages.py", true);
      return;
    }
    runs.forEach((id) => {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = id;
      select.appendChild(opt);
    });
    select.addEventListener("change", () => loadRun(select.value));
    await loadRun(runs[0]);
  } catch (e) {
    setStatus(
      status,
      "Could not load visualizer_dump/runs.json — run: uv run python scripts/export_gepa_viz_for_pages.py",
      true,
    );
  }
}

init();
