$ErrorActionPreference = 'Stop'
$repoRoot = 'D:\code\rynmesh'
$runRoot = Join-Path $repoRoot 'docs/acceptance/ryn-agent-delivery/final-candidate-20261011T171558154'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:CARGO_HOME = Join-Path $repoRoot '.codex-tmp/windows-toolchain/cargo'
$env:RUSTUP_HOME = Join-Path $repoRoot '.codex-tmp/windows-toolchain/rustup'
$env:RUSTUP_TOOLCHAIN = '1.98.1-x86_64-pc-windows-msvc'
$env:PATH = "C:\nvm4w\nodejs;$env:CARGO_HOME\bin;$env:PATH"
$readiness = Get-Content -LiteralPath (Join-Path $repoRoot 'docs/acceptance/ryn-agent-delivery/readiness-execution-20261011.json') -Raw | ConvertFrom-Json
$testPaths = @($readiness.exact_existing_commands | Where-Object { $_.order -in @(2,3) } | ForEach-Object { $_.command -split ' ' | Where-Object { $_ -like 'tests/*.py' } })
$testPaths += @('tests/test_agent_checker_references.py','tests/test_agent_compact_efficiency.py','tests/test_agent_long_resource_context.py','tests/test_agent_read_retry_limit.py','tests/test_agent_tool_menu.py')
$testPaths = @($testPaths | Select-Object -Unique)
function Save-SourceManifest([string]$FileName) {
    $sourceRoots = @('rynmesh','webapp/src','webapp/public','webapp/src-tauri/src','webapp/src-tauri/scripts','webapp/src-tauri/sidecar','webapp/src-tauri/icons','webapp/src-tauri/capabilities')
    $files = @(foreach ($root in $sourceRoots) {
        $path = Join-Path $repoRoot $root
        if (Test-Path -LiteralPath $path) {
            Get-ChildItem -LiteralPath $path -Recurse -File | Where-Object {
                $_.FullName -notmatch '[/\\](heldout|__pycache__|node_modules|target)[/\\]' -and $_.Name -ne 'build_cases.py' -and $_.Extension -notin @('.pyc','.pyo') -and $_.Name -notmatch '^\.env|identity\.ed25519|messaging\.x25519|control_token'
            }
        }
    })
    $fixedPaths = @('pyproject.toml','webapp/package.json','webapp/package-lock.json','webapp/index.html','webapp/tsconfig.json','webapp/tsconfig.app.json','webapp/tsconfig.node.json','webapp/vite.config.ts','webapp/vitest.config.ts','webapp/src-tauri/Cargo.toml','webapp/src-tauri/Cargo.lock','webapp/src-tauri/build.rs','webapp/src-tauri/tauri.conf.json','webapp/src-tauri/tauri.windows.conf.json','tests/conftest.py') + $testPaths
    $files += @(foreach ($relativePath in $fixedPaths) { $path = Join-Path $repoRoot $relativePath; if (Test-Path -LiteralPath $path -PathType Leaf) { Get-Item -LiteralPath $path } })
    $manifest = @($files | Sort-Object FullName -Unique | ForEach-Object {
        [ordered]@{ path = $_.FullName.Substring($repoRoot.Length + 1).Replace('\','/'); sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash; bytes = $_.Length }
    })
    ConvertTo-Json -InputObject $manifest -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot $FileName) -Encoding UTF8
    return $manifest
}
function Save-Changes($Before,$After,[string]$FileName) {
    $beforeMap = @{}; foreach ($item in $Before) { $beforeMap[$item.path] = $item.sha256 }
    $afterMap = @{}; foreach ($item in $After) { $afterMap[$item.path] = $item.sha256 }
    $changes = @(foreach ($path in @($beforeMap.Keys + $afterMap.Keys) | Sort-Object -Unique) {
        if ($beforeMap[$path] -ne $afterMap[$path]) { [ordered]@{ path = $path; before = $beforeMap[$path]; after = $afterMap[$path] } }
    })
    ConvertTo-Json -InputObject $changes -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot $FileName) -Encoding UTF8
    return $changes
}
$beforeSource = @(Save-SourceManifest 'source-before.json')
git -C $repoRoot status --short | Set-Content -LiteralPath (Join-Path $runRoot 'git-status-before.txt') -Encoding UTF8
$artifactPaths = @('webapp/src-tauri/target/x86_64-pc-windows-msvc/release/Ryn.exe','webapp/src-tauri/target/x86_64-pc-windows-msvc/release/rynmesh-peer.exe','webapp/src-tauri/binaries/rynmesh-peer-x86_64-pc-windows-msvc.exe','webapp/src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/Ryn_0.7.0_x64-setup.exe')
$backupRoot = Join-Path $repoRoot '.codex-tmp/final-candidate-backup-20261011T171558154'
if (Test-Path -LiteralPath $backupRoot) { throw 'Backup exists; refusing overwrite.' }
New-Item -ItemType Directory -Path $backupRoot | Out-Null
$backups = @(foreach ($relativePath in $artifactPaths) {
    $sourcePath = Join-Path $repoRoot $relativePath
    if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
        $destination = Join-Path $backupRoot $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        $hash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
        Copy-Item -LiteralPath $sourcePath -Destination $destination
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne $hash) { throw 'Backup hash mismatch.' }
        [ordered]@{ path=$relativePath; sha256=$hash; backup=$destination }
    }
})
ConvertTo-Json -InputObject $backups -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot 'backup-manifest.json') -Encoding UTF8
$steps = [Collections.Generic.List[object]]::new()
function Invoke-Step([string]$Name,[string]$Cwd,[string]$Command) {
    $logPath = Join-Path $runRoot "$Name.log"
    $started = (Get-Date).ToUniversalTime().ToString('o')
    Write-Host "START $Name $started"
    Push-Location $Cwd
    try { & cmd.exe /d /s /c "$Command > `"$logPath`" 2>&1"; $code = $LASTEXITCODE } finally { Pop-Location }
    $finished = (Get-Date).ToUniversalTime().ToString('o')
    $steps.Add([ordered]@{ name=$Name; cwd=$Cwd; command=$Command; exit_code=$code; started_utc=$started; finished_utc=$finished; log=$logPath })
    ConvertTo-Json -InputObject @($steps.ToArray()) -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot 'steps.json') -Encoding UTF8
    Write-Host "FINISH $Name EXIT=$code $finished"
}
$backendCommand = '.venv\Scripts\python.exe -m pytest ' + ($testPaths -join ' ') + ' -q -p no:cacheprovider --tb=short'
Invoke-Step 'backend-regression' $repoRoot $backendCommand
$afterBackend = @(Save-SourceManifest 'source-after-backend.json')
$backendChanges = @(Save-Changes $beforeSource $afterBackend 'source-changes-backend.json')
Write-Host "BACKEND_DONE SOURCE_CHANGES=$($backendChanges.Count)"
Invoke-Step 'frontend-test' (Join-Path $repoRoot 'webapp') 'npm test -- --maxWorkers=4'
Invoke-Step 'frontend-lint' (Join-Path $repoRoot 'webapp') 'npm run lint'
Invoke-Step 'frontend-build' (Join-Path $repoRoot 'webapp') 'npm run build'
$vsDevPath = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat'
$buildScript = Join-Path $repoRoot 'webapp/src-tauri/scripts/build-windows.ps1'
$buildCommand = 'call "{0}" -arch=x64 -host_arch=x64 && powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{1}"' -f $vsDevPath,$buildScript
Invoke-Step 'windows-full-build' $repoRoot $buildCommand
$afterSource = @(Save-SourceManifest 'source-after.json')
$sourceChanges = @(Save-Changes $beforeSource $afterSource 'source-changes.json')
$outputs = @(foreach ($relativePath in $artifactPaths) {
    $path = Join-Path $repoRoot $relativePath
    if (Test-Path -LiteralPath $path -PathType Leaf) { $item = Get-Item -LiteralPath $path; [ordered]@{ path=$path; sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash; bytes=$item.Length; last_write_utc=$item.LastWriteTimeUtc.ToString('o') } }
})
ConvertTo-Json -InputObject $outputs -Depth 5 | Set-Content -LiteralPath (Join-Path $runRoot 'output-manifest.json') -Encoding UTF8
git -C $repoRoot status --short | Set-Content -LiteralPath (Join-Path $runRoot 'git-status-after.txt') -Encoding UTF8
[ordered]@{kind='frozen-candidate-offline-regression-and-windows-build'; formal_trials=0; formal_pass=$false; native_ui_validated=$false; installation_performed=$false; backend_test_files=$testPaths; source_files_before=$beforeSource.Count; source_files_after=$afterSource.Count; source_changes=$sourceChanges.Count; steps=@($steps.ToArray()); outputs=$outputs; limitations=@('Offline deterministic checks and complete installer build only; formal business acceptance remains unexecuted.','Reference 12000-byte checker ceiling remains unverified.','No installation, deployment, native UI or model/remote business calls performed.')} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runRoot 'result.json') -Encoding UTF8
Write-Host "FINAL_DONE SOURCE_CHANGES=$($sourceChanges.Count) RUN_ROOT=$runRoot"
