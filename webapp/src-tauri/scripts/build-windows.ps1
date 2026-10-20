param([switch]$SkipSidecar)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$tauriRoot = Join-Path $repoRoot 'webapp/src-tauri'
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
$manifestTool = Join-Path $PSScriptRoot 'build-computer-use-manifest.py'

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

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
    & $pythonExe -m pip install "${repoRoot}[documents]" 'pyinstaller==6.22.3'
    Assert-Success 'Install node dependencies'
    & $pythonExe -c 'import pypdf; import rynmesh.services.document_extract_child'
    Assert-Success 'Verify bundled document extractor'
    # Native P2P transport (hole punching + QUIC in Rust); maturin builds it with cargo.
    & $pythonExe -m pip install (Join-Path $repoRoot 'native/ryn_transport')
    Assert-Success 'Build native P2P transport'
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
    npm ci
    Assert-Success 'Install frontend dependencies'
    npm run tauri -- build --target x86_64-pc-windows-msvc --config src-tauri/tauri.windows.conf.json
    Assert-Success 'Build Windows installer'
} finally { Pop-Location }

Write-Host "Installer: $tauriRoot/target/x86_64-pc-windows-msvc/release/bundle/nsis"
