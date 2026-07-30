[CmdletBinding()]
param(
    [ValidateSet("auto", "local", "cloud")]
    [string]$Profile = "auto",
    [switch]$DryRun,
    [switch]$Force,
    [string]$IndexUrl,
    [switch]$ProbeMirrors
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CliPath = Join-Path $ScriptDir "asr_cli.py"
$AsrHome = if ($env:ASR_HOME) { $env:ASR_HOME } else { Join-Path $HOME ".video-transcription-audit" }
$VenvDir = Join-Path $AsrHome "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$OfficialIndex = "https://pypi.org/simple"

if ($IndexUrl -and $ProbeMirrors) {
    throw "-IndexUrl and -ProbeMirrors are mutually exclusive."
}

function Invoke-Step {
    param(
        [string]$Description,
        [scriptblock]$Action,
        [string]$Preview
    )
    Write-Host "==> $Description"
    if ($DryRun) {
        if ($Preview) {
            Write-Host "    $Preview"
        }
        return
    }
    & $Action
}

function Find-SystemPython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in @("3.12", "3.11", "3.10", "3.9")) {
            & py "-$version" -c "import sys; raise SystemExit(sys.version_info < (3, 9))" *> $null
            if ($LASTEXITCODE -eq 0) {
                return ,@("py", "-$version")
            }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c "import sys; raise SystemExit(sys.version_info < (3, 9))" *> $null
        if ($LASTEXITCODE -eq 0) {
            return ,@("python")
        }
    }
    return $null
}

function Test-PythonPath {
    param([string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return $false
    }
    & $PythonPath -c "import sys; raise SystemExit(sys.version_info < (3, 9))" 2>$null
    return $LASTEXITCODE -eq 0
}

function Get-DoctorReport {
    param([string]$PythonPath)
    $json = & $PythonPath $CliPath doctor --profile $Profile --install-check --json 2>$null | Out-String
    try {
        return $json | ConvertFrom-Json
    } catch {
        throw "Environment doctor did not return valid JSON using $PythonPath."
    }
}

function Get-NvidiaMemoryMb {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        return 0
    }
    $raw = & nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null |
        Select-Object -First 1
    $memory = 0
    if ([int]::TryParse(($raw -as [string]).Trim(), [ref]$memory)) {
        return $memory
    }
    return 0
}

function Get-RequiredGroups {
    if ($Profile -eq "cloud") {
        return @("base")
    }
    if ((Get-NvidiaMemoryMb) -ge 4096) {
        return @("base", "nvidia")
    }
    return @("base")
}

function Invoke-PipInstall {
    param(
        [string]$PythonPath,
        [string]$RequirementFile,
        [string]$SelectedIndex,
        [switch]$Reinstall
    )
    $arguments = @(
        "-m", "pip", "install",
        "--index-url", $SelectedIndex
    )
    if ($Reinstall) {
        $arguments += "--force-reinstall"
    }
    $arguments += @("-r", $RequirementFile)
    & $PythonPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed while installing $RequirementFile."
    }
}

function Invoke-PipSpecs {
    param(
        [string]$PythonPath,
        [string[]]$Specs,
        [string]$SelectedIndex
    )
    $arguments = @(
        "-m", "pip", "install",
        "--index-url", $SelectedIndex
    )
    $arguments += $Specs
    & $PythonPath @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed while repairing individual requirements."
    }
}

Write-Host "ASR_HOME: $AsrHome"
Write-Host "Profile: $Profile"
if ($DryRun) {
    Write-Host "Dry-run enabled; no changes will be made."
}

$CandidatePython = $null
$CandidateSource = $null
if ($env:ASR_PYTHON) {
    if (-not (Test-PythonPath $env:ASR_PYTHON)) {
        throw "ASR_PYTHON does not point to a working Python 3.9+ executable."
    }
    $CandidatePython = $env:ASR_PYTHON
    $CandidateSource = "ASR_PYTHON"
} elseif (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    if (-not (Test-PythonPath $VenvPython)) {
        throw "The existing ASR_HOME virtual environment is damaged or uses Python older than 3.9. Move it aside and rerun setup."
    }
    $CandidatePython = $VenvPython
    $CandidateSource = "ASR_HOME"
}

$Preflight = $null
if ($CandidatePython) {
    $Preflight = Get-DoctorReport $CandidatePython
    Write-Host "Candidate environment: $CandidateSource ($CandidatePython)"
    if ($Preflight.install_ready -and -not $Force) {
        Write-Host "Environment already install-ready; no packages or indexes were accessed."
        exit 0
    }
}

$SystemPython = Find-SystemPython
if (-not $CandidatePython -and -not $SystemPython) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3.9+ is missing and winget is unavailable. Install Python, then rerun setup."
    }
    Invoke-Step "Install Python 3.11 with winget" {
        winget install --id Python.Python.3.11 --exact --accept-package-agreements --accept-source-agreements
    } "winget install --id Python.Python.3.11 --exact"
    if (-not $DryRun) {
        $SystemPython = Find-SystemPython
        if (-not $SystemPython) {
            throw "Python was installed but is not available in the current shell."
        }
    }
}

