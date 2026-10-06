# Changelog

All notable changes to CST_AI are recorded here. This project uses semantic versioning
for its releases; the internal `PUBLIC_API_VERSION` / module version strings are
provenance and move independently of the release label.

---

## [2.0.0] — 2026 — release freeze

**Status:** usable. First public release.

CST_AI 2.0 is the frozen, end-to-end accepted behaviour of the plugin: a declarative
TaskSpec, a zero-side-effect plan, execution inside an explicitly authorised solve
budget, and a durable, offline-readable run record.

### Added

* Public API v1 — `plan`, `run`, `status`, `inspect`, `resume` (`load_task` for reading
  a TaskSpec from disk), exposed through the PowerShell launcher
  `cst_ai_tool_bridge.ps1` and, optionally, the DSH tool adapter `dsh-cst-tools`.
* TaskSpec schema with strict validation: unknown fields rejected, canonical stage
  order enforced, `output_dir` mandatory, and `FORBID` + `SOLVE` refused as a
  contradiction.
* Solve-budget model: `solve_policy` (`FORBID` / `ALLOW_ONE` / `ALLOW_UP_TO_N` /
  `REUSE_ONLY`) with a single authorising function; `PLANNED_SOLVE_COUNT` and
  `MAX_AUTHORIZED_SOLVES` reported before execution.
* Durable run store: atomic `state.json` checkpoints, append-only `events.jsonl`,
  `bundle.json` / `bundle.md`, corrupt-checkpoint detection that never silently rebuilds
  a run, and cross-process `status` / `inspect` / `resume`.
* ResultBundle with four independent status axes (`execution`, `numerical`,
  `scientific`, `provenance`), artifact descriptors carrying path, size and SHA-256, and
  large arrays kept out of the tool response.
* Capability registry of 21 capabilities, each marked `VERIFIED_E2E`.
* Structured error taxonomy of 23 codes, with `stage`, `detail` and `cause` preserved
  and no raw traceback in the message.
* Parameter sweep with one isolated project per point, one shared budget, and reuse
  admitted only after a signature **and** artifact-hash check.
* Farfield readback: activation of an existing farfield monitor through the documented
  evaluation-list chain, reading the complex field on a theta/phi grid, and deriving
  peak directivity, gain, realized gain, beam direction and a self-computed HPBW.
* Offline test suite (`tests/`, standard library only) covering the TaskSpec schema, the
  public API surface, the dependency closure and repository hygiene.
* Documentation: README, QUICKSTART, RELEASE_NOTES, KNOWN_ISSUES, capability matrix,
  architecture, TaskSpec reference, DSH integration and third-party notices.

### Verified in this release

* Three end-to-end acceptance tasks: a zero-solve read of an existing solved project
  (16 S-parameter channels × 1001 points, `F0 = 20.24145661869825 GHz`,
  `FWHM = 4.10544936261045 GHz`, `Q = 4.930387597285506`), a one-solve build-and-solve
  farfield run (2664-point grid, directivity 2.591272561435593 dB, HPBW
  77.37080653476266°), and a three-point sweep with two new solves, one verified reuse,
  a genuine interruption and a correct resume.
* Solve accounting across the project's history: 13 real solves in earlier rounds plus
  3 in the acceptance runs = **16**, with `duplicate_solves = 0` in the acceptance
  accounting.

### Known issues

* `KI-001` project-file artifact hash can be older than the file on disk.
* `KI-002` `inspect` on an unknown `run_id` is not normalised to `RUN_NOT_FOUND`.
* `KI-003` one historical resume over-claim; not re-exercised on a `FAILED` run, so
  unverified rather than fixed.

See [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

### Not included

Natural-language planning, optimisation, a job queue, multi-CST concurrency, a GUI, a
schema migration engine and native HPBW getters are out of scope for 2.0.0. See the
roadmap in [README.md](README.md).

---

## [1.x] — development series (not released publicly)

The 1.x series is the development history behind 2.0.0: live Design Environment
discovery, the solver control plane, Floquet multimode solves and the R/T/A balance,
mesh and frequency convergence readers, validated resonance extraction
(`f0` / `FWHM` / `Q`), real farfield solves and angular readback, the parameter sweep
engine with reuse, and finally the plugin layer that turned those capabilities into the
planner / orchestrator / run store / capability registry documented here.

Nothing from 1.x is published as a separate release; its verified behaviour is the
substance of 2.0.0, and the three acceptance tasks above are the public evidence.
