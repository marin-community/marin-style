---
name: debug
description: Debug a code bug with a structured debug log that records hypotheses, changes, and results.
---

# Skill: Debug

Systematic debugging for code-level bugs. Keep a structured debug log so each
hypothesis, the change that tests it, and its result are recorded — this keeps a
long investigation from looping and makes the reasoning reviewable.

For infrastructure or operational faults, first read any operations runbook the
repo provides and follow its matching section; the guardrails there (what you may
and may not touch on shared infrastructure) take precedence.

## Code bugs

Maintain a debug log at `docs/debug-log-<task-name>.md`:

```
# Debugging log for <task>

<goal>

## Initial status
<initial status, as reported or observed>

## <Hypothesis N>
The suspected source of the bug, or a change needed to isolate it.

## Changes to make
Which files you are altering and how.

## Results
Test results and any new hypotheses. Repeat the Hypothesis/Results cycle as needed.

## Future work
- [ ] Cleanups observed along the way
```

Work one hypothesis at a time: state it, make the smallest change that confirms
or refutes it, record the result, then move on. When the fix lands, capture a
regression test (see the `write-tests` skill) so the bug cannot return silently.
