param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Ryn'),
    [string]$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [string]$TaskName = 'Ryn-Interactive-Launch',
    [switch]$DefinitionOnly
)
$ErrorActionPreference = 'Stop'
$executable = (Resolve-Path -LiteralPath (Join-Path $InstallDirectory 'Ryn.exe')).Path
$action = New-ScheduledTaskAction -Execute $executable
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
# Task Scheduler defaults to priority 7: BelowNormal CPU, low I/O and memory.
# A foreground Ryn node can otherwise be starved by normal-priority workloads,
# causing health timeouts and watchdog restarts. Priority 4 matches a normal
# interactive launch; do not compensate with longer health timeouts.
$settings = New-ScheduledTaskSettingsSet -Priority 4 -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$definition = New-ScheduledTask -Action $action -Principal $principal -Settings $settings
if ($DefinitionOnly) { return $definition }
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $executable)) {
    throw "Task '$TaskName' belongs to a different executable. Choose a new task name."
}
Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Started Ryn with normal scheduling priority: $executable"
