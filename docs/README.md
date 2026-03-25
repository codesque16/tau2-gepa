# GEPA docs static sites (`/docs`)

- **`index.html`** — redirects to **`run-output-viewer/`** (GEPA run viewer: `best_policy.md`, trees, diffs). Prefer publishing that viewer via **GitHub Actions** (workflow `deploy-run-output-viewer.yml`).
- **`evolution-dump/`** — JSONL evolution dump viewer (was the old root `docs/index.html`). Needs `docs/visualizer_dump/` from `scripts/export_gepa_viz_for_pages.py`.
- The React/Vite app under `web/gepa-visualizer/` is an alternate full UI; this repo’s Pages workflow deploys **`examples/run_output_viewer/`** only.

## Local preview

1. **Export dump files into `docs/visualizer_dump/`** (from repo `gepa/` root):

   ```bash
   cd gepa
   uv run python scripts/export_gepa_viz_for_pages.py
   ```

   This copies `viz_outputs/<run>/visualizer_dump/*.jsonl` into `docs/visualizer_dump/<run>/` and writes `docs/visualizer_dump/runs.json`.

2. **Serve the docs folder**:

   ```bash
   cd gepa/docs
   python3 -m http.server 8080
   ```

   Open **http://localhost:8080** — you are redirected to **`run-output-viewer/`**; for the JSONL evolution viewer use **http://localhost:8080/evolution-dump/** .

## GitHub Pages (`/docs` branch deploy)

1. **Settings → Pages → Build and deployment**
2. **Source**: Deploy from a branch  
3. **Branch**: `main`, **Folder**: `/docs` → Save  
4. Commit exported data: run `export_gepa_viz_for_pages.py` and commit `docs/visualizer_dump/` (same idea as τ²-bench committing `docs/data/`).

Site URL (project repo):

- `https://<username>.github.io/<repo>/`

For the evolution viewer in a **project page** subpath, `evolution-dump/index.html` uses `<base href="../">` so `visualizer_dump/` resolves from the site root.

## Files

| Path | Purpose |
|------|---------|
| `index.html` | Redirect → `run-output-viewer/` |
| `run-output-viewer/` | Mirror of `examples/run_output_viewer/` (sync when you change the source) |
| `evolution-dump/index.html` | JSONL run selector + metrics |
| `js/app.js` | Evolution viewer logic; loads `visualizer_dump/runs.json` |
| `css/style.css` | Evolution viewer layout |
| `.nojekyll` | Disables Jekyll for this tree |
| `visualizer_dump/` | From `scripts/export_gepa_viz_for_pages.py` |

## Related

- **GitHub Actions Pages**: `.github/workflows/deploy-run-output-viewer.yml`
- **τ² trajectory viewer docs**: `tau2-bench/docs/README.md`
