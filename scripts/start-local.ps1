param(
    [string]$PythonExecutable = $env:NOODLE_PYTHON,
    [int]$ApiPort = 8000,
    [int]$HarnessPort = 0
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$serviceSource = Join-Path $projectRoot 'services\noodle\src'
$runtimeDir = Join-Path $projectRoot '.runtime\local'
$pidFile = Join-Path $runtimeDir 'services.json'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

# npm does not load .env before invoking this PowerShell script. Resolve the
# Harness port here so the preflight check and the child DSH process use the
# same value. An explicit -HarnessPort still takes precedence.
if ($HarnessPort -le 0) {
    $configuredHarnessPort = $env:NOODLE_DSH_WEB_PORT
    if (-not $configuredHarnessPort) {
        $configuredHarnessPort = $env:DSH_WEB_PORT
    }
    $envFile = Join-Path $projectRoot '.env'
    if (-not $configuredHarnessPort -and (Test-Path -LiteralPath $envFile)) {
        $configured = Get-Content -LiteralPath $envFile | Where-Object {
            $_ -match '^\s*NOODLE_DSH_WEB_PORT\s*='
        } | Select-Object -Last 1
        if ($configured) {
            $configuredHarnessPort = ($configured -split '=', 2)[1].Trim().Trim('"').Trim("'")
        }
    }
    if ($configuredHarnessPort) {
        $parsedHarnessPort = 0
        if (-not [int]::TryParse($configuredHarnessPort, [ref]$parsedHarnessPort) -or
            $parsedHarnessPort -lt 1 -or $parsedHarnessPort -gt 65535) {
            throw "Invalid NOODLE_DSH_WEB_PORT in environment or .env: $configuredHarnessPort"
        }
        $HarnessPort = $parsedHarnessPort
    } else {
        $HarnessPort = 3080
    }
}
$env:DSH_WEB_PORT = "$HarnessPort"
$env:NOODLE_HARNESS_WEB_URL = "http://127.0.0.1:$HarnessPort"

if (-not $PythonExecutable) {
    $envFile = Join-Path $projectRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        $configured = Get-Content -LiteralPath $envFile | Where-Object {
            $_ -match '^\s*NOODLE_PYTHON\s*='
        } | Select-Object -Last 1
        if ($configured) {
            $PythonExecutable = ($configured -split '=', 2)[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $PythonExecutable) {
    $PythonExecutable = (Get-Command python -ErrorAction Stop).Source
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path

$existingListener = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
if ($existingListener) {
    throw "Port $ApiPort is already in use. Stop the old Noodle API before running npm run dev."
}
$existingHarness = Get-NetTCPConnection -LocalPort $HarnessPort -State Listen -ErrorAction SilentlyContinue
if ($existingHarness) {
    throw "Port $HarnessPort is already in use. Stop the old Harness before running npm run dev."
}

$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ($oldPythonPath) { "$serviceSource;$oldPythonPath" } else { $serviceSource }
if (-not $env:NOODLE_DATA_DIR) {
    $env:NOODLE_DATA_DIR = Join-Path $projectRoot 'data\noodle'
}
if (-not $env:NOODLE_SKILLS_DIR) {
    $env:NOODLE_SKILLS_DIR = Join-Path $projectRoot 'services\noodle\skills'
}
$env:NOODLE_API_BASE_URL = "http://127.0.0.1:$ApiPort"
$env:NOODLE_API_PREFIX = '/internal/v1'

$apiOut = Join-Path $runtimeDir 'api.stdout.log'
$apiErr = Join-Path $runtimeDir 'api.stderr.log'
$workerOut = Join-Path $runtimeDir 'worker.stdout.log'
$workerErr = Join-Path $runtimeDir 'worker.stderr.log'

$api = $null
$worker = $null
try {
    $api = Start-Process -FilePath $PythonExecutable `
        -ArgumentList @('-m', 'uvicorn', 'noodle_agent.internal_api:create_app', '--factory', '--host', '127.0.0.1', '--port', "$ApiPort") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut `
        -RedirectStandardError $apiErr `
        -PassThru

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        if ($api.HasExited) {
            $detail = if (Test-Path -LiteralPath $apiErr) { Get-Content -Raw -LiteralPath $apiErr } else { '' }
            throw "Noodle API exited during startup. $detail"
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2
        } catch {
            $health = $null
            Start-Sleep -Milliseconds 500
        }
    } while (-not $health -and [DateTime]::UtcNow -lt $deadline)
    if (-not $health) {
        throw "Noodle API did not become healthy within 60 seconds. See $apiErr"
    }
    if ($health.legacy_agent_loaded -ne $false) {
        throw 'The API is not the trimmed internal service.'
    }

    $worker = Start-Process -FilePath $PythonExecutable `
        -ArgumentList @('-m', 'noodle_agent.cli', 'worker') `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $workerOut `
        -RedirectStandardError $workerErr `
        -PassThru

    @{
        api = $api.Id
        worker = $worker.Id
        started_at = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

    Write-Host "Noodle Domain API: http://127.0.0.1:$ApiPort/internal/docs"
    Write-Host "Noodle Workbench: http://127.0.0.1:$ApiPort/app/"
    Write-Host "Noodle Worker PID: $($worker.Id)"
    Write-Host 'Starting DeepSeek Harness Web in the foreground...'
    Push-Location $projectRoot
    try {
        & npm run harness
    } finally {
        Pop-Location
    }
} finally {
    foreach ($process in @($worker, $api)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit(5000)
        }
    }
    if (Test-Path -LiteralPath $pidFile) {
        Remove-Item -LiteralPath $pidFile -Force
    }
    $env:PYTHONPATH = $oldPythonPath
}
