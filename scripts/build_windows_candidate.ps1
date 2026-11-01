param(
  [Parameter(Mandatory=$true)][string]$Python,
  [Parameter(Mandatory=$true)][string]$Node,
  [Parameter(Mandatory=$true)][string]$InnoCompiler
)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
Push-Location $workspace
try {
  $env:PATH = (Split-Path $Node -Parent) + ';' + $env:PATH
  Push-Location webapp
  try {
    & $Node node_modules/typescript/bin/tsc -b
    if ($LASTEXITCODE) { throw 'TypeScript build failed' }
    & $Node node_modules/vite/bin/vite.js build
    if ($LASTEXITCODE) { throw 'Web build failed' }
  } finally { Pop-Location }
  $candidate = Join-Path $workspace 'release/windows'
  $bundle = Join-Path $workspace 'build/windows-dist/Ryn'
  & $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name Ryn --paths $workspace --collect-submodules rynmesh --collect-submodules uvicorn --collect-submodules anyio --collect-all pypdf --copy-metadata rynmesh --add-data "${workspace}/webapp/dist:rynmesh/webui" --add-data "${workspace}/rynmesh/friends/invitation.html:rynmesh/friends" --add-data "${workspace}/rynmesh/friends/invitation.js:rynmesh/friends" --distpath build/windows-dist --workpath build/pyinstaller --specpath build scripts/windows_entry.py
  if ($LASTEXITCODE) { throw 'Frozen application build failed' }
  & $InnoCompiler "/DBundleDir=$bundle" "/DOutputDir=$candidate" deploy/windows/ryn.iss
  if ($LASTEXITCODE) { throw 'Windows installer build failed' }
  $revision = git rev-parse HEAD
  $dirty = [bool](git status --porcelain --untracked-files=no)
  $installer = Get-Item -LiteralPath (Join-Path $candidate 'Ryn-Setup.exe')
  @{commit=$revision; tracked_changes=$dirty; sha256=(Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash; bytes=$installer.Length; platform='Windows 10/11 x64'; model_included=$false; signed=$false} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $candidate 'candidate.json') -Encoding utf8
} finally { Pop-Location }
