<#
.SYNOPSIS
    Install Qlik AI as a Windows service.

.DESCRIPTION
    Wraps `python web_app.py` in a service using NSSM, so it starts with the
    machine and comes back after a crash without anybody being logged in.

    Why a wrapper at all: Python is not a service host. Without one the
    server runs only while somebody is signed in with a console open, and
    ends the moment they log off - which is exactly what happens the first
    time the box is patched at 2am.

    Run from an elevated PowerShell prompt.

.PARAMETER ServiceName
    What the service is called. Defaults to QlikAI.

.PARAMETER ProjectDir
    Where the checkout lives. Defaults to this script's parent directory.

.PARAMETER Port
    Port to listen on. Defaults to 8000.

.PARAMETER ListenHost
    Address to bind. Defaults to 127.0.0.1, which is loopback only.
    Use 0.0.0.0 to serve the network - and put TLS in front of it first.

.PARAMETER Account
    The Windows account the service runs as. Defaults to LocalSystem.
    Prefer a dedicated low-privilege account that can read the Qlik
    certificates and write the data directory, and nothing else.

.EXAMPLE
    .\install-service.ps1 -ListenHost 0.0.0.0 -Port 8000

.EXAMPLE
    .\install-service.ps1 -Account "DOMAIN\svc_qlikai" -Password (Read-Host -AsSecureString)
#>

[CmdletBinding()]
param(
    [string]   $ServiceName = "QlikAI",
    [string]   $ProjectDir  = (Split-Path -Parent $PSScriptRoot),
    [int]      $Port        = 8000,
    [string]   $ListenHost  = "127.0.0.1",
    [string]   $Account     = "LocalSystem",
    [SecureString] $Password,
    [string]   $LogDir      = "C:\ProgramData\QlikAI\logs",
    [string]   $DataDir     = "C:\ProgramData\QlikAI"
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell prompt (Run as administrator)."
    }
}

function Find-Nssm {
    $found = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }

    foreach ($candidate in @(
        "$ProjectDir\deploy\nssm.exe",
        "C:\Program Files\nssm\nssm.exe",
        "C:\nssm\nssm.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }

    throw @"
NSSM was not found.

It is a small open-source service wrapper. Download nssm.exe from
https://nssm.cc/download, put it beside this script (or anywhere on PATH),
and run this again.

Alternative if your organisation will not allow NSSM: Windows' own
sc.exe cannot host a plain executable as a service, so the usual
substitutes are a Scheduled Task set to run At startup as the service
account, or WinSW (https://github.com/winsw/winsw), which is configured
with an XML file instead.
"@
}

Assert-Administrator

$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Create one and install requirements.txt first."
}
$entry = Join-Path $ProjectDir "web_app.py"
if (-not (Test-Path $entry)) {
    throw "No web_app.py in $ProjectDir - is -ProjectDir right?"
}

$nssm = Find-Nssm
Write-Host "Using NSSM at $nssm"

# The data directory holds the account database and every saved
# conversation. It is the thing to back up, and the only thing here that
# cannot be recreated from the checkout.
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir  | Out-Null

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service $ServiceName already exists - stopping it to reconfigure."
    if ($existing.Status -ne "Stopped") {
        & $nssm stop $ServiceName | Out-Null
        Start-Sleep -Seconds 2
    }
} else {
    & $nssm install $ServiceName $python $entry
    if ($LASTEXITCODE -ne 0) { throw "nssm install failed with $LASTEXITCODE" }
}

& $nssm set $ServiceName Application       $python
& $nssm set $ServiceName AppDirectory      $ProjectDir
& $nssm set $ServiceName AppParameters     "web_app.py --host $ListenHost --port $Port"
& $nssm set $ServiceName DisplayName       "Qlik AI"
& $nssm set $ServiceName Description       "AI assistant for Qlik Sense: load editor, dashboards and chat."
& $nssm set $ServiceName Start             SERVICE_AUTO_START

# The app writes its own rotating log via LOG_FILE. These catch anything
# that escapes before logging is configured - an import error, a bad .env -
# which is exactly when you most need to see something.
& $nssm set $ServiceName AppStdout         (Join-Path $LogDir "service-out.log")
& $nssm set $ServiceName AppStderr         (Join-Path $LogDir "service-err.log")
& $nssm set $ServiceName AppRotateFiles    1
& $nssm set $ServiceName AppRotateBytes    10485760

# Restart on failure, but back off. A service that crashes on a bad
# configuration and restarts instantly forever writes gigabytes of identical
# stack traces and hides the one at the top.
& $nssm set $ServiceName AppExit Default   Restart
& $nssm set $ServiceName AppRestartDelay   10000
& $nssm set $ServiceName AppThrottle       10000

# Give it time to close Qlik sessions and save open conversations rather
# than being killed mid-write.
& $nssm set $ServiceName AppStopMethodSkip 0
& $nssm set $ServiceName AppStopMethodConsole 15000

if ($Account -ne "LocalSystem") {
    if (-not $Password) {
        throw "-Password is required when -Account is not LocalSystem."
    }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password))
    & $nssm set $ServiceName ObjectName $Account $plain
    Write-Host "Service will run as $Account"
} else {
    Write-Warning @"
Running as LocalSystem, which is more privilege than this needs.
Prefer a dedicated account that can read the Qlik certificates and write
$DataDir, and nothing else. Re-run with -Account and -Password to change it.
"@
}

Write-Host ""
Write-Host "Installed. Before starting it, check these are set in $ProjectDir\.env:" -ForegroundColor Cyan
Write-Host "    QLIK_MODE=enterprise"
Write-Host "    QLIK_HOST=<the engine hostname on the certificate>"
Write-Host "    QLIK_CERT_DIR=<folder holding client.pem, client_key.pem, root.pem>"
Write-Host "    USERS_DB=$DataDir\users.db"
Write-Host "    HISTORY_DIR=$DataDir\history"
Write-Host "    LOG_FILE=$LogDir\qlik-ai.log"
Write-Host "    LLM_PROVIDER=openai"
Write-Host "    OPENAI_BASE_URL=<your endpoint>"
Write-Host ""

if ($ListenHost -ne "127.0.0.1" -and $ListenHost -ne "localhost") {
    Write-Warning @"
Binding to $ListenHost serves the network. Two things must be true first:
  1. AUTH_ENABLED must not be false - the server refuses to start that way,
     which is deliberate.
  2. Put TLS in front of it (IIS ARR, nginx, or the bank's load balancer)
     and then set COOKIE_SECURE=true. Until then passwords cross the wire
     in the clear.
"@
    Write-Host ""
}

Write-Host "Start it with:  nssm start $ServiceName"
Write-Host "Watch it with:  Get-Content '$LogDir\qlik-ai.log' -Wait -Tail 40"
Write-Host "Health check:   curl http://${ListenHost}:$Port/healthz"
Write-Host ""
Write-Host "First start creates an administrator and prints its password ONCE" -ForegroundColor Yellow
Write-Host "to $LogDir\service-out.log. Set ADMIN_PASSWORD in .env to choose it" -ForegroundColor Yellow
Write-Host "yourself, or read it from that file and change it after signing in." -ForegroundColor Yellow
