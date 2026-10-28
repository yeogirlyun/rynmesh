param(
    [string]$Python = "python",
    [string]$TargetTriple = "x86_64-pc-windows-msvc"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$tauri = Join-Path $root "webapp\src-tauri"
$output = Join-Path $tauri "binaries"
$build = Join-Path ([IO.Path]::GetTempPath()) ("rynmesh-sidecar-" + [guid]::NewGuid().ToString("N"))
$dist = Join-Path $build "dist"
$work = Join-Path $build "work"
$spec = Join-Path $build "spec"

try {
    New-Item -ItemType Directory -Force -Path $output, $dist, $work, $spec | Out-Null
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller is required: $Python -m pip install pyinstaller" }
    & $Python -m PyInstaller --onefile --noconfirm --clean `
        --name rynmesh-peer --paths $root `
        --collect-submodules uvicorn --collect-submodules rynmesh --collect-submodules anyio `
        --hidden-import uvicorn.lifespan.on --hidden-import uvicorn.loops.asyncio `
        --hidden-import uvicorn.protocols.http.h11_impl `
        --distpath $dist --workpath $work --specpath $spec `
        (Join-Path $tauri "sidecar\rynmesh_peer_entry.py")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    $destination = Join-Path $output "rynmesh-peer-$TargetTriple.exe"
    Copy-Item -LiteralPath (Join-Path $dist "rynmesh-peer.exe") -Destination $destination -Force
    Write-Output "SIDECAR_BUILT $destination"
} finally {
    if (Test-Path -LiteralPath $build) { [IO.Directory]::Delete($build, $true) }
}
