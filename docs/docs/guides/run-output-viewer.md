# Local run output viewer

The published site [gepa-ai.github.io/gepa](https://gepa-ai.github.io/gepa/) documents the GEPA library. It **cannot** read folders on your machine (browser security), so browsing `outputs/` from GitHub Pages is not possible.

To inspect GEPA runs (for example `outputs/tau2_retail_mermaid_…/`), use the **static viewer** shipped in this repo:

- Path: `gepa/examples/run_output_viewer/`
- Files: `index.html`, `app.js`

## Setup

1. From the **repository root** (the parent of `outputs/` and `gepa/`), start any static HTTP server, for example:

   ```bash
   python -m http.server 8000
   ```

2. Generate a manifest of run folders (any directory under `outputs/` that contains `best_policy.md`, `candidate_tree.html`, or `candidates.json`):

   ```bash
   uv run python gepa/examples/run_output_viewer/gen_outputs_manifest.py
   ```

   (Some monorepos also provide `scripts/gen_outputs_manifest.py` at the repository root — either script writes `outputs/manifest.json`.) Re-run when you add new runs.

3. Open in a browser:

   ```text
   http://localhost:8000/gepa/examples/run_output_viewer/index.html
   ```

## Features

- **Run** dropdown — populated from `outputs/manifest.json`.
- **best_policy.md** — Markdown preview (with Mermaid rendering where fenced blocks use `mermaid`) or raw source; **Copy markdown** button.
- **candidate_tree.html** — embedded in an iframe when present for the selected run.
- **Compare candidates** tab — left/right selects from `candidates.json` order; **Plain** shows two text panes, **Highlighted diff** renders a [diff2html](https://github.com/rtfpessoa/diff2html) side-by-side view (red/green line highlights, same idea as [Diffchecker](https://www.diffchecker.com/)).  
  When the tree is embedded, open a node’s tooltip and use **Left pane** / **Right pane** to send that candidate index to the parent viewer (via `postMessage`).

Newly generated `candidate_tree.html` files include those buttons when the page is loaded inside an iframe.
