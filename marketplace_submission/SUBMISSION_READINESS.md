# Marketplace submission readiness

Status of the DeepSeek Harness marketplace packaging for this repository.
This file is a local working record: nothing here has been submitted, published
or pushed to any catalog.

## 1. Verdicts

| Flag | Value | Why |
| --- | --- | --- |
| `MARKETPLACE_INSTALL_READY` | **false** | the commit that adds the root bundle manifest is still local; the public `main` is `2e6b831d669bb1e1a66be33394731524467e07ed`, which has no root `package.json`, so an install resolves to no package at all |
| `MARKETPLACE_SUBMISSION_READY` | **false** | same blocker, plus the repository is younger than the one-day minimum the catalog gate enforces |
| `MARKET_TOPIC_READY` | **true** | the repository already carries the `dsh-plugin` topic |
| `SUBMISSION_TIME_READY` | **false** | created `2026-10-06T16:05:08Z`; the age gate is satisfied from `2026-10-07T16:05:08Z` |

Single blocker for both flags: **push the packaging commit.** Everything else
that a one-click install depends on has been verified below.

## 2. Why an install did not work before this round

Two independent obstacles, both reproduced with the real toolchain
(`node v24.14.0`, `pnpm 11.8.0`):

1. The repository root had no `package.json`. The marketplace verifies the
   manifest at the URL the catalog entry points at, and generates the install
   command from that same URL, so a root entry needs a root bundle manifest.
   Installing the published commit:

   ```
   dsh plugin --profile marketprobe add github:aomk20201110-commits/CST_AI#2e6b831d669bb1e1a66be33394731524467e07ed
   ```

   fails at the pnpm layer with

   ```
   [ERR_PNPM_INVALID_DEPENDENCY_NAME] Refusing to place a dependency under
   <profile>/node_modules with the invalid alias "CST_AI#2e6b831d669bb1e1a66be33394731524467e07ed"
   ```

   pnpm could not obtain a package name because there is no manifest, so there
   was nothing to install and nothing for the harness to activate.

2. The only manifest lived in `dsh-cst-tools/`, and its `index.js` resolved the
   PowerShell bridge as `../cst_ai_tool_bridge.ps1`, i.e. outside the package.
   Installing that subdirectory delivers only the subdirectory, so the bridge,
   `cst_ai_tool.py` and `cst_ai_plugin/` would not travel with it.

## 3. Packaging choice

**Option A — the repository root is the DSH bundle** (single package, no
subpackage, no npm release, no release tarball required).

| Option | Verdict | Reason |
| --- | --- | --- |
| A. repository-root DSH package | **chosen** | the entry URL is the repository root, so the root manifest is the one the gate and the installer both use; a GitHub install materialises the whole tracked tree, so the bridge and the Python package arrive with it; no build step, so pnpm never needs a `prepare`/`allowBuilds` grant |
| B. monorepo subpackage | rejected | a subdirectory install carries only that subdirectory, so the bridge and `cst_ai_plugin/` would be lost, and a second copy of them inside the subpackage would mean two implementations |
| C. GitHub release tarball | not needed | works (verified below) and stays available as an alternative delivery path, but it adds a build-and-upload step to every release that the source install does not need |
| D. npm package | rejected | same reason as C, plus a publish step, for no additional capability |

Layout after the change:

```
package.json          name dsh-cst-tools, main index.js,
                      dsh.bundle.patch ./cordis.patch.yml,
                      no scripts, no dependencies, no files subset
cordis.patch.yml      - insert: [{ id: cst-tools, name: dsh-cst-tools }]
index.js              the plugin module; resolves cst_ai_tool_bridge.ps1 beside itself
cst_ai_tool_bridge.ps1  PowerShell bridge (locates cst_ai_tool.py beside itself)
cst_ai_tool.py        tool adapter / JSON envelope surface
cst_ai_plugin/        the orchestrator and public API — the only implementation
docs/ examples/ tests/  shipped as-is; not needed at runtime
```

