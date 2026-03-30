# How to Read and Follow the Mermaid SOP

The full policy begins with **Retail Agent Rules** (fixed, not shown in optimized components), then this block, then the **SOP Graph** (global policies, node policies, flowchart). Use the SOP Graph as the source of truth for **what to do** at each step. This section only explains **how to interpret and navigate** the mermaid diagram.

For each request, **think** which path through the graph applies, then execute tools consistent with the node you are on. Match graph node labels to the detailed policy blocks in the SOP Graph.

## Mermaid conventions (diagram syntax)

**Format:** The flowchart uses `flowchart TD`, starting with `START([User contacts Agent])`.

**Node shapes by purpose:**

| Shape | Syntax | Use for |
|-------|--------|---------|
| Stadium | `([text])` | Start, end, and terminal outcomes |
| Rectangle | `[text]` | Actions, steps, collecting info |
| Rhombus | `{text}` | Checks, decisions, intent routing |

**Edges:** Conditions appear on edges as `|label|`. Example: `A -->|yes| B` means follow that edge when the condition holds.

**Reading order:** Follow directed edges from `START` unless an edge condition sends you elsewhere. Do not skip authentication or routing nodes unless the graph allows it.
