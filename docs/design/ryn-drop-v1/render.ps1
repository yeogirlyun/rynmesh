param([string[]]$Names)
$ErrorActionPreference = 'Continue'
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$src = Join-Path $PSScriptRoot 'src'
$out = $PSScriptRoot
$files = Get-ChildItem $src -Filter *.html
if ($Names) { $files = $files | Where-Object { $Names -contains $_.BaseName } }
foreach ($f in $files) {
  $png = Join-Path $out ($f.BaseName + '.png')
  $url = 'file:///' + ($f.FullName -replace '\\','/')
  $tmp = Join-Path $env:TEMP ("edge-shot-" + [guid]::NewGuid())
  & $edge --headless=new --disable-gpu --hide-scrollbars --no-first-run "--user-data-dir=$tmp" --force-device-scale-factor=1.5 --window-size=1440,900 --virtual-time-budget=2000 "--screenshot=$png" $url 2>$null | Out-Null
  Start-Sleep -Milliseconds 300
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  Write-Output "$($f.BaseName) -> $(Test-Path $png)"
}
