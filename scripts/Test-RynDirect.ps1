param(
    [string]$PeerAddress = '',
    [int]$PeerPort = 0,
    [string]$Token = '',
    [string]$BindAddress = '0.0.0.0',
    [int]$Seconds = 600,
    [string]$PeerFile = '',
    [string[]]$StunServers = @('stun.l.google.com:19302', 'stun.cloudflare.com:3478')
)

# Standalone IPv4 UDP diagnostic. No Ryn, admin access, relay, or firewall edits.
# One socket is kept open for STUN, outgoing probes, and incoming replies.
$ErrorActionPreference = 'Stop'
if ($Token -notmatch '^[a-zA-Z0-9-]{8,80}$') { throw 'Supply the shared test Token.' }
if ($Seconds -lt 1 -or $Seconds -gt 1800) { throw 'Seconds must be 1..1800.' }
$udp = New-Object System.Net.Sockets.UdpClient
$udp.Client.Bind((New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Parse($BindAddress)), 0))
$utf8 = [System.Text.Encoding]::UTF8
$ping = $utf8.GetBytes("RYN-PROBE/$Token/PING")
$pong = $utf8.GetBytes("RYN-PROBE/$Token/PONG")
$pending = @{}
$mappings = @{}
$targets = @{}
$sent = 0; $receivedPing = 0; $receivedPong = 0
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$nextStun = 0; $nextPing = 0; $nextStatus = 15; $nextFile = 0
$lastSocketError = ''

function Add-ProbeTarget([string]$Address, [int]$Port) {
    if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid peer port.' }
    $endpoint = New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Parse($Address)), $Port
    $targets[$endpoint.ToString()] = $endpoint
}
if ($PeerAddress) { Add-ProbeTarget $PeerAddress $PeerPort }
$servers = @()
try {
    foreach ($server in $StunServers) {
        $parts = $server.Split(':')
        try {
            $ip = [System.Net.Dns]::GetHostAddresses($parts[0]) | Where-Object { $_.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
            if ($null -eq $ip) { throw 'No IPv4 address' }
            $servers += New-Object System.Net.IPEndPoint $ip, ([int]$parts[1])
        } catch { Write-Output "STUN_DNS_FAILED $server $($_.Exception.Message)" }
    }
    Write-Output "START local=$($udp.Client.LocalEndPoint) duration=${Seconds}s relay=false"
    Write-Output 'Keep this window open. Send the PUBLIC lines to the other operator immediately.'
    while ($watch.Elapsed.TotalSeconds -lt $Seconds) {
        $now = $watch.Elapsed.TotalSeconds
        if ($PeerFile -and $now -ge $nextFile) {
            if (Test-Path -LiteralPath $PeerFile) {
                $peers = Get-Content -LiteralPath $PeerFile -Raw | ConvertFrom-Json
                foreach ($peer in $peers) { Add-ProbeTarget $peer.address ([int]$peer.port) }
            }
            $nextFile = $now + 1
        }
        if ($now -ge $nextStun) {
            $pending.Clear()
            foreach ($server in $servers) {
                [byte[]]$tx = [guid]::NewGuid().ToByteArray()[0..11]
                [byte[]]$request = @(0,1,0,0,0x21,0x12,0xa4,0x42) + $tx
                $pending[[BitConverter]::ToString($tx)] = $server.ToString()
                try { [void]$udp.Send($request, $request.Length, $server) } catch { $lastSocketError = $_.Exception.Message }
            }
            $nextStun = $now + 15
        }
        if ($now -ge $nextPing) {
            foreach ($target in @($targets.Values)) {
                try { [void]$udp.Send($ping, $ping.Length, $target); $sent++ } catch { $lastSocketError = $_.Exception.Message }
            }
            $nextPing = $now + 0.25
        }
        while ($udp.Available -gt 0) {
            $remote = New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Any), 0
            try { $data = $udp.Receive([ref]$remote) } catch { $lastSocketError = $_.Exception.Message; break }
            if ($data.Length -ge 20 -and $data[0] -eq 1 -and $data[1] -eq 1 -and [BitConverter]::ToString($data[4..7]) -eq '21-12-A4-42') {
                $txkey = [BitConverter]::ToString($data[8..19])
                if ($pending[$txkey] -ne $remote.ToString()) { continue }
                $end = 20 + ([int]$data[2] * 256 + [int]$data[3])
                if ($end -gt $data.Length) { continue }
                for ($i = 20; $i + 4 -le $end;) {
                    $type = [int]$data[$i] * 256 + [int]$data[$i+1]
                    $length = [int]$data[$i+2] * 256 + [int]$data[$i+3]
                    $v = $i + 4
                    if ($v + $length -gt $end) { break }
                    if ($type -eq 32 -and $length -eq 8 -and $data[$v+1] -eq 1) {
                        $port = ([int]$data[$v+2] * 256 + [int]$data[$v+3]) -bxor 0x2112
                        [byte[]]$addr = 0,0,0,0
                        for ($j = 0; $j -lt 4; $j++) { $addr[$j] = $data[$v+4+$j] -bxor $data[4+$j] }
                        $mapped = (New-Object System.Net.IPAddress (,$addr)).ToString() + ':' + $port
                        if ($mappings[$remote.ToString()] -ne $mapped) { Write-Output "PUBLIC $mapped via=$remote" }
                        $mappings[$remote.ToString()] = $mapped
                    }
                    $i = $v + $length + ((4 - ($length % 4)) % 4)
                }
                continue
            }
            $message = $utf8.GetString($data)
            if ($message -eq $utf8.GetString($ping)) {
                $receivedPing++
                if ($receivedPing -eq 1) { Write-Output "DIRECT_INBOUND from=$remote" }
                Add-ProbeTarget $remote.Address.ToString() $remote.Port
                try { [void]$udp.Send($pong, $pong.Length, $remote) } catch { $lastSocketError = $_.Exception.Message }
            } elseif ($message -eq $utf8.GetString($pong)) {
                if ($receivedPong -eq 0) { Write-Output "DIRECT_ROUND_TRIP_OK from=$remote relay=false" }
                $receivedPong++
            }
        }
        if ($now -ge $nextStatus) {
            Write-Output "STATUS elapsed=$([int]$now) sent=$sent incoming=$receivedPing replies=$receivedPong publicMappings=$($mappings.Count)"
            $nextStatus = $now + 15
        }
        Start-Sleep -Milliseconds 25
    }
    [pscustomobject]@{ direct_round_trip = ($receivedPong -gt 0); sent = $sent; incoming = $receivedPing; replies = $receivedPong; public_mappings = $mappings; last_socket_error = $lastSocketError; relay_used = $false } | ConvertTo-Json -Depth 4
} finally { $udp.Close() }
