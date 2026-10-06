# CST_AI v2.0 — Quickstart

A first working run in about ten minutes, with no solver spent until you explicitly
authorise one.

---

## 0. Prerequisites

| requirement | how to check |
|---|---|
| CST Studio Suite 2026 installed | the folder `...\CST Studio Suite 2026\Python\python.exe` exists |
| the CST interpreter | `& "C:\Program Files\CST Studio Suite 2026\Python\python.exe" -c "import cst.interface, cst.results; print('ok')"` |
| PowerShell 5.1+ | `$PSVersionTable.PSVersion` |
| this repository copied locally | you are reading it |

CST_AI uses the standard library only — there is nothing to install with `pip`.

---

## 1. Point the launcher at your interpreter

```powershell
$env:CST_AI_PYTHON = "C:\Program Files\CST Studio Suite 2026\Python\python.exe"
```

`cst_ai_tool_bridge.ps1` falls back to the standard install location when the variable
is unset, so this step is only needed for a non-default installation.

---

## 2. Run the test suite

```powershell
cd <this repository>
& $env:CST_AI_PYTHON -m unittest discover -s tests -v
```

The suite is intentionally offline: it validates the TaskSpec schema, the public API
surface, the dependency closure and repository hygiene. It contacts no CST and spends
no solve.

---

## 3. Create your configuration

Edit `cst_ai_config.json` (or copy it and point at it with `config_path`):

```json
{
  "expected_project": null,
  "parameter_rules": { "my_parameter": { "min": 1.0, "max": 10.0 } },
  "run_store_path": "runs",
  "default_output_root": "runs",
  "cst_runtime_pid_policy": "AUTO"
}
```

* `run_store_path` / `default_output_root` — where run records and task outputs land.
  Both default to `runs` and are **git-ignored**.
* `cst_runtime_pid_policy` — `AUTO` follows the recorded runtime PID
  (`cst_runtime.json`, a single-key file `{"pid": <int>}`) and falls back to discovery;
  `EXPLICIT` trusts only the PID you name. Both policies require the PID to be
  verified; a stale PID raises `STALE_RUNTIME_PID`.
* `expected_project` — an optional guard naming the project a run is expected to touch.
  Leave it `null` if you run against several projects.
* `parameter_rules` — optional min/max validation rules for parameter values.

---

## 4. Your first task: read a result you already have (0 solves)

Save this as `my_read_task.json`, pointing `existing_project` at a **solved** project:

```json
{
  "task_id": "read_existing_result",
  "task_kind": "LOAD_EXISTING_RESULT",
  "stages": ["CONNECT", "EXTRACT", "ANALYZE", "REPORT"],
  "solve_policy": "FORBID",
  "spec": {
    "existing_project": "C:/work/my_solved_model.cst",
    "incident_mode": "Zmin(1)"
  },
  "inputs": {},
  "expected_outputs": ["resonance_table", "s_parameters"],
  "output_dir": "runs/read_existing_result",
  "notes": "TEST_ONLY: read-only first run"
}
```

Plan it first — this performs **no** CST contact, **no** build, **no** solve and writes
nothing:

```powershell
.\cst_ai_tool_bridge.ps1 plan <base64-request>
```

or equivalently from Python (run under the CST interpreter, from the repository root):

```python
from cst_ai_plugin import public

task = public.load_task("my_read_task.json")
plan = public.plan(task, store_root="runs")

print(plan["PLANNED_SOLVE_COUNT"], plan["MAX_AUTHORIZED_SOLVES"])
print([s["stage"] for s in plan["stages"]])
```

Expect `PLANNED_SOLVE_COUNT = 0` and `MAX_AUTHORIZED_SOLVES = 0` for this task, because
`FORBID` authorises nothing.

Now execute it:

```python
result = public.run(task, store_root="runs")
print(result["run_id"], result["execution_status"], result["numerical_status"])
```

Ask for the numbers:

```python
obs = public.inspect(result["run_id"])["observables"]
print(obs)
```

For the same view from the DSH tool surface, pass a section name to
`inspect_cst_ai_run` (`summary`, `plan`, `artifacts`, `observables`, `errors`,
`events`, `all`). The Python API has no `section` argument: it returns every section.

---

## 5. Where everything lands

```
runs/<run_id>/
    state.json         atomically replaced checkpoint (survives a crash)
    events.jsonl       append-only event log for this run
    bundle.json        the ResultBundle
    bundle.md          the same bundle, readable
runs/read_existing_result/     task output directory (as declared in output_dir)
    plugin_observables.json
    result_bundle.json
    result_bundle.md
    artifacts...       curves and grids, referenced by path + SHA-256
```

`status` and `inspect` are offline reads of the run store. They work from any later
process — for example after your agent host restarted:

```python
public.status(run_id)                      # lifecycle, checkpoint, solver_usage
public.inspect(run_id)["artifacts"]        # artifact descriptors (path + sha256)
public.inspect(run_id)["errors"]
public.inspect(run_id)["events"]
```

---

## 6. Interrupt and resume

If a multi-point run is interrupted (crash, killed shell — never kill CST itself), the
durable checkpoint holds what is already done:

```python
public.resume(run_id)
```

Resume re-probes runtime availability, dispatches only the stages that are still
missing, never resets the solve budget and never re-solves a completed point. An
already complete run answers `ALREADY_COMPLETE` and performs no work. If the stored
plan cannot be rebuilt to the same execution identity, the run is **blocked** rather
than re-solved.

---

## 7. Spending your first solve (only if you mean it)

