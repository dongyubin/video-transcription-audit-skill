[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AsrHome = if ($env:ASR_HOME) { $env:ASR_HOME } else { Join-Path $HOME ".video-transcription-audit" }
$VenvPython = Join-Path $AsrHome "venv\Scripts\python.exe"

if ($env:ASR_PYTHON -and (Test-Path -LiteralPath $env:ASR_PYTHON)) {
    $Python = $env:ASR_PYTHON
} elseif (Test-Path -LiteralPath $VenvPython) {
    $Python = $VenvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Python = (Get-Command python).Source
} else {
    throw "Python runtime not found. Run setup.ps1 first."
}

& $Python (Join-Path $ScriptDir "asr_cli.py") @RemainingArgs
exit $LASTEXITCODE