$MediaReady = $Preflight -and $Preflight.media_tools_ready
$HasFfmpeg = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)
$HasFfprobe = [bool](Get-Command ffprobe -ErrorAction SilentlyContinue)
if (-not $MediaReady -and (-not $HasFfmpeg -or -not $HasFfprobe)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "FFmpeg is missing and winget is unavailable. Install FFmpeg, then rerun setup."
    }
    Invoke-Step "Install FFmpeg with winget" {
        winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
    } "winget install --id Gyan.FFmpeg --exact"
}

$NewEnvironment = $false
if (-not $CandidatePython) {
    Invoke-Step "Create runtime directory" {
        New-Item -ItemType Directory -Path $AsrHome -Force | Out-Null
    } "New-Item -ItemType Directory -Force '$AsrHome'"
    Invoke-Step "Create Python virtual environment" {
        if ($SystemPython.Count -eq 2) {
            & $SystemPython[0] $SystemPython[1] -m venv $VenvDir
        } else {
            & $SystemPython[0] -m venv $VenvDir
        }
    } "python -m venv '$VenvDir'"
    $CandidatePython = $VenvPython
    $CandidateSource = "new ASR_HOME"
    $NewEnvironment = $true
}

$NeedsPythonRepair = $NewEnvironment -or $Force -or -not $Preflight -or
    -not $Preflight.requirements_ready -or
    -not $Preflight.imports_ready -or
    -not $Preflight.pip_check.ok -or
    -not $Preflight.cuda_ready

if ($NeedsPythonRepair) {
    $ProbePython = if ($DryRun -and -not (Test-Path -LiteralPath $CandidatePython)) {
        if ($SystemPython.Count -eq 2) { $SystemPython[0] } else { $SystemPython[0] }
    } else {
        $CandidatePython
    }
    $SelectedIndex = if ($IndexUrl) { $IndexUrl } else { $OfficialIndex }
    if ($ProbeMirrors -and $ProbePython) {
        Write-Host "==> Probe configured package indexes"
        $SelectedIndex = & $ProbePython $CliPath probe-index
        if ($LASTEXITCODE -ne 0 -or -not $SelectedIndex) {
            throw "Package index probing failed."
        }
        $SelectedIndex = ($SelectedIndex | Select-Object -First 1).Trim()
    }
    Write-Host "Package index: $SelectedIndex"

    if ($NewEnvironment) {
        Invoke-Step "Upgrade pip tooling in the new environment" {
            & $CandidatePython -m pip install --index-url $SelectedIndex --upgrade pip setuptools wheel
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to upgrade pip tooling."
            }
        } "$CandidatePython -m pip install --index-url $SelectedIndex --upgrade pip setuptools wheel"
    } elseif (-not $DryRun) {
        & $CandidatePython -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            & $CandidatePython -m ensurepip --upgrade
            if ($LASTEXITCODE -ne 0) {
                throw "The selected Python environment has no working pip."
            }
        }
    }

    $BrokenRuntime = [bool](
        $Preflight -and
        $Preflight.requirements_ready -and
        (
            -not $Preflight.imports_ready -or
            -not $Preflight.cuda_ready -or
            -not $Preflight.pip_check.ok
        )
    )
    $Reinstall = [bool]($Force -or $BrokenRuntime)
    $UnsatisfiedSpecs = @()
    if ($Preflight) {
        $UnsatisfiedSpecs = @(
            $Preflight.requirements |
                Where-Object { -not $_.satisfied } |
                ForEach-Object { $_.spec }
        )
    }
    if ($Preflight -and -not $Force -and -not $BrokenRuntime -and $UnsatisfiedSpecs.Count -gt 0) {
        $PreviewSpecs = $UnsatisfiedSpecs -join " "
        Invoke-Step "Install missing or incompatible dependencies" {
            Invoke-PipSpecs $CandidatePython $UnsatisfiedSpecs $SelectedIndex
        } "$CandidatePython -m pip install --index-url $SelectedIndex $PreviewSpecs"
    } else {
        $Groups = if ($Preflight -and $Preflight.required_groups) {
            @($Preflight.required_groups)
        } else {
            @(Get-RequiredGroups)
        }
        foreach ($Group in $Groups) {
            $RequirementFile = Join-Path $ScriptDir "requirements-$Group.txt"
            $PreviewReinstall = if ($Reinstall) { " --force-reinstall" } else { "" }
            Invoke-Step "Install or repair $Group dependencies" {
                Invoke-PipInstall $CandidatePython $RequirementFile $SelectedIndex -Reinstall:$Reinstall
            } "$CandidatePython -m pip install --index-url $SelectedIndex$PreviewReinstall -r '$RequirementFile'"
        }
    }
}

if ($DryRun) {
    Write-Host "Dry-run complete."
    exit 0
}

$FinalReport = Get-DoctorReport $CandidatePython
if (-not $FinalReport.install_ready) {
    & $CandidatePython $CliPath doctor --profile $Profile
    throw "Environment installation finished, but the strict readiness check failed."
}

Write-Host "Environment verification passed."
Write-Host "Setup complete. Runtime: $CandidatePython"
