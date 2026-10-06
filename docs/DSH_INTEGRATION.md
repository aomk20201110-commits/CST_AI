# DSH integration

CST_AI is usable from a plain shell (see `QUICKSTART.md`). This document describes the
optional **agent-facing** surface: the five tools a DeepSeek Harness (DSH) session sees,
and the two hops that carry a request from the harness to the plugin.

```
DSH tool call  →  index.js  →  cst_ai_tool_bridge.ps1  →  cst_ai_tool.py  →  cst_ai_plugin.public
```

Each hop has exactly one job, and none of them can run a solve on its own.

---

## 1. Files and wiring

| file | role |
|---|---|
| `index.js` | registers the five tools; encodes the request; launches the bridge; parses one JSON envelope |
| `package.json` | the DSH bundle manifest: ESM package (`"type": "module"`), `dsh.bundle.patch: ./cordis.patch.yml`, no dependencies, no build scripts |
| `cordis.patch.yml` | the one-line patch that inserts the plugin: `- insert: [ {id: cst-tools, name: dsh-cst-tools} ]` |
| `cst_ai_tool_bridge.ps1` | picks the interpreter, checks both paths exist, sets UTF-8 IO, propagates the exit code |
| `cst_ai_tool.py` | the only entry point that talks to `cst_ai_plugin.public`; shapes every outcome into one envelope |
| `cst_ai_plugin/` | the plugin itself; imported by `cst_ai_tool.py` from the directory beside it |

The adapter is deliberately thin: it may import `cst_ai_plugin.public` and nothing else.
It never connects to CST, never builds, solves or extracts, and never shells out to
`cst_cli.py`.

### One installation action

The repository root is the bundle, so the whole product — adapter, bridge, Python
adapter and Python package — is installed by one standard DSH action:

```powershell
dsh plugin --profile web add github:aomk20201110-commits/CST_AI#<40-char-commit>
```

Consequences worth knowing:

* **No build step.** The manifest declares no `scripts`, so pnpm ≥ 10 never needs an
  `allowBuilds` entry and installing the plugin never executes package code at install
  time.
* **No file subset.** The manifest declares no `files` list, so npm packaging falls back
  to `.gitignore`; a git install copies the tracked checkout. Either way the installed
  copy carries `index.js`, `cordis.patch.yml`, `cst_ai_tool_bridge.ps1`,
  `cst_ai_tool.py` and `cst_ai_plugin/`.
* **Nothing to configure but the interpreter.** `index.js` resolves
  `cst_ai_tool_bridge.ps1` beside itself, and the bridge resolves `cst_ai_tool.py` beside
  *itself*, so an installed copy needs no `CST_AI_BRIDGE`. Set `CST_AI_PYTHON` to the
  CST interpreter.
* **Pin the commit.** A GitHub install fetches source rather than a built artifact; the
  commit is what makes the installed bytes reproducible.

---

## 2. Bridge contract

```powershell
.\cst_ai_tool_bridge.ps1 <plan|run|status|inspect|resume> [<base64 request>]
```

* the action is mandatory and validated by `ValidateSet`;
* the request is optional: without it, the adapter reads JSON from **stdin**;
* the interpreter is `$env:CST_AI_PYTHON` when set, otherwise the CST Studio Suite 2026
  default `C:\Program Files\CST Studio Suite 2026\Python\python.exe`
  (edit this fallback to your own installation);
* both the interpreter and `cst_ai_tool.py` must exist, otherwise the bridge `throw`s
  before doing anything;
* `PYTHONIOENCODING=utf-8` is set for the child process, so paths with non-ASCII
  characters survive the round trip;
* the bridge exits with the adapter's exit code.

Direct use, no Node involved:

```powershell
$req = @{ task_path = 'examples/load_existing_result.json'; store_root = 'runs' } |
       ConvertTo-Json -Compress -Depth 8
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($req))
.\cst_ai_tool_bridge.ps1 plan $b64
```

---

## 3. Adapter contract

`cst_ai_tool.py <action> [<base64 request>]` prints **one** JSON object to stdout and
returns `0`; only a request that cannot be decoded at all returns `2` with a
`MALFORMED_REQUEST` envelope. A failed action is still a `0` exit with `"ok": false` —
failures are data, not a crashed tool.

### 3.1 Success envelope

```json
{
  "ok": true,
  "tool_surface_version": "1.17.0",
  "public_api_version": "1.16.0",
  "api_revision": "1.17.0",
  "action": "plan"
}
```

