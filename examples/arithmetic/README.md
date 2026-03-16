# BODMAS arithmetic example

Example to test GEPA and logging (wandb, logfire). Optimizes a **system prompt** so an LLM correctly evaluates arithmetic expressions using BODMAS order. Uses **4-digit numbers** and **10–15+ evaluations** per run.

- **Train:** 12 expressions (4-digit numbers, BODMAS)  
- **Val:** 4 expressions  
- **Budget:** 50 metric calls by default (seed + a few proposal iterations)

## Run

From repo root (or `gepa/`):

```bash
uv sync --extra full
uv run python -m examples.arithmetic.main
```

Options:

- `--fresh` — new run dir with timestamp (`outputs/arithmetic_MM-DD_HH-MM`)
- `--no-wandb` — disable Weights & Biases
- `--no-logfire` — disable Logfire
- `--max-calls N` — evaluation budget (default 50)

Outputs: `outputs/arithmetic/best_prompt.txt` (or under the fresh dir).
