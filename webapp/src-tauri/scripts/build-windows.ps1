param([switch]$SkipSidecar)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$tauriRoot = Join-Path $repoRoot 'webapp/src-tauri'
$buildRoot = Join-Path $repoRoot '.codex-tmp/windows-package'
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

function Assert-Success([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

if (-not $SkipSidecar) {
    $venvRoot = Join-Path $buildRoot 'venv'
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
    & $pythonExe -m PyInstaller --onefile --noconfirm --clean --name rynmesh-peer `
        --collect-submodules uvicorn --collect-submodules rynmesh --collect-submodules anyio --collect-submodules aioquic --collect-submodules pypdf `
        --collect-data rynmesh --copy-metadata rynmesh `
        --collect-data jsonschema_specifications --hidden-import ryn_transport `
        --distpath (Join-Path $buildRoot 'dist') --workpath (Join-Path $buildRoot 'work') `
        --specpath $buildRoot (Join-Path $tauriRoot 'sidecar/rynmesh_peer_entry.py')
    Assert-Success 'Build bundled node'
    New-Item -ItemType Directory -Force -Path (Join-Path $tauriRoot 'binaries') | Out-Null
    Copy-Item -LiteralPath (Join-Path $buildRoot 'dist/rynmesh-peer.exe') `
        -Destination (Join-Path $tauriRoot 'binaries/rynmesh-peer-x86_64-pc-windows-msvc.exe')
}

Push-Location (Join-Path $repoRoot 'webapp')
try {
    npm ci
    Assert-Success 'Install frontend dependencies'
    npm run tauri -- build --target x86_64-pc-windows-msvc --config src-tauri/tauri.windows.conf.json
    Assert-Success 'Build Windows installer'
} finally { Pop-Location }

Write-Host "Installer: $tauriRoot/target/x86_64-pc-windows-msvc/release/bundle/nsis"
