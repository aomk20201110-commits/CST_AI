# CST_AI v2.0 — Capability Matrix

Registry source: `cst_ai_plugin/capabilities.py` (`CAPABILITIES`, **21 entries**).
Every entry is marked `VERIFIED_E2E` in the shipped registry, which means it was
exercised end-to-end against a real CST Studio Suite installation — not only unit
tested.

Columns:

* **capability** — the registry id. Users ask for a capability, never for an internal
  module name.
* **solve behaviour** — `budgeted` (may spend a solve, only inside the authorised
  budget) or `never` (reads something that already exists).
* **public tools** — how the capability is reached through the five public actions.
  `NO` means it is registered and usable by an application-level entry point, but not
  reachable from `plan` / `run` / `status` / `inspect` / `resume`.
* **limitations** — what a user must know before relying on it.

| capability | what it gives you | solve behaviour | public tools | limitations |
|---|---|---|---|---|
| `CST_RUNTIME_CONNECT` | readiness of a live CST Design Environment | never | `plan` (declares whether a connection is required), `run` (`CONNECT`), `status` (`refresh_runtime`) | read-only discovery; CST is never started, stopped or elevated; ambiguity (more than one live DE) is reported, not guessed |
| `MODEL_BUILD` | a project built from a fixture definition | never | `plan`, `run` (`BUILD`) | builds only from a fixture/ModelSpec; the target project path must not already exist — a collision fails the build instead of overwriting |
| `DRY_BUILD` | semantic readback of a freshly built model | never | `run` (`VERIFY`), `inspect` | compares declared values against native values; declare only values you are prepared to compare, because an exact-string mismatch fails `VERIFY` |
| `SOLVE` | a real solver run with lifecycle reporting | **budgeted** | `run` (`SOLVE`) with `ALLOW_ONE` / `ALLOW_UP_TO_N`; `status` | respects the authorised budget; a solve is never re-run to repair a parser or a report defect |
| `SOLVER_STATUS` | native solver lifecycle and run info | never | `run`, `status` | reports what CST reports; a long solve is reported as still running |
| `S_PARAMETERS` | 1D result-tree channels as structured data | never | `run` (`EXTRACT`), `inspect` (`observables`) | needs an existing result; read-only, never mutates the project; large arrays stay in artifacts and are summarised in the envelope |
| `FLOQUET_MULTIMODE` | unit-cell Floquet channels with mode structure | never | `run` (`EXTRACT` with `incident_mode` + `floquet_modes_per_face`) | Floquet only; the boundary and mode count are declared in the TaskSpec and read back for verification |
| `POWER_RTA` | reflected / transmitted / absorbed balance per frequency point | never | `run` (`EXTRACT` + `ANALYZE`), `inspect` | needs a declared `incident_mode`; the channel→physics rule that was applied is reported with the numbers |
| `MESH_CONVERGENCE` | adaptation results, pass count, final mesh cells | never | `run` (`EXTRACT`), `inspect` | reports what the solver recorded; it does not judge mesh adequacy and never re-meshes |
| `FREQUENCY_CONVERGENCE` | calculation count and final interpolation error | never | `run` (`EXTRACT`), `inspect` | absence of a convergence record yields an explicit "not present" state, not a fabricated number |
| `RESONANCE_F0` | interpolated resonance frequency with validity codes | never | `run` (`ANALYZE`), `inspect` | returns `null` plus a validity failure instead of a number when the resonance is not resolvable inside the band |
| `FWHM` | half-power bandwidth | never | `run` (`ANALYZE`), `inspect` | requires both half-level crossings inside the analysed band; otherwise `null` + reason |
| `Q_LINEWIDTH` | `INTERPOLATED_F0 / FWHM` | never | `run` (`ANALYZE`), `inspect` | `null` whenever `FWHM` is not resolvable |
| `FARFIELD_COMPLEX_FIELD` | complex field on a theta/phi grid from an **existing** farfield monitor | never | `run` (`EXTRACT` with a `farfield` block), `inspect` | requires an already solved farfield result; read-only activation of the stored tree item, never a re-solve |
| `DIRECTIVITY` | peak directivity, linear and dB | never | `run` (`EXTRACT` with `farfield`), `inspect` | the normalisation (e.g. total radiated power) is part of the readback, not an assumption |
| `GAIN` | peak gain, linear and dB | never | `run` (`EXTRACT` with `farfield`), `inspect` | same as `DIRECTIVITY`; not returned when the stored farfield is absent |
| `REALIZED_GAIN` | peak realized gain, linear and dB | never | `run` (`EXTRACT` with `farfield`), `inspect` | normalized to stimulated power; the plot-mode literal contains a space and is handled internally |
| `BEAM_DIRECTION` | direction and topology of the pattern maximum | never | `run` (`EXTRACT` / `ANALYZE`), `inspect` | the topology is reported explicitly (`NEAR_DEGENERATE_AZIMUTH_RING`); a rotationally symmetric pattern has no single azimuth, and none is claimed |
| `HPBW` | −3 dB beamwidth, computed from the read grid | never | `run` (`EXTRACT` / `ANALYZE`), `inspect` | **self-computed**; the CST-native getter is documented for `.Plottype "Polar"` only and raises on these results, so it is an `API_GAP`; the cut used is reported so the number is reproducible |
| `PARAMETER_SWEEP` | one isolated project per point, one shared budget, a unified table | **budgeted** | `run` with `spec.points[]` + `ALLOW_UP_TO_N`; `plan` shows the sweep block and the per-point solve declaration | not an optimiser: no adaptive parameter change and no "best parameter" output; a reused point is admitted only after signature **and** artifact-hash verification |
| `REFERENCE_COMPARATOR` | semantic comparison of scalar / curve / complex / resonance / sweep results | never | **NO** — registered as an optional application capability; V2.0's public actions expose no comparison mode | optional by design; the core does not depend on it, and calling it requires an application-level entry point |

## Solve-relevant summary

| fact | value |
|---|---|
| capabilities that may solve | `SOLVE`, `PARAMETER_SWEEP` — **2 of 21** |
| capabilities that never solve | **19 of 21** |
| capabilities that need an existing result | `S_PARAMETERS`, `FLOQUET_MULTIMODE`, `POWER_RTA`, `MESH_CONVERGENCE`, `FREQUENCY_CONVERGENCE`, `RESONANCE_F0`, `FWHM`, `Q_LINEWIDTH`, `FARFIELD_COMPLEX_FIELD`, `DIRECTIVITY`, `GAIN`, `REALIZED_GAIN`, `BEAM_DIRECTION`, `HPBW` |
| capabilities reachable from the five public actions | **20 of 21** (`REFERENCE_COMPARATOR` excepted) |
| registry verification status | `VERIFIED_E2E` for all 21 |

A capability that may solve only ever solves when the TaskSpec authorises it, and the
run stays inside the authorised budget. `plan` reports the planned solve count and the
authorised budget *before* anything runs; the run record reports what was performed.
