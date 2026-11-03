param([string]$OutputDirectory = [Environment]::GetFolderPath('Desktop'))
# Read-only recipient diagnostics. Never stop processes, change firewall rules,
# run an uninstaller, or copy credentials, databases, settings or raw app logs.
$ErrorActionPreference = 'Stop'
$report = [ordered]@{schema=1; time=(Get-Date -Format o); checks=[ordered]@{}}
function Check($Name, [scriptblock]$Action) {
    Write-Host "Checking $Name ..."
    try { $report.checks[$Name] = & $Action }
    catch { $report.checks[$Name] = @{error=$_.Exception.Message} }
}
function LocalGet($Path) {
    # Explicitly bypass system proxies for loopback; do not export auth material.
    $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:8791$Path")
    $request.Proxy = $null
    $request.Timeout = 12000
    $response = $request.GetResponse()
    try {
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
}
Check 'windows' {
    Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture
}
Check 'process-query' {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('Ryn.exe','rynmesh-peer.exe','company-service.exe','ryn-full-install.exe')
    } | Select-Object Name,ProcessId,ExecutablePath)
}
Check 'network' {
    @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -ne '127.0.0.1'} |
        Select-Object InterfaceAlias,IPAddress,PrefixLength,AddressState)
}
Check 'network-profile' { @(Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory,IPv4Connectivity) }
Check 'firewall' { @(Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction) }
Check 'installation' {
    $registered = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Ryn' -ErrorAction SilentlyContinue
    $folder = Join-Path $env:LOCALAPPDATA 'Ryn'
    if ($registered.InstallLocation) { $folder = $registered.InstallLocation.Trim('"') }
    $files = foreach ($name in @('Ryn.exe','rynmesh-peer.exe','uninstall.exe')) {
        $file = Join-Path $folder $name
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            $item=Get-Item -LiteralPath $file
            @{name=$name;bytes=$item.Length;sha256=(Get-FileHash -LiteralPath $file).Hash}
        }
    }
    @{directory=$folder;files=@($files)}
}
Check 'local-discovery' {
    $value = LocalGet '/api/local/settings'
    $value | Select-Object peer_http_host,peer_http_port,public_endpoint,lan_discovery,publish_visibility
}
Check 'local-peers' {
    @(LocalGet '/api/local/peers?quick=1' | ForEach-Object {
        $_ | Select-Object id,name,status,endpoint,lan_endpoints
    })
}
Check 'local-services' {
    $value = LocalGet '/api/local/llm/services'
    @{count=@($value.services).Count;services=@($value.services | Select-Object peer_id,node_name,configured,ready,online,local_only,availability)}
}
foreach ($address in @('172.16.8.191','10.10.10.187')) {
    Check "provider-$address" {
        # Addresses observed on the sharing computer for this incident. Public
        # discovery only; no local-control request is sent to another computer.
        $nonce=[guid]::NewGuid().ToString('N')
        $request=[Net.HttpWebRequest]::Create("http://${address}:8791/api/peer/lan-record?nonce=$nonce")
        $request.Proxy=$null; $request.Timeout=5000
        $response=$request.GetResponse()
        try { @{http_status=[int]$response.StatusCode} } finally { $response.Dispose() }
    }
}
Check 'company-health' {
    $request=[Net.HttpWebRequest]::Create('http://127.0.0.1:18140/api/local/runtime/health')
    $request.Proxy=$null; $request.Timeout=5000
    $response=$request.GetResponse()
    try { @{http_status=[int]$response.StatusCode} } finally { $response.Dispose() }
}
Check 'installer-payload' {
    # Keep the failed installer dialog open so NSIS has not removed its temp files.
    $candidates = @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter 'ns*.tmp' -ErrorAction SilentlyContinue |
        Where-Object {Test-Path -LiteralPath (Join-Path $_.FullName 'payload/full-package.json')} |
        Sort-Object LastWriteTime -Descending)
    if (-not $candidates.Count) { return @{available=$false;hint='Keep the failed installer dialog open and run diagnostics again.'} }
    $root=Join-Path $candidates[0].FullName 'payload'
    $manifest=Get-Content -LiteralPath (Join-Path $root 'full-package.json') -Raw | ConvertFrom-Json
    $bad=@(); $count=0
    foreach ($property in $manifest.files.PSObject.Properties) {
        $file=[IO.Path]::GetFullPath((Join-Path $root $property.Name))
        if (-not $file.StartsWith($root.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid manifest path' }
        $count++
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { $bad+=@{file=$property.Name;error='missing'}; continue }
        try {
            if ((Get-FileHash -LiteralPath $file).Hash -ne $property.Value) { $bad+=@{file=$property.Name;error='hash-mismatch'} }
        } catch { $bad+=@{file=$property.Name;error=$_.Exception.Message} }
    }
    @{available=$true;ryn_commit=$manifest.ryn_commit;company_commit=$manifest.company_commit;files_checked=$count;failures=$bad}
}
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) { throw 'Output directory does not exist' }
$output=Join-Path $OutputDirectory ('Ryn-diagnostic-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
$json=$report | ConvertTo-Json -Depth 12
# Redact the user profile name while retaining useful relative installation paths.
if ($env:USERPROFILE) { $json=$json.Replace(($env:USERPROFILE | ConvertTo-Json -Compress).Trim('"'),'%USERPROFILE%') }
[IO.File]::WriteAllText($output,$json,[Text.UTF8Encoding]::new($false))
Write-Host "Report saved: $output"
