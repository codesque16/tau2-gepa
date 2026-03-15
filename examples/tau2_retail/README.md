# Tau2 Retail

Optimize `policy_solo.md` for the tau2 retail customer-service agent using GEPA in generalization mode. Train on the train split, validate on the test split (from `split_tasks.json`). The candidate is the full policy content; it is written to a temp file and injected via `TAU2_POLICY_SOLO_OVERRIDE` during evaluation.

## Dataset

- **Train**: Task IDs from `split_tasks.json` train split
- **Val (test)**: Task IDs from `split_tasks.json` test split

The candidate is the full policy content (policy_solo.md). During evaluation it is written to a temp file and loaded via `TAU2_POLICY_SOLO_OVERRIDE`. The agent uses `llm_agent_solo2` on `retail_solo_comms` tasks.

## Setup

1. **Python 3.12 or 3.13 recommended** — On Python 3.14, scipy (via tau2’s scikit-learn) may need to build from source and require a Fortran compiler. Use `uv sync --python 3.13 --extra tau2` or install gfortran: `brew install gcc`.

2. Ensure tau2-bench is a sibling of gepa (e.g. both under `Projects/`):
   - `Projects/gepa/`
   - `Projects/tau2-mermaid/tau2-bench/`

3. Install gepa with tau2 (includes full deps):

```bash
uv sync --extra tau2
```

   tau2 is resolved from `../tau2-mermaid/tau2-bench` via `[tool.uv.sources]`. For a different layout, create a symlink or run `uv pip install -e $TAU2_BENCH_PATH` after sync.

4. Set `TAU2_DATA_DIR` to tau2-bench's data directory (or rely on default):

```bash
export TAU2_DATA_DIR=/path/to/tau2-mermaid/tau2-bench/data
```

## Run

From the gepa repo root:

```bash
uv run python -m examples.tau2_retail.main
```

Config (in `main.py`):

- **Reflection LM**: `gemini/gemini-3-flash-preview` (use `openrouter/google/gemini-3-flash-preview` if via OpenRouter)
- **Strategy**: Pareto
- **Wandb**: Enabled (`use_wandb=True`)

The script evaluates baseline (empty instructions) vs optimized instructions on the test split and prints the improvement.
