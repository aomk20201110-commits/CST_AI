# CST_AI v2.0 — Architecture

This document describes what runs where, what each layer is allowed to do, and which
invariants the layers enforce together. It describes the shipped code, not an
aspiration.

---

## 1. Layers

```
┌───────────────────────────────────────────────────────────────────────────┐
│ caller: an agent, a script, or a human at a prompt                        │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  one of: plan | run | status | inspect | resume
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ dsh-cst-tools/index.js            OPTIONAL Node tool adapter              │
│   registers 5 DSH tools; builds a JSON request; base64-encodes it;        │
│   launches the bridge; parses exactly one JSON envelope from stdout.      │
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ cst_ai_tool_bridge.ps1            PowerShell launcher                     │
│   resolves the interpreter (CST_AI_PYTHON, else the CST 2026 default),     │
│   checks that interpreter and adapter exist, sets PYTHONIOENCODING=utf-8,  │
│   runs cst_ai_tool.py <action> [<base64 request>], propagates the exit code│
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ cst_ai_tool.py                    request adapter                         │
│   decodes the request, calls cst_ai_plugin.public, and converts EVERY      │
│   outcome into one structured envelope: ok / error_type / stage / message /│
│   detail (+ run_id, artifacts, sections). No raw traceback in `message`.   │
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ cst_ai_plugin.public              PUBLIC API v1 (the stable surface)      │
│   load_task | plan | run | status | inspect | resume                      │
│   PUBLIC_API_V1 = (plan, run, status, inspect, resume, load_task)          │
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ cst_ai_plugin.*                   plugin internals                        │
│                                                                           │
│   planner.py       pure function of (task, capability snapshot,           │
│                    reuse_index): no CST, no solve, no writes              │
│   orchestrator.py  the ONLY place that performs stage side effects        │
│   runtime.py       attaches to the live Design Environment                │
│   preflight.py     preconditions, protected paths, output path checks     │
│   task.py          TaskSpec schema + the single solve-budget authority    │
│   validation.py    runtime validation of a TaskSpec                       │
│   runstore.py      atomic checkpoints, append-only events, bundles        │
│   capabilities.py  21-entry capability registry                           │
│   depsnapshot.py   dependency/config/capability snapshot per run          │
│   observables.py   observable identity + serialisation rules              │
│   artifacts.py     artifact descriptors (path, bytes, sha256)             │
│   bundle.py        ResultBundle (JSON) + Markdown rendering               │
│   config.py        configuration precedence (CLI > task > file > default) │
│   errors.py        23-code error taxonomy                                 │
│   versions.py      module version table (provenance, no logic)            │
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ CST-facing modules                thin, testable wrappers                 │
│   cst_session_discovery_v19a.py   find/verify a live Design Environment   │
│   cst_solver_control_v19a.py      solver control plane + lifecycle        │
│   v19b_real_solve.py              solve backend (Floquet)                 │
│   cst_result_api_v19.py           result-tree inventory and 1D arrays     │
│   v110c_analysis.py               resonance / convergence analysis        │
│   v111a_export.py                 farfield activation + angular export    │
│   v111a_analysis.py               farfield grid analysis (HPBW, peaks)    │
│   v118_radiator.py                farfield readback driver               │
│   cst_modelspec_v17.py            model specification + script-injection  │
│                                   rejection                               │
│   cst_tools.py                    shared CST helpers                      │
└───────────────────────────────┬───────────────────────────────────────────┘
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ cst.interface / cst.results       Python API of YOUR CST installation     │
│ CST Studio Suite                  not distributed with this project       │
└───────────────────────────────────────────────────────────────────────────┘
```

**Dependency rule:** the plugin internals import only the standard library, `cst`, and
modules of this repository. `tests/test_dependency_closure.py` enforces that rule by
parsing every module in the tree, so a hidden third-party dependency fails the suite
instead of failing on a user's machine.

---

## 2. The five actions

