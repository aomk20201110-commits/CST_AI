# TaskSpec reference

A **TaskSpec** is the single input of CST_AI. It is a JSON object (or a Python dict with
the same shape) that says what should happen, which stages may run, and how many solver
runs are authorised. Nothing happens outside it: if a TaskSpec does not authorise a
solve, no solve occurs.

* Schema version: `TASK_SCHEMA_VERSION = "1.15.0"`
* Validator: `cst_ai_plugin.task.validate_task` (also reachable as `public.load_task`
  and inside `plan` / `run`)
* Identity: `task_spec_hash(task)` — SHA-256 over a canonical JSON projection of the
  task (id, kind, stages, policy, max solves, spec, inputs, expected outputs). Resume
  compares this hash; a task edited after a run is a *different* task, not a repair.

---

## 1. Top-level fields

Exactly these keys are accepted. **Any other key is rejected** — a typo cannot silently
change what runs.

| field | type | required | meaning |
|---|---|---|---|
| `task_id` | string | **yes** | stable identity of the task; also the default run identity |
| `task_kind` | enum | **yes** | `BUILD_ONLY`, `BUILD_AND_SOLVE`, `LOAD_EXISTING_RESULT`, `PARAMETER_SWEEP`, `COMPARE_ONLY` |
| `stages` | list of enum | **yes** | subset of the canonical stage order, unique, in order |
| `solve_policy` | enum | **yes** | `FORBID`, `ALLOW_ONE`, `ALLOW_UP_TO_N`, `REUSE_ONLY` |
| `output_dir` | string | **yes** | where artifacts are written; never implicit |
| `max_solves` | int | only for `ALLOW_UP_TO_N` | `>= 1`; forbidden for `FORBID` / `REUSE_ONLY` |
| `label` | string | no | human label carried into the bundle |
| `spec` | object | no | the kind-specific specification (§5) |
| `inputs` | object | no | inputs for the run, e.g. `project_path`, `incident_mode` |
| `expected_outputs` | list of string | no | declared outputs, reported in the bundle and plan |
| `cst` | object | no | CST binding, e.g. `{"runtime_file": "<...>/cst_runtime.json"}` |
| `notes` | string | no | free text, carried through unchanged |

---

## 2. Task kinds

| kind | typical stages | what it means |
|---|---|---|
| `BUILD_ONLY` | `CONNECT, BUILD, VERIFY, REPORT` | build a model from a fixture and read it back; never solves |
| `BUILD_AND_SOLVE` | `+ SOLVE` | build, then solve inside the authorised budget, then extract |
| `LOAD_EXISTING_RESULT` | `CONNECT, EXTRACT, ANALYZE, REPORT` | read an **already solved** project; the zero-solve path |
| `PARAMETER_SWEEP` | `+ SOLVE` over `spec.points` | one solve per point that is not satisfied by reuse |
| `COMPARE_ONLY` | `ANALYZE, REPORT` | compare already-produced observables; no CST |

The kind alone does not authorise anything — the stages and the solve policy do. A
`BUILD_AND_SOLVE` task with `solve_policy = FORBID` is refused as contradictory.

---

## 3. Stages

```
CONNECT → BUILD → VERIFY → SOLVE → EXTRACT → ANALYZE → REPORT
```

The list must be unique and in this order; `stages must be unique and in canonical
order` is raised otherwise. Each stage may use only its mapped capabilities, and a
stage whose prerequisite stage did not run fails with `STAGE_PREREQUISITE_MISSING`
naming the missing producer rather than with an incidental Python error.

---

## 4. Solve policies

| policy | budget | notes |
|---|---|---|
| `FORBID` | **0** | may not appear together with a `SOLVE` stage |
| `REUSE_ONLY` | **0** | `SOLVE` may appear: the planner turns it into a reuse decision |
| `ALLOW_ONE` | **1** | one fresh solve, at most |
| `ALLOW_UP_TO_N` | `max_solves` | requires `max_solves >= 1` |

```python
from cst_ai_plugin.task import authorized_solve_budget
authorized_solve_budget(task)   # the only place a solve count is authorised
```

The plan reports `PLANNED_SOLVE_COUNT` and `MAX_AUTHORIZED_SOLVES` before any side
effect; the run record reports what was actually used (`solver_usage`) and each reuse
decision with the reason it was taken or refused.

---

## 5. `spec` keys

### 5.1 All kinds

| key | meaning |
|---|---|
| `project_path` | project to create/read; resolved to an absolute path |
| `existing_project` | an already solved project to read (`LOAD_EXISTING_RESULT`) |
| `extract_mode`, `analyze_mode` | optional mode selectors for `EXTRACT` / `ANALYZE` |
| `incident_mode` | e.g. `Zmin(1)`; recorded with the extraction |

### 5.2 `BUILD*` — the fixture

The fixture **is** the model: a JSON file whose `command_blocks` are CST history
blocks. CST_AI does not invent geometry, and no fixture ships with this repository that
was derived from any research model — the examples are deliberately trivial TEST
fixtures.

