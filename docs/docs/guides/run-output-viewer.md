# GEPA run output viewer

Static app under **`examples/run_output_viewer/`** (`index.html`, `app.js`): pick a run, read `best_policy.md`, embed `candidate_tree.html`, compare candidates (plain or highlighted diff).

Full instructions: **`examples/run_output_viewer/README.md`** at the repository root.

## GitHub Pages (viewer only)

Workflow **Deploy run output viewer to GitHub Pages** uploads **only** `examples/run_output_viewer/` — no Jekyll/MkDocs build.

1. **Settings → Pages**: **Source** = **GitHub Actions**.
2. Remove workflows using **`actions/jekyll-build-pages`** if present.
3. URL: **`https://<user>.github.io/<repo>/`**

To populate the run dropdown on Pages, commit `outputs/manifest.json` (and usually run folders) under `outputs/`; see the README above.

## Local use

From repo root (with `outputs/` beside `examples/`):

```bash
python -m http.server 8000
```

Open `http://localhost:8000/examples/run_output_viewer/index.html`.

```bash
uv run python examples/run_output_viewer/gen_outputs_manifest.py
```

Monorepo paths: prefix with `gepa/` where this project lives inside a larger repo.

## Features

- **Run** list from `outputs/manifest.json`
- **best_policy.md** — preview / raw / copy; Mermaid in fenced `mermaid` blocks
- **candidate_tree.html** in an iframe when present
- **Compare** — idx selects from `candidates.json`; **Highlighted diff** via [diff2html](https://github.com/rtfpessoa/diff2html); tree tooltips can **Left/Right pane** into the parent when embedded (`postMessage`)
