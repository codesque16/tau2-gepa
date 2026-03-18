# GEPA evolution visualizer (GitHub Pages, `/docs`)

This folder mirrors how **τ²-bench** publishes the trajectory viewer: a **plain static site** (HTML + JS + CSS) under `docs/`, meant for **Settings → Pages → Deploy from a branch → folder `/docs`**.

The React/Vite app under `web/gepa-visualizer/` is the **other** τ² pattern (like `web/leaderboard/`): build with Node and deploy via **GitHub Actions** + `deploy-pages`. Use one or the other.

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

   Open **http://localhost:8080** (or **http://localhost:8080/index.html**).

## GitHub Pages (`/docs` branch deploy)

1. **Settings → Pages → Build and deployment**
2. **Source**: Deploy from a branch  
3. **Branch**: `main`, **Folder**: `/docs` → Save  
4. Commit exported data: run `export_gepa_viz_for_pages.py` and commit `docs/visualizer_dump/` (same idea as τ²-bench committing `docs/data/`).

Site URL (project repo):

- `https://<username>.github.io/<repo>/`

If assets fail to load, set in `index.html` `<head>`:

```html
<base href="/YOUR_REPO_NAME/" />
```

(τ²-bench documents the same for project sites.)

## Files

| File | Purpose |
|------|---------|
| `index.html` | Run selector + summary of accepted/rejected dumps |
| `js/app.js` | Loads `visualizer_dump/runs.json` and per-run JSONL |
| `css/style.css` | Basic layout |
| `.nojekyll` | Tells GitHub Pages not to run Jekyll on this tree |
| `visualizer_dump/` | Populated by `scripts/export_gepa_viz_for_pages.py` (commit after export) |

## Related

- **Actions deploy (Vite)**: `web/gepa-visualizer/` + `.github/workflows/deploy-gepa-visualizer.yml`
- **τ² trajectory viewer docs**: `tau2-bench/docs/README.md`
