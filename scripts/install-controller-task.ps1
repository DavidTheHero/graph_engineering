param([string]$HomePath = '', [int]$Port = 8771, [int]$Workers = 2, [switch]$Start)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $HomePath) { $HomePath = Join-Path $projectRoot '.runtime/general-ai-agent' }
$resolvedHome = [System.IO.Path]::GetFullPath($HomePath)
$pythonPath = Join-Path $projectRoot '.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create the project Windows virtual environment first (pythonw.exe is required).' }
$servicePath = Join-Path $PSScriptRoot 'controller_service.py'
$taskName = 'GraphEngineeringController-' + $Port
$accountName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-u "' + $servicePath + '" --home "' + $resolvedHome + '" --port ' + $Port + ' --workers ' + $Workers
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectRoot
$principal = New-ScheduledTaskPrincipal -UserId $accountName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Execute -notin @($pythonPath,(Join-Path $projectRoot '.venv/Scripts/python.exe')) -or $existing.Actions.Arguments -ne $arguments)) { throw 'Existing task has a different controller configuration; choose another port.' }
if (-not $existing) { Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description 'Graph Engineering loopback controller. Manual start, current user, no elevated privileges or automatic logon trigger.' | Out-Null }
elseif ($existing.Actions.Execute -ne $pythonPath) { Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null }
if ($Start) { Start-ScheduledTask -TaskName $taskName }
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
