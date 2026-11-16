# Windows integration regression: observe actual child priorities, not just XML.
param([string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Ryn'))
$ErrorActionPreference = 'Stop'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('ryn-priority-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
$taskNames = @()
try {
    $definition = & "$PSScriptRoot/start_windows_interactive.ps1" -InstallDirectory $InstallDirectory -DefinitionOnly
    foreach ($mode in @('default', 'fixed')) {
        $output = Join-Path $temp "$mode.json"
        $child = @'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class PriorityProbe {
 [DllImport("ntdll.dll")] public static extern int NtQueryInformationProcess(IntPtr h,int c,ref int v,int n,out int r);
}
"@
$p=Get-Process -Id $PID
$io=0; $memory=0; $length=0
$s1=[PriorityProbe]::NtQueryInformationProcess($p.Handle,33,[ref]$io,4,[ref]$length)
$s2=[PriorityProbe]::NtQueryInformationProcess($p.Handle,39,[ref]$memory,4,[ref]$length)
if($s1 -ne 0 -or $s2 -ne 0){throw 'Priority query failed'}
[pscustomobject]@{cpu=$p.PriorityClass.ToString();io=$io;memory=$memory}|ConvertTo-Json|Set-Content -LiteralPath '__OUTPUT__' -Encoding UTF8
'@
        $child = $child.Replace('__OUTPUT__', $output.Replace("'", "''"))
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($child))
        $action = New-ScheduledTaskAction -Execute "$env:SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe" -Argument "-NoProfile -EncodedCommand $encoded"
        $name = 'Ryn-Priority-Test-' + [Guid]::NewGuid().ToString('N')
        $taskNames += $name
        $settings = if ($mode -eq 'fixed') { $definition.Settings } else { New-ScheduledTaskSettingsSet }
        Register-ScheduledTask -TaskName $name -Action $action -Principal $definition.Principal -Settings $settings | Out-Null
        Start-ScheduledTask -TaskName $name
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        while (!(Test-Path -LiteralPath $output) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
        if (!(Test-Path -LiteralPath $output)) { throw "Child did not report: $mode" }
        $actual = Get-Content -LiteralPath $output -Raw | ConvertFrom-Json
        $expected = if ($mode -eq 'fixed') { @('Normal', 2, 5) } else { @('BelowNormal', 1, 2) }
        if ($actual.cpu -ne $expected[0] -or $actual.io -ne $expected[1] -or $actual.memory -ne $expected[2]) {
            throw "Unexpected $mode priorities: $($actual | ConvertTo-Json -Compress)"
        }
        [pscustomobject]@{mode=$mode;cpu=$actual.cpu;io=$actual.io;memory=$actual.memory;passed=$true}|ConvertTo-Json -Compress
    }
} finally {
    foreach ($name in $taskNames) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
    # Keep small diagnostic output in the unique temporary directory on failure.
}
