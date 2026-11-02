param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$CoreInstaller,
    [Parameter(Mandatory=$true)][string]$CoreCommit,
    [Parameter(Mandatory=$true)][string]$CoreExecutable,
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$WebViewInstaller,
    [Parameter(Mandatory=$true)][string]$Output,
    [string]$MakeNsis = "$env:LOCALAPPDATA/tauri/NSIS/makensis.exe"
)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repo
try {
    if ((git branch --show-current) -ne 'develop') { throw 'Release requires develop' }
    if (git status --porcelain -uno) { throw 'Commit tracked changes before releasing' }
    git merge-base --is-ancestor $CoreCommit HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Core build is not from integrated source' }
    if (git diff --name-only $CoreCommit HEAD -- rynmesh webapp native pyproject.toml) {
        throw 'Core source changed: rebuild the ordinary installer first'
    }
    if ((Get-Item -LiteralPath $WebViewInstaller).Length -lt 50000000) { throw 'Standalone WebView2 installer required' }
    $signature=Get-AuthenticodeSignature -LiteralPath $WebViewInstaller
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Microsoft Corporation,') {
        throw 'Invalid Microsoft WebView2 signature'
    }
    $payloadPath=(Resolve-Path -LiteralPath $Payload).Path
    $manifestPath=Join-Path $payloadPath 'full-package.json'
    $manifest=Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $manifest.ryn_commit=(git rev-parse HEAD)
    [IO.File]::WriteAllText($manifestPath,($manifest|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
    $build=Join-Path $repo '.codex-tmp/full-package-20261103/final-helper'
    & $Python -m PyInstaller --noconfirm --onedir --name ryn-full-install --collect-submodules rynmesh --collect-data rynmesh --collect-data jsonschema_specifications --distpath "$build/dist" --workpath "$build/work" --specpath $build "$PSScriptRoot/full_package_install.py"
    if ($LASTEXITCODE -ne 0) { throw 'Provisioner build failed' }
    $acceptance=Join-Path $build ('acceptance-'+[guid]::NewGuid().ToString('N'))
    & $Python "$repo/qa/full_package_acceptance.py" --payload $payloadPath --helper "$build/dist/ryn-full-install/ryn-full-install.exe" --core $CoreExecutable --work $acceptance
    if ($LASTEXITCODE -ne 0) { throw 'Complete package isolated acceptance failed' }
    $outputPath=[IO.Path]::GetFullPath($Output)
    New-Item -ItemType Directory -Force -Path (Split-Path $outputPath) | Out-Null
    & $MakeNsis /V2 "/DOUTPUT=$outputPath" "/DCORE=$((Resolve-Path -LiteralPath $CoreInstaller).Path)" "/DPAYLOAD=$payloadPath" "/DHELPER=$build/dist/ryn-full-install" "/DWEBVIEW=$((Resolve-Path -LiteralPath $WebViewInstaller).Path)" "$repo/deploy/windows/full-installer.nsi"
    if ($LASTEXITCODE -ne 0) { throw 'Complete installer build failed' }
    $receipt=@{schema=1;ryn_commit=$manifest.ryn_commit;core_commit=$CoreCommit;company_commit=$manifest.company_commit;plugins=$manifest.plugins;installer_sha256=(Get-FileHash -LiteralPath $outputPath).Hash;core_sha256=(Get-FileHash -LiteralPath $CoreInstaller).Hash;webview_sha256=(Get-FileHash -LiteralPath $WebViewInstaller).Hash;bytes=(Get-Item -LiteralPath $outputPath).Length;helper_source_sha256=(Get-FileHash -LiteralPath "$PSScriptRoot/full_package_install.py").Hash;acceptance=(Get-Content "$acceptance/result.json" -Raw|ConvertFrom-Json)}
    [IO.File]::WriteAllText("$outputPath.json",($receipt|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    Write-Output $outputPath
} finally { Pop-Location }
