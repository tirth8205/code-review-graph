<#
.SYNOPSIS
    code-review-graph one-line installer (Windows / PowerShell).

.DESCRIPTION
    What this does, in order:
      1. Ensures `uv` (https://docs.astral.sh/uv/) is available, installing it
         via the official Astral installer if missing.
      2. Installs the `code-review-graph` CLI as a uv tool (falling back to
         pipx, then `pip --user`).
      3. Prints the next steps.

    NOTE: This installs `uv`, a single static Python toolchain manager. It is
    NOT a bundled/standalone runtime -- uv manages Python for you, so you do
    not have to set Python up yourself, but a Python interpreter is still
    downloaded/used under the hood by uv.

    Idempotent: safe to re-run.

.EXAMPLE
    irm https://raw.githubusercontent.com/tirth8205/code-review-graph/main/install.ps1 | iex

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>

$ErrorActionPreference = 'Stop'

# Pinned to the official Astral uv installer. We echo this before running it so
# the user can see exactly what is being executed.
$UvInstallerUrl = 'https://astral.sh/uv/install.ps1'

function Write-Info { param([string]$Message) Write-Host $Message }
function Write-Warn { param([string]$Message) Write-Warning $Message }

function Test-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# --- 1. ensure uv ----------------------------------------------------------

function Install-Uv {
    if (Test-Command 'uv') {
        Write-Info "uv already installed: $(uv --version)"
        return
    }

    Write-Info 'uv not found. Installing uv via the official Astral installer.'
    Write-Info "  This runs: irm $UvInstallerUrl | iex"
    Write-Info '  (uv is a single static binary that manages Python for you;'
    Write-Info '   this is not a bundled runtime.)'

    Invoke-RestMethod -Uri $UvInstallerUrl | Invoke-Expression

    # uv installs to %USERPROFILE%\.local\bin. Make it visible for the rest of
    # this session even if the user has not restarted their shell.
    if (-not (Test-Command 'uv')) {
        $candidate = Join-Path $env:USERPROFILE '.local\bin'
        if (Test-Path (Join-Path $candidate 'uv.exe')) {
            $env:Path = "$candidate;$env:Path"
        }
    }

    if (-not (Test-Command 'uv')) {
        throw "uv was installed but is not on PATH. Open a new terminal and re-run this script."
    }
    Write-Info "uv installed: $(uv --version)"
}

# --- 2. install the CLI ----------------------------------------------------

function Install-Crg {
    Write-Info 'Installing code-review-graph with: uv tool install code-review-graph'
    try {
        uv tool install code-review-graph
        if ($LASTEXITCODE -eq 0) { return }
    } catch {
        Write-Warn "uv tool install failed: $_"
    }
    Write-Warn 'uv tool install failed; trying pipx.'

    if (Test-Command 'pipx') {
        Write-Info 'Installing with: pipx install code-review-graph'
        try {
            pipx install code-review-graph
            if ($LASTEXITCODE -eq 0) { return }
        } catch {
            Write-Warn "pipx install failed: $_"
        }
    }

    $pip = $null
    if (Test-Command 'pip') { $pip = 'pip' }
    elseif (Test-Command 'pip3') { $pip = 'pip3' }
    if ($pip) {
        Write-Info "Installing with: $pip install --user code-review-graph"
        try {
            & $pip install --user code-review-graph
            if ($LASTEXITCODE -eq 0) { return }
        } catch {
            Write-Warn "$pip install failed: $_"
        }
    }

    throw "All install methods failed (uv tool / pipx / pip --user). See https://github.com/tirth8205/code-review-graph for manual instructions."
}

# --- 3. next steps ---------------------------------------------------------

function Write-NextSteps {
    Write-Info ''
    Write-Info 'code-review-graph installed. Next steps:'
    Write-Info ''
    Write-Info '  1. Configure your AI coding tools:'
    Write-Info '       code-review-graph install'
    Write-Info ''
    Write-Info '  2. Build the graph for your project (run inside a repo):'
    Write-Info '       code-review-graph build'
    Write-Info ''
    Write-Info '  3. Verify the graph (health / stats check):'
    Write-Info '       code-review-graph status'
    Write-Info ''
    if (-not (Test-Command 'code-review-graph')) {
        Write-Warn "'code-review-graph' is not on your PATH yet. Open a new terminal, or add your uv / Python user scripts directory to PATH."
    }
}

# --- main ------------------------------------------------------------------

Write-Info 'code-review-graph installer'
Install-Uv
Install-Crg
Write-NextSteps
