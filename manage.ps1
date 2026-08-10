[CmdletBinding()]
param(
    [ValidateSet("init", "start", "stop", "status", "health")]
    [string]$Action = "status"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$VenvDir = Join-Path $Root ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
$EnvFile = Join-Path $Root ".env"
$RunDir = Join-Path $Root "run"
$LogDir = Join-Path $Root "logs"
$PidFile = Join-Path $RunDir "webui.pid"
$StdoutLog = Join-Path $LogDir "webui.stdout.log"
$StderrLog = Join-Path $LogDir "webui.stderr.log"
$HostAddress = "127.0.0.1"
$Port = 5000
$LoginUrl = "http://${HostAddress}:${Port}/login"
$PlaceholderAuthCode = "CHANGE_ME_LOCAL_ONLY"
$DefaultProxyFile = "..\runtime\resin\data\register-proxies.txt"

function Write-Step([string]$Message) {
    Write-Host "[register-test] $Message"
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Ensure-LocalEnv {
    if (Test-Path -LiteralPath $EnvFile) {
        return
    }

    $content = @"
# Local native runtime. Do not commit this file.
# Replace this placeholder before exposing the WebUI beyond this PC.
WEBUI_AUTH_CODE=$PlaceholderAuthCode

# Native headless browser driver.
REGISTRATION_DRIVER=cloak
CLOAK_HEADLESS=True
CLOAK_HUMANIZE=True
CLOAK_GEOIP=True
CLOAK_USE_PROXY=True
CLOAK_KEEP_BROWSER_OPEN=False

# Prefer Resin sticky-account routes. An absent/empty file falls back to PROXY_POOL.
PROXY_POOL_FILE=../runtime/resin/data/register-proxies.txt
PROXY_POOL=
PLAN_CHECK_PROXY_MODE=auto
"@
    Write-Utf8NoBom -Path $EnvFile -Content ($content.TrimStart() + "`n")
    Write-Step "Created .env with placeholder auth code $PlaceholderAuthCode. Change it before regular use."
}

function Get-ServiceProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }

    $raw = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    $servicePid = 0
    if (-not [int]::TryParse($raw, [ref]$servicePid)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $null
    }

    $commandLine = [string]$process.CommandLine
    if ($commandLine -notmatch '(^|\s)web\.py(\s|$)' -or $commandLine -notmatch '--port\s+5000') {
        throw "PID file does not point to this WebUI. Refusing to operate on PID $servicePid."
    }
    return $process
}

function Test-WebHealth {
    try {
        $response = Invoke-WebRequest -Uri $LoginUrl -UseBasicParsing -TimeoutSec 5
        return [int]$response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Show-ProxyStatus {
    $source = Join-Path $Root $DefaultProxyFile
    $source = [System.IO.Path]::GetFullPath($source)
    if (Test-Path -LiteralPath $source) {
        $count = @(Get-Content -LiteralPath $source -Encoding UTF8 | Where-Object {
            $line = $_.Trim()
            $line -and -not $line.StartsWith("#") -and -not $line.StartsWith(";")
        }).Count
        Write-Step "Proxy file found: $source (non-comment lines: $count)"
    }
    else {
        Write-Step "Proxy file not found: $source. Falling back to PROXY_POOL from .env."
    }
}

function Initialize-NativeRuntime {
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Step "Creating the Python 3.12 virtual environment: $VenvDir"
        & py -3.12 -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create the Python 3.12 virtual environment."
        }
    }
    else {
        Write-Step "Reusing the existing Python 3.12 virtual environment."
    }

    Ensure-LocalEnv
    New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null

    Write-Step "Upgrading pip."
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip."
    }

    Write-Step "Installing requirements.txt."
    & $Python -m pip install -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }

    Write-Step "Checking core imports."
    & $Python -c "import flask, curl_cffi, pyotp, selenium, playwright, cloakbrowser; print('native dependencies: OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Core dependency import check failed."
    }

    Show-ProxyStatus
    Write-Step "Initialization complete. Next, run 02-start.bat."
}

function Start-WebUi {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Runtime is not initialized. Run manage.ps1 -Action init or 01-init.bat first."
    }
    Ensure-LocalEnv
    New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null

    $running = Get-ServiceProcess
    if ($null -ne $running) {
        Write-Step "WebUI is already running. PID=$($running.ProcessId), URL=http://${HostAddress}:${Port}/"
        return
    }

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $listener) {
        throw "Port $Port is already in use by PID $($listener.OwningProcess)."
    }

    $envText = Get-Content -LiteralPath $EnvFile -Raw -ErrorAction SilentlyContinue
    if ($envText -match "(?m)^WEBUI_AUTH_CODE=$([regex]::Escape($PlaceholderAuthCode))\s*$") {
        Write-Warning "WebUI still uses placeholder auth code $PlaceholderAuthCode. Change it in .env."
    }

    Write-Step "Starting WebUI in the background."
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList @("-u", "web.py", "--host", $HostAddress, "--port", "$Port") `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru
    Write-Utf8NoBom -Path $PidFile -Content ([string]$process.Id)

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            $tail = if (Test-Path -LiteralPath $StderrLog) {
                (Get-Content -LiteralPath $StderrLog -Tail 20) -join "`n"
            } else { "No error log was created." }
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
            throw "WebUI process exited during startup.`n$tail"
        }
        if (Test-WebHealth) {
            Write-Step "WebUI is healthy. PID=$($process.Id), URL=http://${HostAddress}:${Port}/"
            Show-ProxyStatus
            return
        }
    } while ((Get-Date) -lt $deadline)

    throw "WebUI did not pass health checks within 30 seconds. Log: $StderrLog"
}

function Stop-WebUi {
    $process = Get-ServiceProcess
    if ($null -eq $process) {
        Write-Step "WebUI is not running."
        return
    }

    $servicePid = [int]$process.ProcessId
    Write-Step "Stopping WebUI. PID=$servicePid"
    Stop-Process -Id $servicePid -Force
    Wait-Process -Id $servicePid -Timeout 10 -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Step "WebUI stopped."
}

function Show-Status {
    $process = Get-ServiceProcess
    if ($null -eq $process) {
        Write-Step "Status: stopped."
        Show-ProxyStatus
        return $false
    }

    $healthy = Test-WebHealth
    Write-Step "Status: running. PID=$($process.ProcessId), HTTP healthy=$healthy, URL=http://${HostAddress}:${Port}/"
    Show-ProxyStatus
    return $healthy
}

try {
    Set-Location -LiteralPath $Root
    switch ($Action) {
        "init" { Initialize-NativeRuntime }
        "start" { Start-WebUi }
        "stop" { Stop-WebUi }
        "status" { [void](Show-Status) }
        "health" {
            if (-not (Show-Status)) {
                exit 3
            }
        }
    }
}
catch {
    Write-Error $_
    exit 1
}
