$ErrorActionPreference = "Stop"

function Show-FatalError {
    param(
        [string]$Message
    )

    Write-Host ""
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        $argText = $Arguments -join " "
        throw ("Command failed with exit code {0}: {1} {2}" -f $LASTEXITCODE, $FilePath, $argText)
    }
}

Write-Host "=========================================="
Write-Host "Auto Image Viewer install"
Write-Host "=========================================="

try {
    Push-Location $PSScriptRoot

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Show-FatalError "Python was not found. Install Python first and enable 'Add python.exe to PATH'."
    }

    $requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        Show-FatalError "requirements.txt was not found in $PSScriptRoot."
    }

    $venvPath = Join-Path $PSScriptRoot ".venv"
    $venvPython = Join-Path $venvPath "Scripts\python.exe"

    Invoke-CheckedCommand -FilePath "python" -Arguments @("--version")

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "Creating local virtual environment..."
        Invoke-CheckedCommand -FilePath "python" -Arguments @("-m", "venv", $venvPath)
    }

    Write-Host "Installing required packages into .venv..."
    Invoke-CheckedCommand -FilePath $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-CheckedCommand -FilePath $venvPython -Arguments @("-m", "pip", "install", "-r", $requirementsPath)

    Write-Host ""
    Write-Host "Install completed. Virtual environment: .venv"
    Read-Host "Press Enter to exit"
}
catch {
    Show-FatalError $_.Exception.Message
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
}