plus the fields of that action:

| action | additional fields |
|---|---|
| `plan` | `task_identity`, `task_id`, `task_spec_hash`, `plan_hash`, `plan_status`, `stages`, `CST_CONNECTION_REQUIRED`, `PLANNED_SOLVE_COUNT`, `MAX_AUTHORIZED_SOLVES`, `REUSED_SOLVED_ARTIFACTS`, `reuse_decisions`, `projects_created`, `expected_outputs`, `zero_solve_paths`, `risks_and_blockers`, `dependency_snapshot_summary`, `resolved_config`, `cst_contacted`, `solved`, `SWEEP`, `stage_solve_decisions` |
| `run` | `run_id`, `task_identity`, `plugin_lifecycle`, `checkpoint`, `execution_status`, `numerical_status`, `scientific_status`, `solve_usage`, `store_root`, `bundle` (paths + sha256), `artifacts`, `reuse_decisions`, `dependency_snapshot_summary`, `errors`, `warnings` |
| `status` | `run_id`, `available`, `plugin_lifecycle`, `checkpoint`, `execution_status`, `numerical_status`, `scientific_status`, `provenance_status`, `current_stage`, `completed_stages`, `solve_usage`, `solve_substate`, `artifact_count`, `checkpoint_version`, `bundle_present`, `loaded_from_disk`, `created_at`, `updated_at`, `errors`, `warnings`, `rehydration`, `cst_contacted`, `mutated_cst_project` |
| `inspect` | `run_id`, `section`, `sections_available`, `cst_contacted: false`, `opened_cst_project: false`, plus the section payload(s) |
| `resume` | `run_id`, `resume_result`, `checkpoint_before`, `checkpoint_after`, `checkpoint_point_index`, `stages_skipped`, `stages_executed`, `stages_resumed_skipped`, `stages_reuse_skipped`, `points_completed_before`, `points_completed_after`, `points_total`, `point_states`, `fresh_solve_count`, `solve_budget_before`, `solve_budget_after`, `budget_reset_on_resume`, `task_integrity`, `plan_recovery`, `rehydration`, `rehydration_checks`, `config_source`, `config_snapshot_hash`, `store_root` |

`status` and `inspect` are the offline actions: their envelopes state `cst_contacted`
explicitly, and `inspect` additionally reports `opened_cst_project: false`.

### 3.2 Failure envelope

```json
{
  "ok": false,
  "tool_surface_version": "1.17.0",
  "error_type": "TASK_SCHEMA_ERROR",
  "stage": "VALIDATION",
  "message": "unknown task field(s): ['stage']",
  "detail": [ ... ],
  "run_id": null,
  "traceback_in_response": false,
  "traceback_artifact": "<text, only for TOOL_ADAPTER_EXCEPTION>"
}
```

`error_type` is the plugin's own error code whenever the plugin produced one; the
adapter adds transport-shape errors of its own:

| `error_type` | when |
|---|---|
| `TASK_REQUIRED` | `plan` / `run` without `task` or `task_path` |
| `RUN_ID_REQUIRED` | `status` / `inspect` / `resume` without `run_id` |
| `UNKNOWN_INSPECT_SECTION` | `section` outside `summary\|plan\|artifacts\|observables\|errors\|events\|all` |
| `UNKNOWN_TOOL_ACTION` | an action outside the five |
| `MALFORMED_REQUEST` | request body that is not decodable base64/JSON (exit code 2) |
| `MALFORMED_PUBLIC_RESPONSE` | the plugin returned something that is not a dict |
| `PUBLIC_API_ERROR` | `ok=false` with no structured error to lift |
| `TOOL_ADAPTER_EXCEPTION` | any unexpected exception; the type is in `message` and the traceback in `traceback_artifact` |

### 3.3 Size discipline

A solve produces 1001-point curves; a farfield readback produces 2664 points. Those
live in artifacts, never in a tool response:

* events: last `MAX_EVENTS = 40`, with `count`/`returned`/`truncated`;
* lists and mappings: first `MAX_LIST = 50` items;
* arrays inside observables are replaced by `{length, first, last, elided: true}`;
* strings longer than 300 characters are truncated with `…`;
* nesting is capped at depth 3;
* artifacts are reported as metadata: id, type, path, `sha256`, producer, statuses,
  plus `size_bytes` computed from the file on disk when it exists.

Reading the numbers means reading the artifact (`bundle.json`, or the path in the
`artifacts` list), not asking a tool to inline them.

---

## 4. The five DSH tools

