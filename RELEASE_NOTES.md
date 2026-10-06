# CST_AI v2.0.0 — Release Notes

```
version            = 2.0.0
release_status     = usable
capability_registry= 21 capabilities, all VERIFIED_E2E
public_api         = plan / run / status / inspect / resume / load_task
verified_against   = CST Studio Suite 2026 (Windows, bundled Python 3.12.x)
```

This is a **release freeze**, not a feature round. The product implementation is the
tree that the acceptance runs exercised; no orchestration, capability, budget or
provenance logic was changed to produce these notes.

---

## 1. What CST_AI 2.0 is

CST_AI drives CST Studio Suite from an agent-facing tool surface and returns
structured, provenance-carrying results. The stable layer is five actions:

```
plan · run · status · inspect · resume
```

plus `load_task` for reading a TaskSpec from disk.

`plan` is verified to have zero side effects: no CST connection, no build, no solve and
no project write. `run` executes a TaskSpec. `status` and `inspect` read the durable run
store offline. `resume` continues an interrupted run without re-solving completed work.

The release label is `2.0.0`; the in-tree API and module version strings
(`PUBLIC_API_VERSION = 1.16.0`, `PUBLIC_API_REVISION = 1.17.0`, `VERSION = 2.0.0`)
describe the same frozen behaviour at two granularities.

---

## 2. Capabilities in this release

| # | capability | what it gives you |
|---|---|---|
| 1 | **Public tool surface** | Five actions through one launcher (`cst_ai_tool_bridge.ps1`), each returning a single JSON envelope. |
| 2 | **plan / run / status / inspect / resume** | Planning without side effects, execution inside a declared budget, offline durable reads, and cross-process resume. |
| 3 | **Durable run store** | `<store_root>/<run_id>/state.json` + `events.jsonl`, written atomically (temp file + fsync + replace) with a monotonically non-decreasing checkpoint version. Status and inspect work from a completely independent process. |
| 4 | **Solve-budget enforcement** | A TaskSpec declares `solve_policy` (`FORBID` / `ALLOW_ONE` / `ALLOW_UP_TO_N` / `REUSE_ONLY`) and `max_solves`. The plan states `PLANNED_SOLVE_COUNT` and `MAX_AUTHORIZED_SOLVES` before anything runs; `FORBID` together with a `SOLVE` stage is refused. |
| 5 | **Build / solve / result pipeline** | One TaskSpec drives `CONNECT → BUILD → VERIFY → SOLVE → EXTRACT → ANALYZE → REPORT`, with the stage list checked against the task kind. |
| 6 | **S-parameters / Floquet / RTA** | Reads the 1D result tree, parses channel identities into port/mode structure, and reports reflected / transmitted / absorbed balance per frequency point. |
| 7 | **Resonance f0 / FWHM / Q** | Validated extraction: in-band minimum, baseline, half level, parabolic `INTERPOLATED_F0`, half-level crossings for `FWHM`, `Q = f0 / FWHM`, with explicit validity codes. When a crossing is outside the band it reports `null` plus the reason instead of inventing a number. |
| 8 | **Convergence observability** | Mesh adaptation timeline (passes, final cell count) and frequency-sweep state (calculations, final interpolation error) read back from the solved project. |
| 9 | **Farfield / directivity / gain / realized gain / HPBW** | Activates a **stored** farfield monitor through the documented evaluation-list chain (session/GUI state only — the model history is never modified), reads the complex field on a theta/phi grid, and reports peak directivity, gain, realized gain, beam direction and a self-computed HPBW. |
| 10 | **Parameter sweep** | `spec.points[]` drives one isolated project per point under one budget (`ALLOW_UP_TO_N`), with per-point artifacts and a unified table. Reuse is signature-checked *and* hash-checked; already solved points are never solved again. |
| 11 | **Comparator capability** | A semantic comparator (scalar / curve / complex / resonance / sweep) is registered as an optional application capability. It is **not** reachable through the five public actions. |
| 12 | **Structured ResultBundle** | Every run writes `result_bundle.json` + `result_bundle.md` alongside `plugin_observables.json`, each carrying `execution_status`, `numerical_status`, `scientific_status`, `provenance_status`, solve usage, artifacts, observables, warnings, unresolved items and errors. Large arrays are referenced by artifact path + hash, never inlined into a tool response. |
| 13 | **Structured errors** | Failures come back as `{ok: false, error_type, stage, message, detail}` with a code from a fixed 23-entry taxonomy and no raw traceback in the message. |

Per-capability detail — solve behaviour, public-tool reachability, limitations — is in
[CAPABILITY_MATRIX_V2.0.md](CAPABILITY_MATRIX_V2.0.md).

---

## 3. Acceptance summary

Three end-to-end acceptance tasks were run against a real CST installation:

