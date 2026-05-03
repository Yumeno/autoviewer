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
Write-Host "Auto Image Viewer startup"
Write-Host "=========================================="

try {
    Push-Location $PSScriptRoot

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        Show-FatalError "Python was not found. Install Python first and enable 'Add python.exe to PATH'."
    }

    Invoke-CheckedCommand -FilePath "python" -Arguments @("--version")

    $viewerPath = Join-Path $PSScriptRoot "viewer.py"
    if (-not (Test-Path -LiteralPath $viewerPath)) {
        Show-FatalError "viewer.py was not found in $PSScriptRoot."
    }

    Write-Host "Installing required packages (Pillow, watchdog)..."
    Invoke-CheckedCommand -FilePath "python" -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-CheckedCommand -FilePath "python" -Arguments @("-m", "pip", "install", "Pillow", "watchdog")

    Write-Host "Launching viewer..."
    Invoke-CheckedCommand -FilePath "python" -Arguments @($viewerPath)
}
catch {
    Show-FatalError $_.Exception.Message
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
}