| tool | request fields | timeout |
|---|---|---|
| `plan_cst_ai_task` | `task` (object) or `task_path` (string); optional `store_root`, `config_path` | 120 s |
| `run_cst_ai_task` | as above, plus optional `reuse_index` | 1800 s |
| `get_cst_ai_run_status` | `run_id` (required); optional `store_root`, `refresh_runtime` | 120 s |
| `inspect_cst_ai_run` | `run_id` (required); optional `section` (default `summary`), `store_root` | 120 s |
| `resume_cst_ai_run` | `run_id` (required); optional `task`, `store_root`, `config_path` | 1800 s |

Notes that matter in practice:

* `plan` is the safe first call: no CST, no solve, no write. Its envelope contains
  `PLANNED_SOLVE_COUNT` and `MAX_AUTHORIZED_SOLVES` **before** anything happens.
* `status` and `inspect` never touch CST, so they are safe to call any time, including
  after a restart.
* `inspect section="observables"` is the cheap way to read results; `all` is available
  but returns every section at once.
* `refresh_runtime` exists in the tool schema but V2.0 answers
  `REFRESH_RUNTIME_NOT_AUTHORIZED` for `true` — runtime refresh is not part of the
  approved surface.
* `run` / `resume` may take minutes because they may solve; the long timeouts are
  intentional, and both obey the task's solve policy rather than the timeout.

---

## 5. Request encoding

The Node adapter builds one JSON object, encodes it as UTF-8, base64-encodes it, and
passes it as a single command-line argument:

```js
const payload = JSON.stringify(request);
const b64 = Buffer.from(payload, 'utf8').toString('base64');
await runPowerShellBridge(bridge, [ action, b64 ], timeoutMs);
```

Two consequences worth knowing when debugging:

* the request must be JSON-serialisable — an inline `task` is fine, a live object graph
  is not;
* the base64 hop is why a request containing quotes, newlines or non-ASCII paths needs
  no escaping at the PowerShell boundary.

The bridge path itself resolves in this order: `process.env.CST_AI_BRIDGE`, then
`cst_ai_tool_bridge.ps1` in the same directory as `index.js`. No absolute path is
compiled into the adapter, and an installed bundle needs no override at all.

---

## 6. Registering the tool package

The package is an ESM module exporting the DSH plugin shape:

```js
export const name = 'cst-tools';
export const inject = ['tools'];
export function apply(ctx) { /* registers the five tools */ }
```

Registration happens through the manifest, which is the bundle declaration the DSH
loader reads (`package.json` at the repository root):

```json
{
  "name": "dsh-cst-tools",
  "version": "2.0.0",
  "type": "module",
  "main": "index.js",
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

`cordis.patch.yml` contains only the insert line shown in §1, and it names the package
rather than a path, so node resolution finds the installed copy wherever the harness
put it. There is no harness layout to assume and nothing to copy by hand; the install
command in §1 is the whole wiring step.

---

## 7. Operational checklist

1. `CST_AI_PYTHON` points at a Python that can `import cst_ai_plugin` — the interpreter
   bundled with CST Studio Suite (it provides the `cst` module).
2. CST is already running with the project you intend to read; CST_AI never starts it.
   Record the runtime PID in `cst_runtime.json` (`{"pid": <int>}`) so a run can verify
   it rather than guess.
3. `plan` first, and read `PLANNED_SOLVE_COUNT` / `MAX_AUTHORIZED_SOLVES`.
4. `run`, keeping your own task's `output_dir` and `store_root` outside any directory you
   do not want written.
5. `status` for a cheap progress check; `inspect` for content.
6. If a run is interrupted, `resume` with the same `run_id`; it reuses what exists and
   only performs the missing work.
7. If a tool answers `ok: false`, read `error_type` first — it is the actionable part —
   and `stage` second.

Common failures:

| symptom | cause |
|---|---|
| `CST_UNAVAILABLE` | no live Design Environment, or the project path moved |
| `MULTIPLE_LIVE_DES` | more than one live instance; CST_AI refuses to choose |
| `STALE_RUNTIME_PID` | `cst_runtime.json` names a PID that no longer attaches |
| `MISSING_REQUIRED_FIELD` / `TASK_SCHEMA_ERROR` | tool arguments or the TaskSpec are incomplete |
| `TASK_REQUIRED` | `task` / `task_path` was not passed |
| `TOOL_ADAPTER_EXCEPTION` | an unexpected exception; the traceback text is in `traceback_artifact` |