```json
{
  "fixture_id": "example_generic_brick",
  "valid_for_science": false,
  "command_blocks": {
    "units": "With Units\n.Geometry \"mm\"\n.Frequency \"GHz\"\n.Time \"ns\"\nEnd With\n",
    "brick": "With Brick\n.Reset\n.Name \"block\"\n.Component \"test\"\n.Material \"PEC\"\n.Xrange \"-5\", \"5\"\n.Yrange \"-5\", \"5\"\n.Zrange \"-5\", \"5\"\n.Create\nEnd With\n",
    "frequency": "Solver.FrequencyRange 8.0, 12.0\n"
  }
}
```

Each value is one history block: a single string of CST history lines, applied verbatim
with `model3d.add_to_history("v115_cmd_" + name, block)` and followed by one
`Rebuild()`. The public repository ships no fixture files at all — you write your own,
outside the repository, and reference it from `spec.fixture_definition`.

Both the fixture file and the built project live **outside** this repository.

### 5.3 `SOLVE` — solver binding

| key | default | meaning |
|---|---|---|
| `solver_backend` | `"floquet"` | which solve backend to drive |
| `floquet_modes_per_face` | `2` | Floquet modes per face |

### 5.4 `PARAMETER_SWEEP` — `spec.points`

A list; each point is one parameter vector. Per point:

| key | meaning |
|---|---|
| `fixture_definition` | fixture JSON for this point (falls back to `spec.fixture_definition`) |
| `project_path` | project for this point (falls back to `spec.project_path`) |
| `parameters` | the parameter values the point stands for |
| `reuse_signature` / `fixture_signature` | signature the point declares; reuse needs a signature **and** a hash match |
| `farfield` | per-point farfield block (see 5.5) |

### 5.5 `EXTRACT` — farfield readback

Required keys of a `farfield` block: `tree_path`, `name`, `theta`, `phi`,
`monitor_frequency_ghz`. `theta` / `phi` are inclusive `(start, stop, step)` triples.

```json
"farfield": {
  "tree_path": "Farfields\\farfield (f=20.0) [1]",
  "name": "farfield (f=20.0) [1]",
  "theta": [0, 180, 5],
  "phi": [0, 355, 5],
  "monitor_frequency_ghz": 20.0
}
```

Reading a stored farfield activates the result in the live session (GUI/session
selection state only — the model history is never modified) and never re-solves.

---

## 6. Validation rules and their exact refusals

`validate_task` raises `TaskSchemaError` (`TASK_SCHEMA_ERROR`) with one of:

| message | trigger |
|---|---|
| `task must be a mapping` | the task is not a JSON object |
| `unknown task field(s): [...]` | any key outside the allowed set |
| `missing required field: <name>` | `task_id`, `task_kind`, `stages` or `solve_policy` absent |
| `unknown task_kind '<v>'` | kind not in `TASK_KINDS` |
| `unknown solve_policy '<v>'` | policy not in `SOLVE_POLICIES` |
| `stages must be a non-empty list` | empty or wrong type |
| `unknown stage(s): [...]` | stage not in `STAGES` |
| `stages must be unique and in canonical order` | duplicates or wrong order |
| `output_dir is required` | missing or empty |
| `ALLOW_UP_TO_N requires max_solves >= 1` | missing, non-int or `< 1` |
| `FORBID must not declare max_solves` / `REUSE_ONLY must not declare max_solves` | redundant budget |
| `stages include SOLVE but solve_policy is FORBID` | contradictory request |

Validation happens before any CST contact or write: a rejected task returns
`{"ok": false, "stage": "VALIDATION", "errors": [...], "cst_contacted": false,
"solved": false}`.

`cst_modelspec_v17` additionally rejects CST script text that tries to inject control
flow or external commands into generated history blocks (`SCRIPT_PATTERNS`); a fixture
is data, not a program channel.

---

## 7. Authoring and checking a task

```python
from cst_ai_plugin import public

task = public.load_task("examples/load_existing_result.json")   # validates on load
print(task["task_id"], task["stages"], task["solve_policy"])

plan = public.plan(task, store_root="runs")                    # no CST, no solve
print(plan["PLANNED_SOLVE_COUNT"], plan["MAX_AUTHORIZED_SOLVES"])
```

```python
from cst_ai_plugin.task import validate_task, task_spec_hash, authorized_solve_budget

t = {
    "task_id": "demo_zero_solve",
    "task_kind": "LOAD_EXISTING_RESULT",
    "stages": ["CONNECT", "EXTRACT", "ANALYZE", "REPORT"],
    "solve_policy": "FORBID",
    "spec": {"existing_project": "<absolute path to a solved .cst>"},
    "expected_outputs": ["resonance_table"],
    "output_dir": "<absolute path outside this repository>",
}
validate_task(t)
print(task_spec_hash(t), authorized_solve_budget(t))           # ... 0
```

The three shipped examples are minimal, TEST-grade and complete:

* `examples/load_existing_result.json` — zero solve, read a solved project;
* `examples/build_and_solve.json` — one authorised solve;
* `examples/parameter_sweep.json` — sweep with `ALLOW_UP_TO_N` and reuse.

Each carries a top-level `_comment`-style `notes` string explaining what must be edited
before it can run; paths are placeholders, never absolute machine paths.