A build-and-solve task needs a **fixture definition**: a JSON file with the CST history
command blocks that create your model. CST project geometry is not redistributable, so
this repository ships the *shape* of a fixture, not ready-made models. Create
`fixtures/my_model.json`:

```json
{
  "command_blocks": {
    "units": "With Units.GetUnitsObject()\n    .SetUnit(\"Length\", \"mm\")\nEnd With",
    "geometry": "<your CST history block>",
    "mesh": "<your CST history block>",
    "solver": "<your CST history block>"
  }
}
```

Then:

```json
{
  "task_id": "build_and_solve_test",
  "task_kind": "BUILD_AND_SOLVE",
  "stages": ["CONNECT", "BUILD", "VERIFY", "SOLVE", "EXTRACT", "ANALYZE", "REPORT"],
  "solve_policy": "ALLOW_ONE",
  "spec": {
    "fixture_definition": "fixtures/my_model.json",
    "project_path": "work/my_model.cst",
    "solver_backend": "floquet",
    "incident_mode": "Zmin(1)"
  },
  "inputs": {},
  "expected_outputs": ["s_parameters", "resonance_table"],
  "output_dir": "runs/build_and_solve_test",
  "notes": "TEST_ONLY: first authorised solve"
}
```

* `ALLOW_ONE` authorises exactly **one** solver run. The plan will report
  `MAX_AUTHORIZED_SOLVES = 1` before anything happens.
* `project_path` must not already exist: a collision fails the build instead of
  overwriting your file.
* `solver_backend` defaults to `floquet`; `floquet_modes_per_face` defaults to `2`.

---

## 8. A parameter sweep with a reuse cache

```json
{
  "task_id": "sweep_demo",
  "task_kind": "PARAMETER_SWEEP",
  "stages": ["CONNECT", "BUILD", "VERIFY", "SOLVE", "EXTRACT", "ANALYZE", "REPORT"],
  "solve_policy": "ALLOW_UP_TO_N",
  "max_solves": 2,
  "spec": {
    "fixture_definition": "fixtures/my_model.json",
    "points": [
      { "parameter": "my_parameter", "value": 6.5, "project_path": "work/p65.cst",
        "fixture_definition": "fixtures/my_model.json" },
      { "parameter": "my_parameter", "value": 7.0, "project_path": "work/p70.cst",
        "fixture_definition": "fixtures/my_model.json" },
      { "parameter": "my_parameter", "value": 7.5, "project_path": "work/p75.cst",
        "fixture_definition": "fixtures/my_model.json" }
    ],
    "incident_mode": "Zmin(1)"
  },
  "inputs": {},
  "expected_outputs": ["s_parameters", "resonance_table"],
  "output_dir": "runs/sweep_demo"
}
```

Pass a reuse cache to `run` so already solved points cost nothing:

```python
result = public.run(task, store_root="runs", reuse_index={
    "my_parameter=7.0": {"artifact": "work/p70.cst", "sha256": "sha256:..."}
})
```

Reuse is admitted only when the point's **signature** and the artifact **hash** both
match. A mismatch denies reuse (`SIGNATURE_MISMATCH` / `CACHE_INVALID`) instead of
silently re-simulating. Reused points consume none of the authorised budget.

---

## 9. Reading the solve accounting

Every run reports its own usage. Check it before and after:

```python
plan  = public.plan(task)
print(plan["PLANNED_SOLVE_COUNT"], plan["MAX_AUTHORIZED_SOLVES"])

result = public.run(task)
print(result["solve_usage"])                      # performed / remaining, this run
print(public.status(result["run_id"])["solver_usage"])
```

`solver_usage` (in the run store) and `solve_usage` (in the `run` envelope) report the
same accounting: the number of real solver runs. A reused point increments the reuse
counter, not the solve counter.

---

## 10. Troubleshooting

| symptom | error code | what to do |
|---|---|---|
| no CST found | `CST_UNAVAILABLE` | start CST Studio Suite yourself; CST_AI never starts it |
| more than one live Design Environment | `MULTIPLE_LIVE_DES` | close the extra instance; CST_AI will not choose for you |
| recorded runtime PID is gone | `STALE_RUNTIME_PID` | update `cst_runtime.json` or use `cst_runtime_pid_policy: "AUTO"` |
| `FORBID_WITH_SOLVE_STAGE` | `TASK_SCHEMA_ERROR` | remove `SOLVE` from `stages`, or authorise a budget |
| unknown field / missing field in a TaskSpec | `TASK_SCHEMA_ERROR` | fix the JSON; the detail lists every problem field |
| the project named by `existing_project` is not readable | `EXISTING_RESULT_NOT_FOUND` | check the path — it must be an absolute or resolvable path to a solved project |
| the stored farfield result cannot be activated | `RESULT_ACTIVATION_FAILED` | confirm the farfield monitor exists in the result tree and that its tree path/name in `spec.farfield` matches |
| a resonance metric is `null` | `RESONANCE_INVALID` / validity code | the metric genuinely is not resolvable in the analysed band; the reason is reported instead of a fabricated number |
| a solve would exceed the budget | `SOLVE_BUDGET_EXCEEDED` | raise `max_solves` (and say so in the TaskSpec), or reuse solved artifacts |

Every failure arrives as a structured envelope:

```json
{ "ok": false, "error_type": "TASK_SCHEMA_ERROR", "stage": "PLAN",
  "message": "unknown task field(s): ['stage']; allowed: [...]", "detail": {} }
```

The full code list is in the "Errors" section of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
