# CST_AI v2.0 — Known Issues

Release `2.0.0` · status: usable.

All three issues below are **open** and **non-blocking**. None of them changes a stored
number, and none of them was "fixed" by rewriting a historical record.

---

## KI-001 — a project-file artifact can be hashed before the final save/close

**Severity:** low — provenance metadata only, no numerical effect.
**Status:** open, deferred to an artifact-finalization improvement.

### What happens

When a run registers a CST project file as an artifact, the project is still open in
the Design Environment, and closing it performs one more save. The SHA-256 recorded in
the artifact descriptor is therefore the hash of the file *at registration time*, while
the file on disk is the newer, re-saved file. A later verification that re-hashes the
project sees a mismatch that is **not** evidence of tampering.

### Scope of the drift

Measured over every artifact descriptor the two acceptance runs registered:

```
acceptance run 1: 11 artifacts checked,  9 unchanged, 2 project files drifted, 0 others changed
acceptance run 2: 10 artifacts checked,  8 unchanged, 2 project files drifted, 0 others changed
```

Only project-file descriptors drift. Observables, semantic readback, farfield angular
data, extraction output, the ledger and the result bundle all re-hash identically, and
the frozen reuse anchor re-hashes identically too — which is why signature-and-hash
based reuse still holds.

### Handling rules

* **Do not change historical artifacts.** The recorded digests stay as they are; the
  drift is documented rather than "fixed" by rewriting history.
* Treat a project-file hash mismatch as expected when the file was closed after
  registration; verify the *non-project* artifacts instead.
* **Future improvement (not part of 2.0.0):** finalize the project (save + close)
  *before* hashing it, or record both a pre-close and a post-close hash.

---

## KI-002 — `inspect` on an unknown `run_id` is not normalised

**Severity:** low — error-shape gap; the failure is still reported, nothing is mutated.
**Status:** open, reproducible.

`status` and `resume` return a structured `RUN_NOT_FOUND` for an unknown `run_id`.
`inspect` does not: the adapter lets the internal `RunNotFound` exception escape into a
generic envelope and attaches a traceback artifact.

```
case                        ok     error_type              stage      traceback artifact
status  unknown run_id      false  RUN_NOT_FOUND           null       no
inspect unknown run_id      false  TOOL_ADAPTER_EXCEPTION  INSPECT    YES
resume  unknown run_id      false  RUN_NOT_FOUND           null       no
```

Observed envelope shape:

```json
{"ok": false, "error_type": "TOOL_ADAPTER_EXCEPTION", "stage": "INSPECT",
 "message": "RunNotFound: no run store entry for run_id '<id>'",
 "detail": {"exception_type": "RunNotFound"},
 "traceback_in_response": false,
 "traceback_artifact": "Traceback (most recent call last): ..."}
```

Why it is non-blocking: the envelope is still structured (`error_type`, `stage`,
`message`, `detail`), `message` carries no raw traceback, `traceback_in_response` is
`false`, and the caller still receives a usable failure. The gap is the error *type*
(`TOOL_ADAPTER_EXCEPTION` instead of `RUN_NOT_FOUND`) and the extra traceback artifact.

**Future improvement:** route `RunNotFound` through the same normalisation that
`status` / `resume` already use, so all five actions answer an unknown `run_id` with
`RUN_NOT_FOUND`.

The other zero-solve error paths probed at the same time are structured:

```
plan with a malformed TaskSpec   -> MISSING_REQUIRED_FIELD   (detail lists every missing field)
plan FORBID + SOLVE stage        -> FORBID_WITH_SOLVE_STAGE
inspect with an unknown section  -> UNKNOWN_INSPECT_SECTION  (message lists valid sections)
```

Zero CST contact, zero solves were involved in that probe.

---

## KI-003 — resume over-claim observed once, and not re-exercised since

**Severity:** medium *if* it recurs (a run can look complete when no work happened).
**Status:** open / **unverified**.

### The observation

In an earlier round, a resume invocation drove a `FAILED` run to `COMPLETED` in about
half a second, with `solver_lifecycle: null` and no CST contact. The run record was
written as `execution_status = COMPLETED`, `numerical_status = RESOLVED`,
`provenance_status = COMPLETE` — resolution-style statuses for a run that performed no
extraction and no analysis.

### What changed afterwards

The resume path was changed so that resume always re-probes runtime availability and
dispatches the stages that are still missing, instead of trusting the stored lifecycle
state. Resume was then exercised for real on two different states:

* a genuinely interrupted multi-point run (the child runner's process tree was
  terminated; the CST solver was **not** in that tree and was not touched), which
  resumed from the durable completed-point record and solved only the remaining point —
  `fresh_solve_count = 1`, no duplicate solve;
* a `COMPLETED` run, which answered `ALREADY_COMPLETE` with `fresh_solve_count = 0`, all
  stages skipped and no CST contact.

### Why it stays open

Resume of a **`FAILED`** run was not re-exercised after the change, so the original
symptom is *unverified*, not *confirmed fixed*. Re-testing it is planned for 2.0.x, and
it must be done without spending an unauthorised solve.

**Workaround until then:** after resuming a run that was previously `FAILED`, read
`status` and confirm `completed_stages` and `solver_usage` match what you expected
before trusting `execution_status`.

---

## Recorded here so they are not mistaken for defects

* **`null` resonance metrics for a point whose crossing is outside the band.** If the
  in-band minimum has no left half-level crossing inside the analysed band, `F0`, `FWHM`
  and `Q` are reported as `null` with
  `validity_failures = ["NO_LEFT_HALF_LEVEL_CROSSING"]`. Reporting a number there would
  be fabrication.
* **A TEST fixture's directivity ≈ 2.59 dB.** That is the computed value for a
  deliberately simple test fixture, not a specification violation. Such runs are
  labelled `scientific_status = TEST_ONLY`.
* **`numerical_status = UNRESOLVED` on a run whose solve succeeded.** The solve can
  succeed while one derived observable is unresolved (for example the resonance
  extractor above). Execution, numerical, scientific and provenance statuses are
  reported separately for exactly this reason.
* **Native farfield convenience getters raise.** The CST-native main-lobe direction and
  angular-width getters are documented for `.Plottype "Polar"` only and raise on the
  farfield results this plugin reads (`API_GAP`). Beam direction is therefore taken from
  the read grid and HPBW is computed from it, with the cut reported so the number is
  reproducible.
