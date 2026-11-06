param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$CoreInstaller,
    [Parameter(Mandatory=$true)][string]$CoreCommit,
    [Parameter(Mandatory=$true)][string]$CoreExecutable,
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$WebViewInstaller,
    [Parameter(Mandatory=$true)][string]$Output,
    [string]$MakeNsis = "$env:LOCALAPPDATA/tauri/NSIS/makensis.exe",
    # lzma (solid, 128 MB window) lets each Office CLI/GUI pair share one copy of their
    # common code: 176 MB -> 37 MB vs 90 MB with zlib. zlib is ~5x faster for test builds.
    [ValidateSet('lzma','zlib')][string]$Compression = 'lzma'
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
    # Freeze the helper with the release venv from build-windows.ps1 (the node's exact
    # dependencies). A developer venv pulled numpy and test tools into the installer.
    # `python -m` puts the repository first on sys.path, so rynmesh still comes from HEAD.
    $releasePython=Join-Path $repo '.codex-tmp/windows-package/venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $releasePython)) { throw 'Release venv missing: run webapp/src-tauri/scripts/build-windows.ps1 first' }
    $helperArgs=@('--noconfirm','--onedir','--name','ryn-full-install','--collect-submodules','rynmesh','--collect-data','rynmesh','--collect-data','jsonschema_specifications','--exclude-module','tkinter','--exclude-module','_tkinter','--exclude-module','numpy')
    $helperExe="$build/dist/ryn-full-install/ryn-full-install.exe"
    $helperStamp="$build/helper-inputs.sha256"
    # Rebuilding the helper took up to 10 minutes; reuse it when nothing it bundles changed.
    $helperInputs=@((Get-FileHash -LiteralPath "$PSScriptRoot/full_package_install.py").Hash,(git rev-parse HEAD:rynmesh),((& $releasePython -m pip freeze --all) -join ';'),(& $releasePython --version),($helperArgs -join ' ')) -join '|'
    $helperKey=[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($helperInputs))).Replace('-','')
    $untrackedSource=git status --porcelain --untracked-files=all -- rynmesh scripts/full_package_install.py
    $helperReused=(-not $untrackedSource) -and (Test-Path -LiteralPath $helperExe) -and (Test-Path -LiteralPath $helperStamp) -and ((Get-Content -LiteralPath $helperStamp -Raw).Trim() -eq $helperKey)
    if ($helperReused) {
        Write-Output 'Installer helper inputs unchanged: reusing the previous build'
    } else {
        if (Test-Path -LiteralPath $helperStamp) { Remove-Item -LiteralPath $helperStamp }
        & $releasePython -m PyInstaller @helperArgs --distpath "$build/dist" --workpath "$build/work" --specpath $build "$PSScriptRoot/full_package_install.py"
        if ($LASTEXITCODE -ne 0) { throw 'Provisioner build failed' }
        if (-not $untrackedSource) { Set-Content -LiteralPath $helperStamp -Value $helperKey -Encoding ascii }
    }
    # Match a normal installation's path depth; the portable OCR engine already
    # contains long upstream file names under its model directory.
    $acceptance=Join-Path $repo ('.codex-tmp/full-qa-'+[guid]::NewGuid().ToString('N').Substring(0,8))
    & $Python "$repo/qa/full_package_acceptance.py" --payload $payloadPath --helper $helperExe --core $CoreExecutable --work $acceptance
    if ($LASTEXITCODE -ne 0) { throw 'Complete package isolated acceptance failed' }
    $acceptanceResult=Get-Content "$acceptance/result.json" -Raw|ConvertFrom-Json
    # Each acceptance run installs the whole payload (~1 GB); keep it only when it fails.
    try { Remove-Item -LiteralPath $acceptance -Recurse -Force } catch { Write-Warning "Could not remove $acceptance`: $_" }
    $outputPath=[IO.Path]::GetFullPath($Output)
    New-Item -ItemType Directory -Force -Path (Split-Path $outputPath) | Out-Null
    # @() keeps a one-item list an array; a splatted string is passed one character at a time.
    $compressor=@(if ($Compression -eq 'zlib') { '/XSetCompressor /FINAL zlib' } else { '/XSetCompressor /FINAL /SOLID lzma'; '/XSetCompressorDictSize 128' })
    & $MakeNsis /V2 @compressor "/DOUTPUT=$outputPath" "/DCORE=$((Resolve-Path -LiteralPath $CoreInstaller).Path)" "/DPAYLOAD=$payloadPath" "/DHELPER=$build/dist/ryn-full-install" "/DWEBVIEW=$((Resolve-Path -LiteralPath $WebViewInstaller).Path)" "$repo/deploy/windows/full-installer.nsi"
    if ($LASTEXITCODE -ne 0) { throw 'Complete installer build failed' }
    $receipt=@{schema=1;ryn_commit=$manifest.ryn_commit;core_commit=$CoreCommit;company_commit=$manifest.company_commit;plugins=$manifest.plugins;installer_sha256=(Get-FileHash -LiteralPath $outputPath).Hash;core_sha256=(Get-FileHash -LiteralPath $CoreInstaller).Hash;webview_sha256=(Get-FileHash -LiteralPath $WebViewInstaller).Hash;bytes=(Get-Item -LiteralPath $outputPath).Length;helper_source_sha256=(Get-FileHash -LiteralPath "$PSScriptRoot/full_package_install.py").Hash;helper_executable_sha256=(Get-FileHash -LiteralPath $helperExe).Hash;helper_reused=$helperReused;compression=$Compression;acceptance=$acceptanceResult}
    [IO.File]::WriteAllText("$outputPath.json",($receipt|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
    Write-Output $outputPath
} finally { Pop-Location }