| action | planning | CST | solves | writes |
|---|---|---|---|---|
| `plan` | full | none | none | nothing |
| `run` | full, then executes | if the plan requires it | only inside the authorised budget | run store, artifacts, bundle |
| `status` | none | none | none | nothing |
| `inspect` | none | none | none | nothing |
| `resume` | rebuilds the stored plan | if missing stages need it | only for stages that are not complete | run store, artifacts |

`status(refresh_runtime=True)` is deliberately not authorised in V2.0: it returns
`REFRESH_RUNTIME_NOT_AUTHORIZED` rather than touching CST. Offline status is the
contract.

### Stage order

```
CONNECT → BUILD → VERIFY → SOLVE → EXTRACT → ANALYZE → REPORT
```

A TaskSpec lists the stages it needs; the list must be unique and in canonical order,
and each stage may only use the capabilities mapped to it:

| stage | capabilities it may use |
|---|---|
| `CONNECT` | `CST_RUNTIME_CONNECT`, `SOLVER_STATUS` |
| `BUILD` | `MODEL_BUILD` |
| `VERIFY` | `DRY_BUILD` |
| `SOLVE` | `SOLVE` |
| `EXTRACT` | `S_PARAMETERS`, `FLOQUET_MULTIMODE`, `POWER_RTA`, `MESH_CONVERGENCE`, `FREQUENCY_CONVERGENCE`, `FARFIELD_COMPLEX_FIELD`, `DIRECTIVITY`, `GAIN`, `REALIZED_GAIN`, `BEAM_DIRECTION`, `HPBW` |
| `ANALYZE` | `RESONANCE_F0`, `FWHM`, `Q_LINEWIDTH`, `PARAMETER_SWEEP`, `REFERENCE_COMPARATOR` |
| `REPORT` | — |

A stage that runs without the output an earlier stage was supposed to produce raises
`STAGE_PREREQUISITE_MISSING` and names the missing producer, rather than surfacing as an
`AttributeError`.

---

## 3. The solve-budget model

`cst_ai_plugin/task.py` is the single authority:

```python
def authorized_solve_budget(task) -> int:
    policy = task["solve_policy"]
    if policy in (FORBID, REUSE_ONLY):  return 0
    if policy == ALLOW_ONE:             return 1
    if policy == ALLOW_UP_TO_N:         return int(task["max_solves"])
```

Rules enforced by `validate_task`:

* required fields: `task_id`, `task_kind`, `stages`, `solve_policy`, and `output_dir`;
* unknown top-level fields are **rejected** (a typo cannot silently change a run);
* `stages` must be non-empty, known, unique and in canonical order;
* `ALLOW_UP_TO_N` requires `max_solves >= 1`;
* `FORBID` / `REUSE_ONLY` must **not** declare `max_solves`;
* `stages` containing `SOLVE` with `solve_policy = FORBID` is refused as a
  contradiction. `REUSE_ONLY` is not a contradiction: the stage stays in the plan and
  becomes a reuse decision;
* exceeding the budget at runtime raises `SOLVE_BUDGET_EXCEEDED`.

The plan reports `PLANNED_SOLVE_COUNT` and `MAX_AUTHORIZED_SOLVES` before any side
effect; the run record reports what was actually performed.

---

## 4. Durability: the run store

```
<run_store>/<run_id>/
    state.json      atomically replaced checkpoint (temp file + fsync + os.replace)
    events.jsonl    append-only event log for this run
    bundle.json     the ResultBundle, once produced
    bundle.md       the same bundle, readable
```

* `RUN_STORE_SCHEMA_VERSION = "1.16.0"`; every checkpoint carries its schema version
  and a payload hash.
* A corrupt or version-incompatible checkpoint raises `RUN_STORE_CORRUPT`. The store
  **never** silently rebuilds a run into an empty state.
* `checkpoint_version` is monotonically non-decreasing across a run's life.
* `status` and `inspect` read this directory only. They need no orchestrator instance,
  no CST and no live process — which is why they still answer after a restart.
