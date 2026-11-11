# -CleanInstall forces `npm ci` and the native transport rebuild even when their inputs are unchanged.
param([switch]$SkipSidecar, [switch]$Updater, [switch]$CleanInstall)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$tauriRoot = Join-Path $repoRoot 'webapp/src-tauri'
if ([string]::IsNullOrWhiteSpace($env:RYN_DESKTOP_UPDATE_PUBLIC_KEY)) {
    $env:RYN_DESKTOP_UPDATE_PUBLIC_KEY = (Get-Content -LiteralPath (Join-Path $tauriRoot 'desktop-update.pub') -Raw).Trim()
}
if ($Updater -and -not $env:TAURI_SIGNING_PRIVATE_KEY) {
    throw 'Signed update packages require TAURI_SIGNING_PRIVATE_KEY; ordinary builds retain the official verification public key without it.'
}
$buildRoot = Join-Path $repoRoot '.codex-tmp/windows-package'
$venvRoot = Join-Path $buildRoot 'venv'
$nativeCrateManifest = Join-Path $repoRoot 'native/ryn_computer_use/Cargo.toml'
$nativeTargetDir = Join-Path $buildRoot 'native-target'
$nativeReleaseExe = Join-Path $nativeTargetDir 'x86_64-pc-windows-msvc/release/ryn_computer_use.exe'
$stagingDir = Join-Path $buildRoot 'staging'
$stagingManifest = Join-Path $stagingDir 'computer-use-manifest.json'
$receiptPath = Join-Path $stagingDir 'computer-release-receipt.json'
$distDir = Join-Path $buildRoot 'dist'
$workDir = Join-Path $buildRoot 'work'
$distPeerExe = Join-Path $distDir 'rynmesh-peer.exe'
$binariesDir = Join-Path $tauriRoot 'binaries'
$stagedPeerExe = Join-Path $binariesDir 'rynmesh-peer-x86_64-pc-windows-msvc.exe'
$stagedNativeExe = Join-Path $binariesDir 'ryn_computer_use-x86_64-pc-windows-msvc.exe'
$tauriWindowsConf = Join-Path $tauriRoot 'tauri.windows.conf.json'
$tauriPrebuiltConf = Join-Path $tauriRoot 'tauri.prebuilt-frontend.conf.json'
$manifestTool = Join-Path $PSScriptRoot 'build-computer-use-manifest.py'

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

# `npm ci` deletes node_modules and reinstalls everything (1-3 minutes per build).
# Skip it when the installed tree came from the same lockfile, npm and node.
function Install-NodePackages([string]$Project, [string[]]$NpmArgs, [string]$Step) {
    $lock = Join-Path $Project 'package-lock.json'
    $stamp = Join-Path $Project 'node_modules/.ryn-npm-ci.stamp'
    $key = @((Get-FileHash -LiteralPath $lock -Algorithm SHA256).Hash, (node --version), (npm.cmd --version), ($NpmArgs -join ' ')) -join '|'
    if (-not $CleanInstall -and (Test-Path -LiteralPath $stamp) -and
        (Get-Content -LiteralPath $stamp -Raw).Trim() -eq $key) {
        Write-Host "$Step`: lockfile unchanged, reusing $Project/node_modules (-CleanInstall to force)"
        return
    }
    npm.cmd ci --prefix $Project @NpmArgs
    Assert-Success $Step
    Set-Content -LiteralPath $stamp -Value $key -Encoding ascii
}

Install-NodePackages (Join-Path $repoRoot 'webapp') @() 'Install frontend dependencies'
Push-Location (Join-Path $repoRoot 'webapp')
try {
    npm.cmd run build
    Assert-Success 'Build frontend before freezing bundled node'
} finally { Pop-Location }

