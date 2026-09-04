param(
    [Parameter(Mandatory = $true)][string]$Sidecar,
    [int]$Port = 18791
)
$ErrorActionPreference = "Stop"
$sidecarPath = (Resolve-Path -LiteralPath $Sidecar).Path
$verify = Join-Path ([IO.Path]::GetTempPath()) ("rynmesh-sidecar-verify-" + [guid]::NewGuid().ToString("N"))
$nodeHome = Join-Path $verify "node"
$network = Join-Path $verify "network"
New-Item -ItemType Directory -Force -Path $verify | Out-Null
$previous = @{}
$names = @("RYNMESH_HOME", "RYNMESH_NETWORK_DIR", "RYNMESH_DESKTOP_MODE", "RYNMESH_PEER_HOST", "RYNMESH_PEER_PORT", "RYNMESH_AUTO_REGISTER")
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
$process = $null
try {
    $env:RYNMESH_HOME = $nodeHome
    $env:RYNMESH_NETWORK_DIR = $network
    $env:RYNMESH_DESKTOP_MODE = "1"
    $env:RYNMESH_PEER_HOST = "127.0.0.1"
    $env:RYNMESH_PEER_PORT = [string]$Port
    $env:RYNMESH_AUTO_REGISTER = "0"
    $process = Start-Process -FilePath $sidecarPath -PassThru -WindowStyle Hidden
    $started = [Diagnostics.Stopwatch]::StartNew()
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        if ($process.HasExited) { throw "sidecar exited early with code $($process.ExitCode)" }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
            if ($health.status -eq "ok" -and $health.peer_id) {
                Write-Output ("SIDECAR_HEALTHY milliseconds={0} peer_id={1}" -f $started.ElapsedMilliseconds, $health.peer_id)
                return
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    throw "sidecar did not become healthy within 30 seconds"
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        $null = $process.WaitForExit(5000)
    }
    if ($process) { $process.Dispose(); $process = $null }
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process") }
    if (Test-Path -LiteralPath $verify) { [IO.Directory]::Delete($verify, $true) }
}