* `resume` re-derives the plan from the stored TaskSpec, dependency snapshot, resolved
  config snapshot and original authorisation. It refuses to reduce or reset the budget,
  and answers `ALREADY_COMPLETE` for a finished run.

`run_status` returns, among others: `plugin_lifecycle_state`, `checkpoint`,
`current_stage`, `completed_stages`, `execution_status`, `numerical_status`,
`scientific_status`, `provenance_status`, `solver_usage`, `solver_substate`,
`artifact_count`, `bundle_present`.

`run_inspect` returns: `task_spec`, `task_spec_hash`, `execution_plan`,
`execution_plan_hash`, `stages`, `current_stage`, `artifacts`, `observables`,
`warnings`, `errors`, `unresolved`, `solver_usage`, `scientific_labels`, `events`.

---

## 5. ResultBundle and the four status axes

Every run writes `result_bundle.json` + `result_bundle.md` next to
`plugin_observables.json`. The bundle keeps four independent axes:

| axis | meaning |
|---|---|
| `execution_status` | did the requested stages run to the end |
| `numerical_status` | were the derived quantities resolvable from the data |
| `scientific_status` | may these numbers be used for science, or is this a TEST fixture |
| `provenance_status` | is the record complete: plan hash, artifact hashes, ledger entries |

They are reported separately because they genuinely differ. A run can be
`COMPLETED` + `UNRESOLVED` (a solve succeeded but one observable is not resolvable), or
`COMPLETED` + `RESOLVED` + `TEST_ONLY` (a valid measurement on a deliberately trivial
fixture). Collapsing them into one word would hide exactly the distinction a user needs.

Large arrays are never inlined. They are written as artifacts and referenced by
`path`, `bytes` and `sha256`.

---

## 6. Hashing and identity

* `canonical_hash(value)` — SHA-256 over JSON with sorted keys; every identity in the
  system derives from it.
* `task_spec_hash(task)` — identity of the task as authored (task id, kind, stages,
  policy, max solves, spec, inputs, expected outputs).
* `execution_plan_hash(plan)` — identity of the plan (stage order, solve count,
  projects created, …). Resume rebuilds the plan and compares this hash; a mismatch
  blocks the resume instead of guessing.
* artifact `sha256` — identity of every produced file, used for reuse verification and
  drift detection.
* `dependency_snapshot` — the reuse index, capability snapshot and resolved-config
  snapshot captured **before** execution, so a later process can rebuild the same plan.

---

## 7. Configuration precedence

One rule, applied everywhere:

```
CLI explicit option  >  TaskSpec explicit field  >  config file  >  safe default
```

The resolved value **and** the layer it came from are returned
(`CLI_EXPLICIT` / `TASKSPEC_EXPLICIT` / `CONFIG_FILE` / `SAFE_DEFAULT`).

| key | default | meaning |
|---|---|---|
| `run_store_path` | `runs` | where run records are written |
| `default_output_root` | `runs` | base for relative `output_dir` values |
| `cst_runtime_pid_policy` | `AUTO` | `AUTO` follows the recorded runtime PID then discovery; `EXPLICIT` trusts only the PID you name |
| `expected_project` | none | optional guard naming the project a run may touch |
| `parameter_rules` | none | optional min/max rules for parameter values |

Both PID policies require the recorded PID to be **verified** before it is used. A
stale PID raises `STALE_RUNTIME_PID`; more than one live Design Environment raises
`MULTIPLE_LIVE_DES`. CST_AI never starts, stops, kills or elevates CST, and never picks
one of several live instances on its own.

---

## 8. Error taxonomy

Every failure is one of 23 codes, carried by a `PluginError` subclass with `message`,
`stage`, `detail` and `cause`. `to_dict()` produces the machine-readable form.