One implementation is kept: the Python package is not copied, and no `files`
subset hides part of the tree from a tarball install.

## 4. Installation

Primary (after the push), pinned to a commit as the official packaging guide
requires:

```
dsh plugin --profile web add github:aomk20201110-commits/CST_AI#<40-character-commit>
```

Alternative, prebuilt tarball (`pnpm pack`, no publish, no release asset
required):

```
dsh plugin --profile web add ./dsh-cst-tools-2.0.0.tgz
```

The user still supplies CST Studio Suite and its Python interpreter:
`CST_AI_PYTHON` when the interpreter is not at the default location. Nothing
else is manual: no copying of the bridge, no editing of the profile, no
`CST_AI_BRIDGE` pointing at a checkout.

## 5. Clean-profile install test

Executed with the harness's own plugin manager, into a dedicated profile
created for the test (`$DSH_HOME/profiles/marketprobe`); the shipping `desktop`
profile was left untouched (verified: its manifest still pins the development
link and none of its files changed).

```
dsh plugin --profile marketprobe add <release-scratch>/market_probe/dsh-cst-tools-2.0.0.tgz
dsh: initialized profile marketprobe at $DSH_HOME/profiles/marketprobe
+ dsh-cst-tools file:.../dsh-cst-tools-2.0.0.tgz
Done in 550ms using pnpm v11.8.0
```

The harness wrote, in the profile manifest:

```json
"dependencies": { "dsh-cst-tools": "file:.../dsh-cst-tools-2.0.0.tgz" },
"dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "dsh-cst-tools"] } }
```

and the resolved configuration contains the layer:

```yaml
# == dsh-cst-tools
- id: cst-tools
  name: dsh-cst-tools
```

## 6. Tool inventory and zero-solve smoke test

Run from an empty working directory against the installed copy, with
`CST_AI_BRIDGE` and `PYTHONPATH` unset and only `CST_AI_PYTHON` set:

- module loaded: `$DSH_HOME/profiles/marketprobe/node_modules/dsh-cst-tools/index.js`
- five tools registered: `plan_cst_ai_task`, `run_cst_ai_task`,
  `get_cst_ai_run_status`, `inspect_cst_ai_run`, `resume_cst_ai_run`
- `plan_cst_ai_task` executed end to end through bridge → `cst_ai_tool.py` →
  `cst_ai_plugin.public.plan`, returning `ok: true`,
  `PLANNED_SOLVE_COUNT: 0`, `MAX_AUTHORIZED_SOLVES: 0`, `cst_contacted: false`,
  `solved: false`, `task_spec_hash sha256:7fd62bc3c600384167e2d6523d87ac928919b4415d302ac49e42b63fe34740f3`,
  `plan_hash sha256:076c9fd93366883b858ae3a81db4e68a2836a2dccf20b507e7bfa0cf347b98c4`
- nothing was written: no run store and no output directory appeared

`REAL_SOLVE_COUNT = 0`, `CST_CONTACT = 0`.

## 7. Fresh-environment proof

The probe imports the module from the installed profile, and the bridge it
resolves is the sibling `cst_ai_tool_bridge.ps1` inside that same installed
directory — the expression in `index.js` and the file that exists agree:

```
module   $DSH_HOME/profiles/marketprobe/node_modules/dsh-cst-tools/index.js
bridge   $DSH_HOME/profiles/marketprobe/node_modules/dsh-cst-tools/cst_ai_tool_bridge.ps1
adapter  $DSH_HOME/profiles/marketprobe/node_modules/dsh-cst-tools/cst_ai_tool.py
package  $DSH_HOME/profiles/marketprobe/node_modules/dsh-cst-tools/cst_ai_plugin/public.py
```

No path in the test touches the development tree or the public checkout, and no
environment variable points at either. The PLAN call succeeding proves the
Python package was imported from the installed copy: with `PYTHONPATH` unset the
adapter can only find `cst_ai_plugin` next to itself.

## 8. Package contents and hashes

