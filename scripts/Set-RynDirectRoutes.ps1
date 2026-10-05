<#
Windows VPN exception routes for a known Ryn peer and STUN servers.
Run Enable/Disable as administrator. Status requires no elevation.
These are destination-based routes, not process-specific VPN exclusions.
No default route, VPN profile, firewall rule, or relay setting is changed.
#>
param(
    [ValidateSet('Enable', 'Disable', 'Status')][string]$Mode = 'Status',
    [string[]]$PeerAddress = @(),
    [string]$InterfaceAlias = '',
    [string[]]$StunHost = @('stun.l.google.com', 'stun.cloudflare.com')
)
$ErrorActionPreference = 'Stop'
$statePath = Join-Path $env:LOCALAPPDATA 'Ryn\direct-routes.json'
$saved = @()
if (Test-Path -LiteralPath $statePath) { $saved = @(Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json) }
if ($Mode -eq 'Status') {
    foreach ($item in $saved) {
        Get-NetRoute -DestinationPrefix $item.prefix -InterfaceIndex $item.interface_index -NextHop $item.next_hop -ErrorAction SilentlyContinue |
            Select-Object DestinationPrefix,InterfaceAlias,NextHop,PolicyStore,ValidLifetime
    }
    return
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal $identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script in an administrator PowerShell.' }
if ($Mode -eq 'Disable') {
    foreach ($item in $saved) {
        foreach ($policy in @('PersistentStore', 'ActiveStore')) {
            Get-NetRoute -DestinationPrefix $item.prefix -InterfaceIndex $item.interface_index -NextHop $item.next_hop -PolicyStore $policy -ErrorAction SilentlyContinue |
                Remove-NetRoute -Confirm:$false
        }
    }
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath }
    Write-Output 'Removed only routes recorded by this script.'
    return
}
if (-not $PeerAddress.Count -or -not $InterfaceAlias) { throw 'Enable requires PeerAddress and the physical InterfaceAlias.' }
$adapter = @(Get-NetAdapter -Name $InterfaceAlias -Physical | Where-Object Status -eq 'Up')
if ($adapter.Count -ne 1) { throw 'Select one connected physical adapter.' }
$index = $adapter[0].ifIndex
$gateway = Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
    Where-Object NextHop -ne '0.0.0.0' | Sort-Object RouteMetric | Select-Object -First 1
if (-not $gateway) { throw 'No IPv4 gateway found on the selected adapter.' }
$addresses = @($PeerAddress)
foreach ($name in $StunHost) {
    $resolved = @([Net.Dns]::GetHostAddresses($name) | Where-Object AddressFamily -eq 'InterNetwork')
    if (-not $resolved.Count) { throw "No IPv4 address for STUN server $name; nothing changed." }
    $addresses += @($resolved | ForEach-Object ToString)
}
# Validate all destinations before changing any route.
$planned = @()
foreach ($address in @($addresses | Sort-Object -Unique)) {
    $ip = [Net.IPAddress]::Parse($address)
    $octets = $ip.GetAddressBytes()
    if ($ip.AddressFamily -ne 'InterNetwork' -or $octets[0] -eq 0 -or $octets[0] -eq 127 -or $octets[0] -ge 224) { throw "Invalid unicast IPv4 peer: $address" }
    $prefix = $ip.ToString() + '/32'
    $existing = @(Get-NetRoute -DestinationPrefix $prefix -PolicyStore PersistentStore -ErrorAction SilentlyContinue)
    if ($existing.Count) {
        $matching = @($existing | Where-Object { $_.InterfaceIndex -eq $index -and $_.NextHop -eq $gateway.NextHop })
        if (-not $matching.Count) { throw "A different persistent route exists for $prefix; left unchanged." }
        Write-Output "Already configured: $prefix"
        continue
    }
    if (@(Get-NetRoute -DestinationPrefix $prefix -PolicyStore ActiveStore -ErrorAction SilentlyContinue).Count) {
        throw "An unmanaged active route exists for $prefix; left unchanged."
    }
    $planned += [pscustomobject]@{prefix=$prefix;interface_index=$index;next_hop=$gateway.NextHop}
}
$created = @()
try {
    foreach ($item in $planned) {
        # Omitting PolicyStore creates both active and persistent entries.
        New-NetRoute -DestinationPrefix $item.prefix -InterfaceIndex $item.interface_index -NextHop $item.next_hop -RouteMetric 5 | Out-Null
        $created += $item
        Write-Output "Direct: $($item.prefix) via $InterfaceAlias"
    }
    # Record ownership for a precise rollback. Preserve routes from earlier runs.
    $all = @($saved) + @($created)
    if ($all.Count) {
        New-Item -ItemType Directory -Path (Split-Path $statePath) -Force | Out-Null
        $temp = $statePath + '.tmp'
        ConvertTo-Json -InputObject $all | Set-Content -LiteralPath $temp -Encoding ascii
        Move-Item -LiteralPath $temp -Destination $statePath -Force
    }
} catch {
    foreach ($item in $created) {
        foreach ($policy in @('PersistentStore', 'ActiveStore')) {
            Get-NetRoute -DestinationPrefix $item.prefix -InterfaceIndex $item.interface_index -NextHop $item.next_hop -PolicyStore $policy -ErrorAction SilentlyContinue |
                Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
    throw
}
Write-Output "Saved. If the peer public IP, STUN DNS, or physical network changes, rerun Enable with the current values. Roll back with -Mode Disable."