| acceptance | user task | real solves | outcome |
|---|---|---|---|
| **A** | "read this already solved CST project and tell me the main S-parameters, the resonance frequency, the FWHM and Q; do not recompute anything" | **0** | PASSED. 16 S-parameter channels × 1001 points read from a frozen solved project; `INTERPOLATED_F0 = 20.24145661869825 GHz`, `FWHM = 4.10544936261045 GHz`, `Q = 4.930387597285506`, `T_min = 5.033102112150721e-06`. |
| **B** | "build a simple TEST dipole, compute the pattern near 20 GHz, and report directivity, gain, realized gain, main radiation direction and HPBW" | **1** | PASSED. Fresh TEST project built and solved; farfield monitor activated and a 2664-point grid read; directivity peak **2.591272561435593 dB** (1.8160477196689644 linear), gain **2.591882899824891 dB**, realized gain **2.208232664222606 dB**; beam direction θ = 90°, φ = 55° with `NEAR_DEGENERATE_AZIMUTH_RING` (35 equivalent peaks); HPBW **77.37080653476266°**; sanity check `SANITY_CONSISTENT`. |
| **C** | parameter sweep over a three-point fixture family under `ALLOW_UP_TO_N` | **2 new + 1 reused** | PASSED. One point reused a verified artifact (full signature + artifact-hash validation, 0 solves), two points were solved as new isolated projects. The run was **genuinely interrupted** (the child runner's process tree was terminated — the CST solver was not in that tree, so the solver was untouched) and then resumed from durable state without re-solving any solved point. |

Acceptance solve accounting:

```
A     = 0
B     = 1
C     = 2
TOTAL = 3
duplicate_solves = 0
```

---

## 4. Solve accounting across the project's history — do not misread it

Each development round reports its own solve count, and many of those reports are `0`.
A per-round zero is **not** a statement about the project's history. Real CST solves
have happened, are recorded in hash-chained ledgers, and the capabilities above were
verified against them:

| era | real solves |
|---|---|
| earlier development rounds (solver control, Floquet, convergence, resonance, farfield, sweep, first plugin run) | **13** |
| the acceptance run summarised above (B = 1, C = 2) | **3** |
| **total recorded real solves to date** | **16** |

This matters for interpretation: the farfield angular readback capability was verified
against a project that was *really solved* before it was read. The readback itself added
zero solves because it activates and reads an existing result — but the data is real
measurement, not a simulation of one.

---

## 5. Known issues

Three open, non-blocking issues are documented in [KNOWN_ISSUES.md](KNOWN_ISSUES.md):

* **`KNOWN_ISSUE_V2_0_001`** — a project-file artifact can be hashed before the final
  save/close, so the recorded project hash can be older than the file on disk. This
  affects provenance metadata only (no numerical result), and historical records are
  deliberately not rewritten.
* **`KNOWN_ISSUE_V2_0_002`** — `inspect` on an unknown `run_id` returns a generic
  adapter error and attaches a traceback artifact, where `status` and `resume` return
  the structured `RUN_NOT_FOUND`. The envelope stays structured and the message carries
  no traceback.
* **`KNOWN_ISSUE_V2_0_003`** — one historical observation of a resume over-claiming
  completion. The resume path was subsequently changed to re-probe runtime availability
  and dispatch only the missing stages, and resume was re-verified on an interrupted run
  and on a completed run — but resume of a **failed** run was not re-exercised, so that
  exact symptom is *unverified*, not *confirmed fixed*.

---

## 6. Not implemented in V2.0 (and not blocking it)

* natural-language → TaskSpec planning
* optimisation / adaptive parameter search
* job queue and multi-session execution
* multi-CST concurrency
* graphical user interface
* schema migration engine
* native CST HPBW getter (`GetMainWidXdB`-style native getters are documented for
  `.Plottype "Polar"` only and raise on the results this plugin reads, so HPBW is
  computed from the read grid instead)

---

## 7. Verification you can repeat

```powershell
& $env:CST_AI_PYTHON -m unittest discover -s tests -v
```

The suite is offline: it validates the TaskSpec schema and its refusals, the public API
surface, the dependency closure (standard library + `cst` + repository modules only) and
repository hygiene (no credentials, no private absolute paths, no CST binaries). It
contacts no CST and spends no solve.

---

## 8. Contents of this repository

```
README.md                     overview, safety model, capabilities, roadmap
QUICKSTART.md                 first run, step by step, with troubleshooting
RELEASE_NOTES.md              this file
KNOWN_ISSUES.md               the three known issues, in full
CHANGELOG.md                  release history
CAPABILITY_MATRIX_V2.0.md     all 21 capabilities, limitations included
LICENSE                       MIT
THIRD_PARTY_NOTICES.md        CST Studio Suite relationship and disclaimers
VERSION                       2.0.0
cst_ai_tool_bridge.ps1        PowerShell launcher
cst_ai_tool.py                tool adapter (request → envelope)
cst_cli.py                    minimal command-line entry point
cst_ai_config.json            configuration (neutral defaults)
cst_ai_plugin/                the plugin package (public API, planner, orchestrator, …)
cst_tools.py                  CST-facing helpers
cst_modelspec_v17.py          declarative model specification + safety validation
cst_result_api_v19.py         result-tree read API
cst_session_discovery_v19a.py live Design Environment discovery
cst_solver_control_v19a.py    solver control plane
v110c_analysis.py             resonance / convergence analysis
v111a_analysis.py             farfield grid analysis
v111a_export.py               farfield activation + export
v118_radiator.py              farfield readback driver
v19b_real_solve.py            solve backend
index.js                      optional DSH tool adapter (Node, no dependencies)
package.json                  DSH bundle manifest (dsh.bundle.patch)
cordis.patch.yml              the one-line layer that registers the bundle
docs/                         architecture, TaskSpec reference, DSH integration
examples/                     generic example task specs
tests/                        offline test suite
```
