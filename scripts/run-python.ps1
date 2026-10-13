param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('setup', 'api', 'worker', 'test', 'lint', 'delivery')]
    [string]$Task
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonExecutable = $env:NOODLE_PYTHON
if (-not $pythonExecutable) {
    $envFile = Join-Path $projectRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        $configured = Get-Content -LiteralPath $envFile | Where-Object {
            $_ -match '^\s*NOODLE_PYTHON\s*='
        } | Select-Object -Last 1
        if ($configured) {
            $pythonExecutable = ($configured -split '=', 2)[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $pythonExecutable) {
    $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
}
$pythonExecutable = (Resolve-Path -LiteralPath $pythonExecutable).Path
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot '.runtime') | Out-Null
$testBaseTemp = Join-Path $projectRoot (".runtime/pytest-" + [guid]::NewGuid().ToString("N"))

if ($Task -eq 'delivery') {
    $deliveryScript = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'scripts\delivery-check.py')).Path
    & $pythonExecutable $deliveryScript
    exit $LASTEXITCODE
}

$pythonArgs = switch ($Task) {
    'setup' { @('-m', 'pip', 'install', '-e', './services/noodle[dev]') }
    'api' { @('-m', 'noodle_agent.cli', 'serve-internal', '--host', '127.0.0.1', '--port', '8000') }
    'worker' { @('-m', 'noodle_agent.cli', 'worker') }
    'lint' { @('-m', 'ruff', 'check', './services/noodle/src', './services/noodle/tests') }
    'test' {
        @(
            '-m', 'pytest', './services/noodle/tests',
            '--basetemp', $testBaseTemp,
            '-p', 'no:cacheprovider'
        )
    }
}

& $pythonExecutable @pythonArgs
exit $LASTEXITCODE