Both delivery forms were built from the same commit and installed successfully.
The rows below are the artifacts built at commit
`c36ecb7cc9c1536568dda9da9670e32838e8cc78`, i.e. before this documentation file
existed; because the package ships the whole tree, adding this file changes the
bytes, so the authoritative hashes for the current HEAD are recorded in
`package_hashes.txt` in the release scratch rather than here.

| Artifact | Bytes | SHA256 |
| --- | --- | --- |
| `pnpm pack` tarball (`package/` prefix, pack-list filtered) | 171821 | see `package_hashes.txt` |
| git-archive tarball (same shape as the GitHub `codeload` archive) | 173006 | `20EA96D55757B6DCA07EC1A7C72510FF7AE8CBD4526A732A816380E6F66897B9` |

Content manifest: the git-archive form contains 58 tracked files plus directory
entries; the packed form contains 57 files, the difference being `.gitignore`,
which npm-style pack lists always drop. Both contain `package.json`,
`cordis.patch.yml`, `index.js`, `cst_ai_tool_bridge.ps1`, `cst_ai_tool.py` and
the whole `cst_ai_plugin/` package.

Excluded as required: no run stores, no reports, no ledgers, no CST projects, no
local configuration, no secrets, no private paths, no `tests` fixtures that hold
private material.

## 9. Security scan of the installed package

The repository's independent release audit (run outside the repository) was
pointed at the extracted package tree:

```
PAPER_RELATED_MATCHES = 0
SECRET_MATCHES = 0
PRIVATE_ABSOLUTE_PATH_MATCHES = 0
THIRD_PARTY_IMPORTS = 0
FORBIDDEN_BINARY_FILES = 0
ALL_CLEAR = true
```

Extensions `*.cst`, `*.ffm`, `*.fme`, `*.feh`, `*.lok`, `*.sat`, `*.prj`,
`*.his`: none present.

## 10. Catalog gate checklist

Checked against the gate itself (`scripts/check-submission.mjs`) and the entry
schema (`scripts/lib/entries.mjs`), not against memory:

| Gate | Status |
| --- | --- |
| `package.json` at the entry URL declares `dsh.bundle` | pass — root `package.json`, `dsh.bundle.patch = ./cordis.patch.yml`, a safe relative path |
| repository older than one day | pending — satisfied from `2026-10-07T16:05:08Z` |
| repository exists, not archived | pass |
| not the harness itself | pass — no `@deepseek-ai/dsh-base` / `dsh-web-app` / `dsh-headless` package in the tree |
| entry file name | `data/plugins/aomk20201110-commits__CST_AI.yml` |
| entry `url` | `https://github.com/aomk20201110-commits/CST_AI` (root, so the root manifest is the verified one) |
| `name` | `CST_AI`; no `owner/repo#sub` form needed because the bundle is at the root |
| `category` | `tools` |
| `description.en` | one line, ends with a period, quoted because it contains `": "`, states only implemented behaviour |

## 11. Deliberately not done

- no pull request to the catalog, no fork of it, no branch pushed to it
- no `npm publish`
- no GitHub topic change (the topic was already present)
- no release asset uploaded
- the packaging commit has not been pushed

## 12. After the push

1. `git push origin main` (the packaging commit and this file).
2. Re-run the install against the real URL:
   `dsh plugin --profile <test> add github:aomk20201110-commits/CST_AI#<new commit>`
   and repeat the inventory plus zero-solve smoke test.
3. Confirm the catalog gate reads the root manifest and reports the repository
   as old enough.
4. Only then open the catalog pull request with `data/plugins/aomk20201110-commits__CST_AI.yml`
   containing `plugin.yml` verbatim.

## 13. Maintenance rules this packaging depends on

- the root `package.json` stays the single bundle manifest; `version` stays an
  exact stable semver and must match `VERSION`
- no `scripts` entry, so a git install never asks the user to authorise install
  time code execution
- no `files` subset, so a tarball install carries the same tree as a git install
- the bridge is resolved relative to `index.js`, never through a checkout path
- `cst_ai_plugin/` keeps exactly one implementation
