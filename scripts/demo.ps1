[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string]$ControllerHome = "",
    [string]$Python = "python",
    [switch]$SeedOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $OutputRoot) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $OutputRoot = Join-Path $ProjectRoot ("evidence/demo-" + $stamp)
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if (-not $ControllerHome) { $ControllerHome = Join-Path $OutputRoot "controller" }
$ControllerHome = [IO.Path]::GetFullPath($ControllerHome)
$OldPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
$SourcePath = Join-Path $ProjectRoot "src"
[Environment]::SetEnvironmentVariable("PYTHONPATH", $SourcePath, "Process")
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-Graph {
    param([string[]]$Arguments, [switch]$AllowBlocked)
    $raw = & $Python -m graph_engineering --home $ControllerHome @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not ($AllowBlocked -and $code -eq 2)) {
        throw ("graph-engineering failed with exit code " + $code + ": " + ($Arguments -join " "))
    }
    $data = ($raw -join "`n") | ConvertFrom-Json
    return [pscustomobject]@{ ExitCode = $code; Data = $data }
}

function Save-Receipt {
    param([string]$Name, $Value)
    $path = Join-Path $OutputRoot $Name
    $json = $Value | ConvertTo-Json -Depth 40
    [IO.File]::WriteAllText($path, $json + "`n", $Utf8)
}

function Quote-PowerShell {
    param([string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

Push-Location -LiteralPath $ProjectRoot
try {
    # seed refuses existing fixture paths and never removes earlier evidence.
    $seed = Invoke-Graph -Arguments @("seed", "--output", $OutputRoot)
    Write-Host ("Real Python and JavaScript Git fixtures created under " + $OutputRoot)
    if ($SeedOnly) { return }

    $doctor = Invoke-Graph -Arguments @("doctor") -AllowBlocked
    Save-Receipt -Name "doctor.json" -Value $doctor.Data
    Write-Host ("Endpoint health observed; doctor exit code " + $doctor.ExitCode)

    foreach ($repository in $seed.Data.repositories) {
        $name = Split-Path -Leaf $repository
        $initialized = Invoke-Graph -Arguments @("init", $repository, "--backend", "local", "--trust-local-code", "--routing", "multimodel")
        Save-Receipt -Name ($name + ".initialization.json") -Value $initialized.Data
        $execution = Invoke-Graph -Arguments @("execute", "--config", $initialized.Data.config) -AllowBlocked
        Save-Receipt -Name ($name + ".run.json") -Value $execution.Data
        Write-Host ($name + ": run " + $execution.Data.id + " -> " + $execution.Data.status + " at " + $execution.Data.node)
        if ($execution.Data.status -eq "waiting_approval") {
            $prefix = $Python + " -m graph_engineering --home " + (Quote-PowerShell $ControllerHome)
            Write-Host "Review the retained diff, tests, review, and policy evidence before approving this exact head:"
            Write-Host ($prefix + " approve " + (Quote-PowerShell $execution.Data.id) + " --head " +
                (Quote-PowerShell $execution.Data.state.head) + " --actor '<your-name>'")
            Write-Host ($prefix + " resume " + (Quote-PowerShell $execution.Data.id))
        }
    }
    Write-Host "This script records local backend operations. It does not approve or merge a run automatically."
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable("PYTHONPATH", $OldPythonPath, "Process")
}