if (-not $SkipSidecar) {
    New-Item -ItemType Directory -Force -Path $nativeTargetDir | Out-Null
    $prevCargoTargetDir = $env:CARGO_TARGET_DIR
    try {
        $env:CARGO_TARGET_DIR = $nativeTargetDir
        cargo build --manifest-path $nativeCrateManifest --release --locked --target x86_64-pc-windows-msvc --bin ryn_computer_use
        Assert-Success 'Build native computer-use worker (x86_64-pc-windows-msvc)'
    } finally {
        if ($null -eq $prevCargoTargetDir) {
            Remove-Item Env:CARGO_TARGET_DIR -ErrorAction SilentlyContinue
        } else {
            $env:CARGO_TARGET_DIR = $prevCargoTargetDir
        }
    }

    python -m venv $venvRoot
    Assert-Success 'Create Python build environment'
    $pythonExe = Join-Path $venvRoot 'Scripts/python.exe'
    Install-NodePackages (Join-Path $repoRoot 'rynmesh/agent_runtime/chrome_runtime') @('--omit=dev') 'Install Chrome runtime dependencies'
    # setuptools reuses <repo>/build/lib for every wheel and never deletes files that are
    # gone from the source tree, so stale web UI bundles kept shipping inside the node.
    $staleWheelLib = Join-Path $repoRoot 'build/lib'
    if (Test-Path -LiteralPath $staleWheelLib) { Remove-Item -LiteralPath $staleWheelLib -Recurse -Force }
    $staleWheelStage = Join-Path $repoRoot 'build/bdist.win-amd64'
    if (Test-Path -LiteralPath $staleWheelStage) { Remove-Item -LiteralPath $staleWheelStage -Recurse -Force }
    & $pythonExe -m pip install "${repoRoot}[documents]" 'pyinstaller==6.22.3'
    Assert-Success 'Install node dependencies'
    & $pythonExe -c 'import pypdf; import rynmesh.services.document_extract_child'
    Assert-Success 'Verify bundled document extractor'
    # Native P2P transport (hole punching + QUIC in Rust); maturin builds it with cargo.
    # Its source rarely changes, so reuse the installed build while the source hash matches.
    $transportSource = Join-Path $repoRoot 'native/ryn_transport'
    $transportStamp = Join-Path $buildRoot 'ryn-transport-source.sha256'
    $transportKey = & $pythonExe -c 'import hashlib, pathlib, sys; root = pathlib.Path(sys.argv[1]); h = hashlib.sha256(); [h.update(p.relative_to(root).as_posix().encode() + bytes(1) + p.read_bytes()) for p in sorted(root.rglob(''*'')) if p.is_file() and not {''target'', ''__pycache__''} & set(p.relative_to(root).parts)]; print(h.hexdigest())' $transportSource
    Assert-Success 'Hash native P2P transport source'
    $transportCurrent = (-not $CleanInstall) -and (Test-Path -LiteralPath $transportStamp) -and ((Get-Content -LiteralPath $transportStamp -Raw).Trim() -eq $transportKey)
    if ($transportCurrent) {
        & $pythonExe -c 'import ryn_transport'
        $transportCurrent = ($LASTEXITCODE -eq 0)
    }
    if ($transportCurrent) {
        Write-Host 'Native P2P transport source unchanged: reusing the installed build'
    } else {
        & $pythonExe -m pip install $transportSource
        Assert-Success 'Build native P2P transport'
        Set-Content -LiteralPath $transportStamp -Value $transportKey -Encoding ascii
    }
    & $pythonExe -c 'import ryn_transport, rynmesh.native_transport as n; assert n.enabled()'
    Assert-Success 'Verify native P2P transport'

    # Generate deterministic computer-use-manifest.json from final native exe and stage into isolated build venv package
    & $pythonExe $manifestTool stage-manifest `
        --repo-root $repoRoot `
        --build-root $buildRoot `
        --venv-root $venvRoot `
        --native-target-dir $nativeTargetDir `
        --native-exe $nativeReleaseExe `
        --staging-manifest $stagingManifest
    Assert-Success 'Stage authoritative computer-use manifest into isolated build venv'

    # Run PyInstaller from $buildRoot cwd so the source repository checkout cannot shadow the venv-installed rynmesh package
    Push-Location $buildRoot
    try {
        & $pythonExe -m PyInstaller --onefile --noconfirm --clean --name rynmesh-peer `
            --collect-submodules uvicorn --collect-submodules rynmesh --collect-submodules anyio --collect-submodules aioquic --collect-submodules pypdf `
            --collect-data rynmesh --copy-metadata rynmesh `
            --collect-data jsonschema_specifications --hidden-import ryn_transport `
            --exclude-module tkinter --exclude-module _tkinter `
            --add-data "${repoRoot}/webapp/dist;rynmesh/webui" `
            --distpath $distDir --workpath $workDir `
            --specpath $buildRoot (Join-Path $tauriRoot 'sidecar/rynmesh_peer_entry.py')
        Assert-Success 'Build bundled node'
    } finally { Pop-Location }

    New-Item -ItemType Directory -Force -Path $binariesDir | Out-Null
    Copy-Item -LiteralPath $distPeerExe -Destination $stagedPeerExe -Force
    Copy-Item -LiteralPath $nativeReleaseExe -Destination $stagedNativeExe -Force

    # Verify single embedded manifest in PyInstaller TOC and write external build receipt
    & $pythonExe $manifestTool write-receipt `
        --repo-root $repoRoot `
        --build-root $buildRoot `
        --venv-root $venvRoot `
        --work-dir $workDir `
        --native-target-dir $nativeTargetDir `
        --native-exe $nativeReleaseExe `
        --staged-native-exe $stagedNativeExe `
        --dist-peer-exe $distPeerExe `
        --staged-peer-exe $stagedPeerExe `
        --staging-manifest $stagingManifest `
        --tauri-windows-conf $tauriWindowsConf `
        --receipt-out $receiptPath
    Assert-Success 'Write and verify computer-use release receipt'
} else {
    $venvPython = Join-Path $venvRoot 'Scripts/python.exe'
    $verifyPython = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
    & $verifyPython $manifestTool verify-skip-sidecar `
        --repo-root $repoRoot `
        --build-root $buildRoot `
        --venv-root $venvRoot `
        --work-dir $workDir `
        --native-target-dir $nativeTargetDir `
        --native-exe $nativeReleaseExe `
        --staged-native-exe $stagedNativeExe `
        --dist-peer-exe $distPeerExe `
        --staged-peer-exe $stagedPeerExe `
        --staging-manifest $stagingManifest `
        --tauri-windows-conf $tauriWindowsConf `
        --receipt-path $receiptPath
    Assert-Success 'Verify existing sidecar build receipt and native/peer pair (-SkipSidecar)'
}

Push-Location (Join-Path $repoRoot 'webapp')
try {
    $updateArgs = @()
    if ($Updater) {
        $updateArgs = @('--config', 'src-tauri/tauri.updater.conf.json')
    }
    # Tauri embeds the web UI into the app crate, so every release recompiled it from
    # scratch (2-5 minutes) even without Rust changes; incremental reuse cuts that to under a minute.
    $prevCargoIncremental = $env:CARGO_INCREMENTAL
    $env:CARGO_INCREMENTAL = '1'
    try {
        # The frontend was built above; the prebuilt config stops Tauri from building it again.
        npm run tauri -- build --target x86_64-pc-windows-msvc --config src-tauri/tauri.windows.conf.json --config $tauriPrebuiltConf @updateArgs
        Assert-Success 'Build Windows installer'
    } finally {
        if ($null -eq $prevCargoIncremental) {
            Remove-Item Env:CARGO_INCREMENTAL -ErrorAction SilentlyContinue
        } else {
            $env:CARGO_INCREMENTAL = $prevCargoIncremental
        }
    }
} finally { Pop-Location }

Write-Host "Installer: $tauriRoot/target/x86_64-pc-windows-msvc/release/bundle/nsis"
