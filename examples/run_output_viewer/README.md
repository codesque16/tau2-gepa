# GEPA run output viewer

Static HTML UI to browse `outputs/<run>/` (`best_policy.md`, `candidate_tree.html`, `candidates.json`, diff compare).

## GitHub Pages

1. **Settings → Pages → Build and deployment**: set **Source** to **GitHub Actions**.
2. Delete or disable any workflow that uses **`jekyll-build-pages`** (it will fail on `docs/` blog front matter).
3. Push to `main`. The workflow **Deploy run output viewer to GitHub Pages** publishes this folder.

Your site: **`https://<github-username>.github.io/<repo>/`**

The run dropdown loads **`/<repo>/outputs/manifest.json`**. Generate it locally with `uv run python examples/run_output_viewer/gen_outputs_manifest.py`, then commit `outputs/` (and `manifest.json`) if you want the list and files on Pages—otherwise use the viewer locally with `python -m http.server`.

## Local preview

From the **repository root** (next to `outputs/`):

```bash
python -m http.server 8000
```

Open `http://localhost:8000/examples/run_output_viewer/index.html`.

In a monorepo where this repo lives under `gepa/`, use `gepa/examples/run_output_viewer/index.html` instead.