| code | class | what it means |
|---|---|---|
| `PLUGIN_ERROR` | `PluginError` | base class |
| `CST_UNAVAILABLE` | `CSTUnavailable` | no reachable CST / the project disappeared |
| `MULTIPLE_LIVE_DES` | `MultipleLiveDesignEnvironments` | more than one live Design Environment |
| `STALE_RUNTIME_PID` | `StaleRuntimePid` | the recorded runtime PID no longer attaches |
| `BUILD_FAILED` | `BuildFailed` | the model could not be built |
| `SEMANTIC_READBACK_FAILED` | `SemanticReadbackFailed` | declared vs native values disagree |
| `START_NOT_ACKNOWLEDGED` | `StartNotAcknowledged` | a solver start was not acknowledged |
| `SOLVER_FAILED` | `SolverFailed` | the solver reported failure |
| `SOLVER_TIMEOUT` | `SolverTimeout` | the solver did not finish in time |
| `SOLVE_BUDGET_EXCEEDED` | `SolveBudgetExceeded` | the task's authorised budget is exhausted |
| `RESULT_NOT_FOUND` | `ResultNotFound` | the run / result does not exist |
| `RESULT_ACTIVATION_FAILED` | `ResultActivationFailed` | a stored result could not be activated |
| `OBSERVABLE_UNRESOLVED` | `ObservableUnresolved` | an observable could not be resolved |
| `RESONANCE_INVALID` | `ResonanceInvalid` | the resonance is not resolvable in the band |
| `SIGNATURE_MISMATCH` | `SignatureMismatch` | a reuse candidate does not match the signature |
| `CACHE_INVALID` | `CacheInvalid` | the cached artifact failed its hash check |
| `SEMANTIC_COMPARISON_MISMATCH` | `SemanticComparisonMismatch` | comparison inputs are not comparable |
| `CAPABILITY_MISSING` | `CapabilityMissing` | the requested capability is not available |
| `PREFLIGHT_BLOCKED` | `PreflightBlocked` | a precondition failed before any side effect |
| `TASK_SCHEMA_ERROR` | `TaskSchemaError` | the TaskSpec is invalid |
| `OBSERVABLE_IDENTITY_ERROR` | `ObservableIdentityError` | observable identity is ambiguous |
| `STAGE_PREREQUISITE_MISSING` | `StagePrerequisiteMissing` | a stage ran without its producer stage's output |
| `EXISTING_RESULT_NOT_FOUND` | `ExistingResultNotFound` | the named existing project is not readable |

The adapter adds transport-level types (`TOOL_ADAPTER_EXCEPTION`,
`UNKNOWN_INSPECT_SECTION`, `MISSING_REQUIRED_FIELD`, …) for request-shape problems that
never reach the plugin. `KI-002` records the one place where that normalisation is
inconsistent.

---

## 9. Invariants

These hold across every layer and are the reason the tool is safe to point at a live
CST session:

1. **No accidental solve.** A real solver run requires both a `SOLVE` stage and a
   policy that authorises it.
2. **One budget authority.** `authorized_solve_budget()` is the only place a solve count
   is derived; refusals and budgets are recorded per run.
3. **No re-solving of solved work.** Reuse requires a signature match *and* a hash match.
4. **No implicit writes.** `output_dir` is mandatory, and `preflight` refuses an output
   directory that is empty, is the repository root, or overlaps a protected input.
5. **Offline reads stay offline.** `plan`, `status` and `inspect` touch no CST.
6. **CST is somebody else's process.** Never started, stopped, killed or elevated; at
   most one live instance is used, and ambiguity is an error.
7. **Read-only means read-only.** Farfield readback changes session/GUI selection state
   to activate a stored result; it never modifies the model history and never re-solves.
8. **Statuses are not collapsed.** Execution, numerical, scientific and provenance
   statuses are reported separately.
9. **Nothing is fabricated.** An unresolvable quantity is `null` with a validity code.
10. **History is not rewritten.** A known defect is documented, not edited out of the
    record.

---

## 10. What this architecture deliberately is not

* No database, no daemon, no service, no network surface: state is files on disk.
* No hidden state outside the run store and the declared output directory.
* No plugin-level caching that could serve a stale number.
* No third-party Python dependency to install, pin or audit.
* No second transport beside the PowerShell → adapter → public API path.
