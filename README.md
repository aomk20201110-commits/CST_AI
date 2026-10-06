# CST_AI v2.0

**CST_AI turns a declarative task specification into a planned, budgeted, persisted
CST Studio Suite workflow — and returns structured results with the provenance needed
to trust them.**

It is neither a GUI nor an autonomous scientific agent. It does not decide what to
simulate, and it does not guess a number when the data is not there. You describe the
task in a `TaskSpec`, CST_AI plans it, states the solver cost *before* anything runs,
executes inside that authorised budget, and writes a durable run record that a later,
completely fresh process can read back.

```
version = 2.0.0
```

---

## Table of contents

- [What CST_AI is](#what-cst_ai-is)
- [Status](#status)
- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Public actions](#public-actions)
- [TaskSpec example](#taskspec-example)
- [Safety and the solve-budget model](#safety-and-the-solve-budget-model)
- [Result capabilities](#result-capabilities)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [License](#license)
- [Third-party disclaimer](#third-party-disclaimer)

---

## What CST_AI is

CST_AI is a plugin layer around CST Studio Suite. It sits between an agent or a
script and the CST Python API, and it provides exactly four things:

1. **A declarative front door.** A task is a JSON document (`TaskSpec`) that names
   the task kind, the stages, the solve policy and the inputs. Unknown fields are
   rejected, so a typo can never silently change a run.
2. **A plan before a side effect.** `plan` is verified to perform zero CST contact,
   zero builds, zero solves and zero project writes. It reports the stage order, the
   planned solve count and the authorised budget up front.
3. **A budgeted execution.** `run` performs only the side effects the plan lists.
   Whether a solver may start is decided *only* by the TaskSpec solve policy. There
   is no hidden "and while we are in there, solve" behaviour.
4. **Durable, structured output.** Every run writes an atomic checkpoint, an
   append-only event log and a `ResultBundle` (JSON + Markdown) that keeps
   **execution**, **numerical**, **scientific** and **provenance** status separate —
   because a solver that exits successfully is not the same thing as a result you may
   use for science.

Large arrays never travel through the agent surface as raw samples: they are written
to artifacts and referenced by path, size and SHA-256.

## Status

| item | value |
|---|---|
| release | `2.0.0` |
| release status | usable, frozen feature set |
| capability registry | 21 capabilities, all `VERIFIED_E2E` |
| public API | `PUBLIC_API_V1` = plan / run / status / inspect / resume / load_task |
| in-tree API version | `PUBLIC_API_VERSION = 1.16.0`, `PUBLIC_API_REVISION = 1.17.0` |
| verified against | CST Studio Suite 2026, Windows, bundled Python 3.12.x |
| automated tests | `tests/` (standard library `unittest`, zero third-party dependencies) |
| known issues | see [KNOWN_ISSUES.md](KNOWN_ISSUES.md) |

Release evidence, capability-by-capability limitations and the acceptance runs are
summarised in [RELEASE_NOTES.md](RELEASE_NOTES.md).

## Features

* **Five public actions** — `plan`, `run`, `status`, `inspect`, `resume` — reachable
  from one launcher and from Python.
* **Solve-budget enforcement** — `solve_policy` (`FORBID` / `ALLOW_ONE` /
  `ALLOW_UP_TO_N` / `REUSE_ONLY`) plus `max_solves`; a task that declares a `SOLVE`
  stage under `FORBID` is refused as a contradiction, not executed.
* **Durable run store** — `<store_root>/<run_id>/{state.json, events.jsonl,
  bundle.json, bundle.md}`; `status` and `inspect` answer from a fresh process, and
  `resume` continues an interrupted run without re-solving anything already solved.
* **Parameter sweeps** — `spec.points[]`, one isolated project per point, one shared
  budget, a signature-checked reuse cache, and real resume after interruption.
* **Results that stay honest** — resonance `f0` / `FWHM` / `Q` return `null` with an
  explicit validity code when a half-level crossing is outside the band instead of
  inventing a number.
* **Structured errors** — every failure is
  `{ok: false, error_type, stage, message, detail}` with a machine-readable code from
  a fixed taxonomy (23 codes), and no raw traceback in the message.
* **Reuse without re-solving** — a reused point must pass both a signature check and
  an artifact-hash check, otherwise reuse is denied.

## Architecture

```
   agent / script
        │
        ▼
   index.js                            DSH tool adapter (Node, optional)
        │  base64 JSON request
        ▼
   cst_ai_tool_bridge.ps1              PowerShell launcher (resolves interpreter)
        │
        ▼
   cst_ai_tool.py                      request adapter: TaskSpec → envelope
        │
        ▼
   cst_ai_plugin.public                PUBLIC API v1: plan/run/status/inspect/resume
        │
        ├── planner.py       pure: no CST, no solve, no writes
        ├── orchestrator.py  stages: CONNECT → BUILD → VERIFY → SOLVE → EXTRACT → ANALYZE → REPORT
        ├── runstore.py      atomic checkpoints + append-only events
        ├── capabilities.py  capability registry (21 entries)
        ├── validation.py    TaskSpec runtime validation
        ├── preflight.py     preconditions, protected paths
        ├── depsnapshot.py   dependency/config snapshot per run
        └── bundle.py        ResultBundle (JSON + Markdown)
        │
        ▼
   cst.interface  /  cst.results       CST Studio Suite Python API
        │
        ▼
   CST Studio Suite                   (NOT distributed with this project)
```

`cst_cli.py` is a small command-line entry point for the same five actions when you do
not want the Node adapter. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
layer-by-layer contract and [docs/DSH_INTEGRATION.md](docs/DSH_INTEGRATION.md) for the
tool-adapter details.

## Requirements

* **Windows** (the launcher is PowerShell; the CST Python API is used as installed).
* **CST Studio Suite 2026** with its bundled Python 3.12.x interpreter.
  CST_AI imports `cst.interface` / `cst.results`, which exist only in that
  interpreter — a stock CPython installation does not have them.
* **PowerShell 5.1 or newer** for `cst_ai_tool_bridge.ps1`.
* **Node.js 18+** *only* if you want the DSH tool adapter (the `dsh-cst-tools` bundle).
* **No Python packages.** CST_AI uses the standard library only. There is nothing to
  `pip install`, and no virtual environment to create.

## Installation

1. Clone or copy this repository somewhere writable:

   ```
   git clone <your-fork-url> CST_AI_public
   ```

2. Point the bridge at your CST interpreter. Either set the environment variable
   (recommended):

   ```powershell
   $env:CST_AI_PYTHON = "C:\Program Files\CST Studio Suite 2026\Python\python.exe"
   ```

   or edit the default inside `cst_ai_tool_bridge.ps1`.

3. Create your local configuration (optional but recommended). Copy
   `cst_ai_config.json` and set:

   * `run_store_path` — where run records are written (default `runs`),
   * `default_output_root` — where task output directories resolve (default `runs`),
   * `cst_runtime_pid_policy` — `AUTO` or `EXPLICIT` (see below),
   * `expected_project` — optional guard: the project path a run is expected to touch,
   * `parameter_rules` — optional min/max rules for parameter validation.

4. *(Optional)* Install the DSH bundle instead of wiring a checkout by hand:

   ```powershell
   dsh plugin --profile web add github:aomk20201110-commits/CST_AI#<40-char-commit>
   ```

   The repository root **is** the bundle: `package.json` declares
   `dsh.bundle.patch: ./cordis.patch.yml`, so one `dsh plugin add` installs the
   Node adapter, the PowerShell bridge and the Python package together. There is no
   build step and no install-time code execution, so pnpm never has to be
   allowlisted. Pin a commit — a GitHub install fetches source, so a branch would
   be free to change under you.

   A manual checkout keeps working: `CST_AI_BRIDGE` points the adapter at another
   installation's bridge.

   ```powershell
   $env:CST_AI_BRIDGE = "C:\path\to\CST_AI_public\cst_ai_tool_bridge.ps1"
   ```

   The DSH marketplace listing is not published yet; until it is, install from
   GitHub as above.

5. Run the tests to confirm the checkout is healthy (see [tests/](tests/)):

   ```powershell
   & $env:CST_AI_PYTHON -m unittest discover -s tests -v
   ```

CST_AI never starts, stops, kills or elevates CST. A Design Environment must already
be running (or be startable by you, the user).

## Quick start

**1. Plan — no CST contact, no solve, no writes:**

```powershell
.\cst_ai_tool_bridge.ps1 plan (base64 of the request)
```

or from Python:

```python
from cst_ai_plugin import public

plan = public.plan({
    "task_id": "read_my_result",
    "task_kind": "LOAD_EXISTING_RESULT",
    "stages": ["CONNECT", "EXTRACT", "ANALYZE", "REPORT"],
    "solve_policy": "FORBID",
    "spec": {"existing_project": "C:/work/solved_model.cst",
             "incident_mode": "Zmin(1)"},
    "inputs": {},
    "expected_outputs": ["resonance_table"],
    "output_dir": "runs/read_my_result",
})

print(plan["PLANNED_SOLVE_COUNT"], plan["MAX_AUTHORIZED_SOLVES"])
```

**2. Run — only the side effects the plan lists:**

```python
result = public.run(task, store_root="runs")
print(result["run_id"], result["execution_status"])
```

**3. Read it back later, from any process:**

```python
public.status(run_id)                  # offline: execution status + solver_usage
public.inspect(run_id)["observables"]  # the numbers, and their status
public.resume(run_id)                  # continue an interrupted run
```

`QUICKSTART.md` walks through the same flow with the example task specs in
[examples/](examples/).

## Public actions

| action | contacts CST | may solve | writes | what it is for |
|---|---|---|---|---|
| `plan` | **no** | **no** | **nothing** | stage order, planned solve count, authorised budget, blockers |
| `run` | yes, if the plan says so | only inside the authorised budget | run store, artifacts, bundle | execute a TaskSpec |
| `status` | **no** | **no** | nothing | durable status read from the run store: lifecycle, checkpoint, stage, execution/numerical/scientific/provenance status, `solver_usage` |
| `inspect` | **no** | **no** | nothing | read-only detail of a stored run: task spec, plan, stages, artifacts, observables, warnings, errors, unresolved items, events. The tool adapter can filter this to one section (`summary`, `plan`, `artifacts`, `observables`, `errors`, `events`, `all`); the Python API returns every section |
| `resume` | yes, if the run needs it | only for stages that are not already complete | run store, artifacts | continue an interrupted run without re-solving solved work |

Both `status` and `inspect` are offline by contract. Requesting a runtime refresh from
`status` is deliberately **not** authorised in V2.0: it answers
`REFRESH_RUNTIME_NOT_AUTHORIZED` instead of quietly touching CST.

## TaskSpec example

```json
{
  "task_id": "cross_fss_point_7p0",
  "task_kind": "PARAMETER_SWEEP",
  "stages": ["CONNECT", "BUILD", "VERIFY", "SOLVE", "EXTRACT", "ANALYZE", "REPORT"],
  "solve_policy": "ALLOW_UP_TO_N",
  "max_solves": 2,
  "spec": {
    "fixture_definition": "fixtures/my_unit_cell.json",
    "points": [
      {"parameter": "cross_span_mm", "value": 6.5, "project_path": "work/point_6p5.cst"},
      {"parameter": "cross_span_mm", "value": 7.0, "project_path": "work/point_7p0.cst"}
    ],
    "incident_mode": "Zmin(1)"
  },
  "inputs": {},
  "expected_outputs": ["s_parameters", "resonance_table"],
  "output_dir": "runs/cross_fss_point_7p0",
  "notes": "TEST_ONLY: not valid for science"
}
```

Required fields: `task_id`, `task_kind`, `stages`, `solve_policy`, `output_dir`.
Everything else is optional and validated. The complete schema, stage rules, solve
policies and per-kind requirements are in [docs/TASKSPEC.md](docs/TASKSPEC.md).

## Safety and the solve-budget model

* **Nothing solves by accident.** A real solver run requires a `SOLVE` stage *and* a
  solve policy that authorises it (`ALLOW_ONE` or `ALLOW_UP_TO_N`). `REUSE_ONLY`
  keeps the stage in the plan and turns it into a reuse decision; `FORBID` refuses a
  task that declares a `SOLVE` stage at all.
* **One place authorises a solve count.** The plan reports `PLANNED_SOLVE_COUNT` and
  `MAX_AUTHORIZED_SOLVES` before execution; the run record reports what was
  performed. Exceeding the budget raises `SOLVE_BUDGET_EXCEEDED`.
* **Never re-solve solved work.** A reused point must match a signature *and* an
  artifact hash. A mismatch denies reuse (`SIGNATURE_MISMATCH` / `CACHE_INVALID`)
  instead of silently re-simulating.
* **No implicit output location.** `output_dir` is mandatory; `preflight` refuses an
  output directory that is empty, is the repository root, or overlaps a protected
  input.
* **CST is treated as somebody else's live process.** CST_AI discovers an existing
  Design Environment and attaches to it; it never starts, stops, kills or elevates
  CST. `cst_runtime_pid_policy` is `AUTO` (follow the recorded runtime PID, then fall
  back to discovery) or `EXPLICIT` (only the PID you name); both require the PID to be
  verified, and ambiguity is reported rather than guessed.
* **Statuses are not interchangeable.** `execution_status`, `numerical_status`,
  `scientific_status` and `provenance_status` are reported separately. A completed
  solver run with an unresolvable observable is `COMPLETED` *and* `UNRESOLVED` —
  both true.

## Result capabilities

21 capabilities are registered; each carries its verification status, its inputs and
outputs, and whether it needs a solve. Highlights:

| capability | what you get | solve |
|---|---|---|
| `S_PARAMETERS` | 1D result-tree channels, read-only, parsed into port/mode structure | never |
| `FLOQUET_MULTIMODE` | unit-cell Floquet channels with declared mode count | never |
| `POWER_RTA` | reflected / transmitted / absorbed balance per frequency point | never |
| `RESONANCE_F0`, `FWHM`, `Q_LINEWIDTH` | validated resonance metrics with explicit validity codes | never |
| `MESH_CONVERGENCE`, `FREQUENCY_CONVERGENCE` | what the solver recorded about adaptation and convergence | never |
| `FARFIELD_COMPLEX_FIELD` | complex field on a theta/phi grid from an **existing** farfield monitor | never |
| `DIRECTIVITY`, `GAIN`, `REALIZED_GAIN` | peak values, linear or dB, with the normalisation stated | never |
| `BEAM_DIRECTION` | the maximum's topology (`NEAR_DEGENERATE_AZIMUTH_RING` when relevant) | never |
| `HPBW` | −3 dB beamwidth computed from the read grid, with the cut reported | never |
| `MODEL_BUILD`, `DRY_BUILD` | build a model from a fixture and read back the semantics | no solve |
| `SOLVE`, `PARAMETER_SWEEP` | the only two capabilities that can spend a solve | budgeted |
| `REFERENCE_COMPARATOR` | optional semantic comparator | never |

Everything except `REFERENCE_COMPARATOR` is reachable through the five public actions.
The full table — verification stage, evidence, availability and limitations — is in
[CAPABILITY_MATRIX_V2.0.md](CAPABILITY_MATRIX_V2.0.md).

## Limitations

* **Windows only.** The launcher and the CST Python API integration are Windows-based.
* **You must own CST.** CST Studio Suite is not part of this project; CST_AI is a
  client of an installation you license separately.
* **One live Design Environment at a time.** CST_AI attaches to a single live DE and
  reports ambiguity instead of picking one arbitrarily.
* **Existing results are required for the read-only capabilities.** `FARFIELD_*`,
  `DIRECTIVITY`, `GAIN`, `REALIZED_GAIN`, `BEAM_DIRECTION` and `HPBW` activate a
  *stored* farfield result; they never re-solve to obtain one.
* **Farfield readback depends on result activation.** The angular data is read through
  the documented farfield evaluation-list chain after selecting the stored tree item.
  The CST-native `GetMainLobeDirection` / `GetAngularWidthXdB` getters are documented
  for `.Plottype "Polar"` only and raise on the results this plugin reads, so beam
  direction is taken from the read grid and HPBW is computed from it.
* **No optimisation.** `PARAMETER_SWEEP` evaluates the points you list; it does not
  adapt parameters, search a space or claim a "best" point.
* **No natural-language input.** You write the TaskSpec. (See the roadmap.)
* **Fixtures are yours.** CST project geometry is not redistributable, so this
  repository ships the *shape* of a fixture definition, not ready-made CST models.
* **Repeatability is bounded by physics and by the solver.** A numerically identical
  re-read of a stored result is exact; a new solve is as reproducible as CST itself.

## Roadmap

| version | theme |
|---|---|
| **2.0.x** | reliability, artifact finalization, error handling (includes `KNOWN_ISSUE_V2_0_001` / `_002`) |
| **2.1** | natural language → TaskSpec (planning only; the solve budget rules stay unchanged) |
| **2.2** | optimisation and parameter search on top of the existing sweep engine |
| **2.3** | job queue and multi-session execution |

Roadmap items are intentions, not commitments, and none of them weakens the
solve-budget or provenance rules above.

## License

MIT — see [LICENSE](LICENSE). Third-party notices, including the CST Studio Suite
relationship, are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Third-party disclaimer

CST Studio Suite and its Python API are products of Dassault Systèmes. They are **not**
distributed with this project, and this project is **not** affiliated with, endorsed
by, or sponsored by Dassault Systèmes. You must have your own valid CST Studio Suite
installation and licence to use CST_AI. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
