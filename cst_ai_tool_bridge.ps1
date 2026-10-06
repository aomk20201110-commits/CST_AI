# CST_AI tool bridge.
#
# The DSH tool surface (dsh-cst-tools) runs PowerShell bridges and parses one JSON
# object from stdout.
#
# The bridge is a launcher and nothing else: it resolves the interpreter, checks
# the files it needs, and hands the action to cst_ai_tool.py.  All logic lives in
# that adapter, which calls cst_ai_plugin.public.
#
# The CST interpreter is used because EXTRACT needs `cst.results`, and because
# `cst_ai_plugin` is importable under it (verified: 3.12.9).  PLAN, STATUS,
# INSPECT and RESUME never open a project, so they cannot disturb a live session.

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("plan", "run", "status", "inspect", "resume")]
    [string]$Action,

    [Parameter(Position = 1)]
    [string]$RequestB64
)

$ErrorActionPreference = "Stop"

# Point CST_AI_PYTHON at the python.exe of your CST Studio Suite installation.
# The default below matches the standard Windows install location of
# CST Studio Suite 2026.
$Python = $env:CST_AI_PYTHON

if (-not $Python) {
    $Python = "C:\Program Files\CST Studio Suite 2026\Python\python.exe"
}

$Adapter = Join-Path $PSScriptRoot "cst_ai_tool.py"

foreach ($required in @($Python, $Adapter)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "cst_ai_tool_bridge.ps1: required file not found: $required"
    }
}

# the adapter emits UTF-8 JSON; keep the pipe from re-encoding it
$env:PYTHONIOENCODING = "utf-8"

$PyArgs = @($Adapter, $Action)

if ($RequestB64) {
    $PyArgs += $RequestB64
}

& $Python @PyArgs

exit $LASTEXITCODE
