# Third-Party Notices

CST_AI v2.0.0 is licensed under the MIT License (see [LICENSE](LICENSE)). It contains
**no third-party source code**, no vendored libraries and no bundled binaries. The
notices below exist so that the boundary between this project and the products it
drives is unambiguous.

---

## 1. CST Studio Suite (Dassault Systèmes) — NOT distributed

CST_AI is a client of CST Studio Suite. It calls the Python API that ships with a CST
Studio Suite installation (`cst.interface`, `cst.results`) and, where the documented
workflow requires it, it drives the farfield post-processing objects of the running
application.

* **CST Studio Suite is not included in this repository**, in whole or in part.
* **No CST binaries, libraries, macros, project files, help files or documentation
  pages are redistributed here.**
* **You must own a valid CST Studio Suite installation and licence** to use any part of
  CST_AI that talks to CST. Nothing in this project grants you any right to CST Studio
  Suite.
* **No affiliation, endorsement or sponsorship.** This project is an independent,
  unaffiliated work. It is not produced by, endorsed by, sponsored by, or otherwise
  connected to Dassault Systèmes or the CST Studio Suite product team. Product names
  and trademarks are used only to describe interoperability, and remain the property of
  their respective owners.

Where this project's source code or documentation mentions CST API names, method
signatures or short descriptions of their behaviour, that is done to document the
interface CST_AI calls. Those quotations are short, factual and used descriptively;
they are not extracts of vendor source code.

## 2. Python

CST_AI uses only the Python standard library. It does not require, bundle, vendor or
patch any third-party Python package. `cst` and `cst.results` are provided by your CST
Studio Suite installation, not by this project.

## 3. Node.js / DSH tool adapter

`dsh-cst-tools/` is an optional tool adapter written for the DeepSeek Harness (DSH)
plugin system. It imports only Node.js built-in modules (`node:child_process`,
`node:util`, `node:path`, `node:url`). It declares no npm dependencies, and the file
`dsh-cst-tools/package.json` intentionally has no `dependencies` or `devDependencies`
block.

## 4. PowerShell

`cst_ai_tool_bridge.ps1` uses only built-in PowerShell cmdlets. It launches the Python
interpreter you point it at and nothing else.

## 5. No warranty for engineering results

CST_AI reports what the solver produced together with the status of every derived
number, but it does not replace engineering judgement. Numerical results obtained
through this tool are your responsibility, and the MIT License disclaims all warranties
accordingly. See the "Limitations" section of [README.md](README.md).
