$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $projectRoot '.runtime\local\services.json'
$harnessPidFile = Join-Path $projectRoot '.runtime\local\harness.json'
if (-not (Test-Path -LiteralPath $pidFile) -and -not (Test-Path -LiteralPath $harnessPidFile)) {
    Write-Host 'No managed Noodle local services are recorded.'
    exit 0
}

if (Test-Path -LiteralPath $harnessPidFile) {
    $harnessState = Get-Content -Raw -LiteralPath $harnessPidFile | ConvertFrom-Json
    foreach ($id in @($harnessState.child, $harnessState.supervisor)) {
        if (-not $id) { continue }
        $process = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -match '^node') {
            Stop-Process -Id $id -Force
            Write-Host "Stopped $($process.ProcessName) PID $id"
        }
    }
    Remove-Item -LiteralPath $harnessPidFile -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $pidFile) {
    $state = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
    foreach ($id in @($state.worker, $state.api)) {
        if (-not $id) { continue }
        $process = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($process -and $process.ProcessName -match '^python') {
            Stop-Process -Id $id -Force
            Write-Host "Stopped $($process.ProcessName) PID $id"
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
