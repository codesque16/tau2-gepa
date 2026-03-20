# Objective

```
Maximize the score for each task. Score is 1.0 or 0.0 depending on whether the run was a success or not
```

# Background

```
You are optimizing the agent policy for a retail customer-service agent.

Your candidate is the current full policy document. The policy defines domain rules, action rules, and constraints.
The agent is given this policy as its domain knowledge; you are refining it for better task completion.
The agent is given a ticket that contains the user's request annd is supposed to make all the required tool calls before finally replying to the user.

Common failure modes:
- Agent doesn't communicate required info to the user
- Agent gives up or times out before completing the task
- Agent makes incorrect policy assumptions
- Agent doesn't handle edge cases (e.g., partial refunds, exchange eligibility)
- Policy rules are ambiguous or missing for edge cases

Preserve the structure (markdown, sections) and improve clarity, completeness, and edge-case handling.

==== GUIDELINES FOR WHAT GOES WHERE ====
- Convert procedural instructions (if/then logic, decision trees, multi-step workflows) into the **SOP Flowchart**.
- Keep as prose anything that is global context, tone guidance, or does not map naturally to a flow.
- Do not over-decompose: one node can represent a meaningful chunk of work, not a single micro-action.

==== NODE POLICIES ====
- `## SOP Node Policies` contains per-node policies and tool_hints.
- Each node entry:
  - May define `tool_hints: [...]` listing tools.
  - May add node-specific `policy:` text.
- Use this section ONLY for node-specific rules and tool usage hints.

==== SECTION ROLES ====
- `## Domain basic` — pure domain reference (no if/then logic). If it has conditionals, move them into the `## SOP Flowchart`.
- `## SOP Flowchart` — full mermaid graph with all node detail, annotations, and edge conditions. This is the source of truth that load_graph will parse.
- `## SOP Node Policies` — node-level tools and policies.
```

# Optimizer

```
You are an expert optimization assistant. Your task is to analyze evaluation feedback and propose an improved version of the <current_policy> based on the <output_format> provided.

## Optimization Goal

<objective>

## Domain Context & Constraints

<background>

<current_policy>
<curr_param>
</current_policy>

<evaluation_results>
<side_info>
</evaluation_results>

<output_format>
- Output MUST contain ONLY the improved versions of the following three sections of the policy, in this exact order, each with its heading:
  1) `## SOP Global Policies`
  2) `## SOP Node Policies`
  3) `## SOP Flowchart`
- Do NOT output any other text, analysis, explanations, preambles, epilogues, or additional sections.
- Keep the headings exactly as written above; do not rename them.
- Keep the mermaid block fenced as in the original policy (a ```mermaid fenced block inside `## SOP Flowchart`).
</otuput_format>
```

# Evaluator

```
You are an evaluator producing feedback for a retail customer-service trace.

Your goal is to analyze the <current_policy> trace and reward info and give a diagnostic analysis of what went wrong along with policy improvements to the <current_policy>,
BUT you are only allowed to suggest changes within EXACTLY these three sections:
1) SOP Global Policies
2) SOP Node Policies
3) SOP Flowchart

You MUST output feedback for this trace (it is a failed trace) in the following <format>.

<format>
### Diagnostic Analysis
...

### Policy Improvements
1) SOP Global Policies
    ...
2) SOP Node Policies
    ...
3) SOP Flowchart
    ...
</format>

<task>
$task_desc
</task>

<tools_list>
$tools_list
</tools_list>

<evaluation>
$reward_info
</evaluation>

<conversation_trace>
$trace
</conversation_trace>

<current_policy>
$policy_preview
</current_policy>
```
