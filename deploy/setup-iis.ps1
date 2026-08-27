<#
.SYNOPSIS
    Put IIS in front of Qlik AI as an HTTPS reverse proxy.

.DESCRIPTION
    Qlik AI listens on 127.0.0.1:8000 and speaks plain HTTP. That is
    deliberate: it is not the job of an application server to hold a
    certificate. This puts IIS in front on 443, terminates TLS there, and
    forwards to the app.

    Why it matters more than it sounds: with Active Directory sign-in, the
    password people type into the login form is their WINDOWS DOMAIN
    PASSWORD. Without TLS that crosses the network in clear text on every
    sign-in - readable by anyone who can see the traffic. It is not this
    app's password that leaks, it is their bank network credential.

    Run from an elevated PowerShell prompt.

.PARAMETER SiteName
    IIS site to create or update. Defaults to QlikAI.

.PARAMETER HostName
    The name people will type. Must match the certificate, and must resolve
    in DNS to this machine.

.PARAMETER CertificateThumbprint
    An existing certificate in LocalMachine\My. Get it with:
        Get-ChildItem Cert:\LocalMachine\My | Format-List Subject, Thumbprint
    Omit it and use -SelfSigned to make one for testing.

.PARAMETER SelfSigned
    Create a self-signed certificate instead. For TESTING ONLY - every
    browser will warn, and a warning people are taught to click through is
    worse than no padlock at all.

.PARAMETER AppPort
    Where Qlik AI is listening. Defaults to 8000.

.EXAMPLE
    .\setup-iis.ps1 -HostName qlikai.bank.internal `
                    -CertificateThumbprint A1B2C3D4E5F6...

.EXAMPLE
    .\setup-iis.ps1 -HostName qlikai.bank.internal -SelfSigned
#>

[CmdletBinding()]
param(
    [string] $SiteName = "QlikAI",
    [Parameter(Mandatory = $true)]
    [string] $HostName,
    [string] $CertificateThumbprint,
    [switch] $SelfSigned,
    [int]    $AppPort = 8000,
    [string] $SitePath = "C:\inetpub\qlikai"
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an elevated PowerShell prompt (Run as administrator)."
    }
}

Assert-Administrator
Import-Module WebAdministration -ErrorAction Stop

# ----------------------------------------------------------------------
# The two IIS pieces that are separate downloads
# ----------------------------------------------------------------------

$missing = @()
if (-not (Get-WebGlobalModule -Name "RewriteModule" -ErrorAction SilentlyContinue)) {
    $missing += "URL Rewrite 2.1  ->  https://www.iis.net/downloads/microsoft/url-rewrite"
}
if (-not (Get-WebGlobalModule -Name "ApplicationRequestRouting" -ErrorAction SilentlyContinue)) {
    $missing += "Application Request Routing 3.0  ->  https://www.iis.net/downloads/microsoft/application-request-routing"
}
if ($missing) {
    throw @"
IIS is missing the modules that make it a reverse proxy. Install these, then
run this again:

  $($missing -join "`n  ")

Both are small MSI downloads from Microsoft. ARR depends on URL Rewrite, so
install URL Rewrite first.
"@
}
Write-Host "URL Rewrite and ARR are present." -ForegroundColor Green

# ----------------------------------------------------------------------
# The certificate
# ----------------------------------------------------------------------

if ($SelfSigned) {
    if ($CertificateThumbprint) {
        throw "Use -CertificateThumbprint or -SelfSigned, not both."
    }
    Write-Warning @"
Creating a SELF-SIGNED certificate. This is for testing only.

Every browser will show a warning, and teaching people to click through a
certificate warning is worse than having no padlock at all - it trains them
to ignore the one thing that would tell them they are being intercepted.

For production, ask whoever runs your internal certificate authority for a
certificate issued to $HostName. Most banks run AD Certificate Services and
this is a routine request.
"@
    $cert = New-SelfSignedCertificate -DnsName $HostName `
        -CertStoreLocation "Cert:\LocalMachine\My" `
        -FriendlyName "Qlik AI (self-signed, testing only)" `
        -NotAfter (Get-Date).AddYears(2)
    $CertificateThumbprint = $cert.Thumbprint
    Write-Host "Created self-signed certificate $CertificateThumbprint"
}

if (-not $CertificateThumbprint) {
    Write-Host ""
    Write-Host "Certificates available in LocalMachine\My:" -ForegroundColor Cyan
    Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.HasPrivateKey } |
        Select-Object Subject, NotAfter, Thumbprint | Format-Table -AutoSize
    throw "Pass -CertificateThumbprint with one of the above, or -SelfSigned to make one for testing."
}

$cert = Get-Item "Cert:\LocalMachine\My\$CertificateThumbprint" -ErrorAction SilentlyContinue
if (-not $cert) {
    throw "No certificate with thumbprint $CertificateThumbprint in LocalMachine\My."
}
if (-not $cert.HasPrivateKey) {
    throw "That certificate has no private key, so it cannot serve TLS. Import the .pfx, not the .cer."
}
if ($cert.NotAfter -lt (Get-Date)) {
    throw "That certificate expired on $($cert.NotAfter)."
}
if ($cert.NotAfter -lt (Get-Date).AddDays(30)) {
    Write-Warning "That certificate expires on $($cert.NotAfter) - under 30 days away."
}
Write-Host "Using certificate: $($cert.Subject)  (expires $($cert.NotAfter.ToString('yyyy-MM-dd')))"

# ----------------------------------------------------------------------
# Turn IIS into a proxy
# ----------------------------------------------------------------------

# Off by default, and nothing forwards anywhere until it is on.
Set-WebConfigurationProperty -PSPath "MACHINE/WEBROOT/APPHOST" `
    -Filter "system.webServer/proxy" -Name "enabled" -Value "True"

# THE setting that breaks the assistant if it is wrong. The chat replies
# stream as server-sent events; ARR buffers responses by default, so the
# whole answer would arrive at once after a minute of apparently nothing
# happening - which reads as the model being hung.
Set-WebConfigurationProperty -PSPath "MACHINE/WEBROOT/APPHOST" `
    -Filter "system.webServer/proxy" -Name "responseBufferThreshold" -Value 0

# Leave the app's own Location headers alone; it only ever redirects to its
# own relative paths.
Set-WebConfigurationProperty -PSPath "MACHINE/WEBROOT/APPHOST" `
    -Filter "system.webServer/proxy" -Name "reverseRewriteHostInResponseHeaders" -Value "False"

Write-Host "ARR proxy enabled, response buffering off." -ForegroundColor Green

# The rewrite rule sets these, so they have to be allowed first.
foreach ($variable in @("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO")) {
    $existing = Get-WebConfiguration -PSPath "MACHINE/WEBROOT/APPHOST" `
        -Filter "system.webServer/rewrite/allowedServerVariables/add[@name='$variable']" `
        -ErrorAction SilentlyContinue
    if (-not $existing) {
        Add-WebConfiguration -PSPath "MACHINE/WEBROOT/APPHOST" `
            -Filter "system.webServer/rewrite/allowedServerVariables" `
            -Value @{ name = $variable }
    }
}

# ----------------------------------------------------------------------
# The site
# ----------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $SitePath | Out-Null

if (-not (Get-Website -Name $SiteName -ErrorAction SilentlyContinue)) {
    New-Website -Name $SiteName -PhysicalPath $SitePath -Port 443 `
        -HostHeader $HostName -Ssl | Out-Null
    Write-Host "Created site $SiteName"
} else {
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $SitePath
    Write-Host "Site $SiteName already exists - reconfiguring it"
}

# HTTPS binding, with the certificate attached.
if (-not (Get-WebBinding -Name $SiteName -Protocol https -ErrorAction SilentlyContinue)) {
    New-WebBinding -Name $SiteName -Protocol https -Port 443 -HostHeader $HostName -SslFlags 1
}
$binding = Get-WebBinding -Name $SiteName -Protocol https
$binding.AddSslCertificate($CertificateThumbprint, "My")

# Plain HTTP on 80, only so it can redirect to HTTPS. Somebody will type the
# address without the scheme, and a connection refused is a support ticket.
if (-not (Get-WebBinding -Name $SiteName -Protocol http -ErrorAction SilentlyContinue)) {
    New-WebBinding -Name $SiteName -Protocol http -Port 80 -HostHeader $HostName
}

# ----------------------------------------------------------------------
# web.config: the forwarding itself
# ----------------------------------------------------------------------

$webConfig = @"
<?xml version="1.0" encoding="UTF-8"?>
<!--
  Written by deploy/setup-iis.ps1. Everything arriving here is forwarded to
  Qlik AI on 127.0.0.1:$AppPort, which is the only place it listens.
-->
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <!-- Anyone who typed the address without https gets sent back with
             it. 307 rather than 301: a permanent redirect is cached by the
             browser forever, which is painful to undo if the site ever
             moves. -->
        <rule name="HTTPS only" stopProcessing="true">
          <match url="(.*)" />
          <conditions>
            <add input="{HTTPS}" pattern="^OFF$" />
          </conditions>
          <action type="Redirect" url="https://{HTTP_HOST}/{R:1}"
                  redirectType="Temporary" />
        </rule>

        <rule name="Forward to Qlik AI" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:$AppPort/{R:1}" />
          <serverVariables>
            <!-- The audit trail records this as the caller's address. -->
            <set name="HTTP_X_FORWARDED_FOR" value="{REMOTE_ADDR}" />
            <set name="HTTP_X_FORWARDED_PROTO" value="https" />
          </serverVariables>
        </rule>
      </rules>
    </rewrite>

    <!-- Chat replies stream as server-sent events. Buffering here would
         hold the whole answer until it finished, which looks exactly like
         the model having hung. -->
    <serverRuntime enabled="true" frequentHitThreshold="1"
                   uploadReadAheadSize="0" />

    <httpProtocol>
      <customHeaders>
        <!-- Tells browsers to use HTTPS for this host for a year without
             being told. Only meaningful once the certificate is trusted -
             harmless with a self-signed one, useless too. -->
        <add name="Strict-Transport-Security"
             value="max-age=31536000; includeSubDomains" />
        <!-- The app serves its own pages and nothing embeds it. -->
        <add name="X-Content-Type-Options" value="nosniff" />
        <add name="X-Frame-Options" value="SAMEORIGIN" />
        <add name="Referrer-Policy" value="same-origin" />
      </customHeaders>
    </httpProtocol>

    <security>
      <requestFiltering>
        <!-- A load script can be large; the default 30 MB is generous
             enough, but the default 2 GB request limit is not something to
             leave open. -->
        <requestLimits maxAllowedContentLength="31457280" />
      </requestFiltering>
    </security>
  </system.webServer>
</configuration>
"@

$configPath = Join-Path $SitePath "web.config"
Set-Content -Path $configPath -Value $webConfig -Encoding UTF8
Write-Host "Wrote $configPath"

# ----------------------------------------------------------------------

New-NetFirewallRule -DisplayName "Qlik AI HTTPS" -Direction Inbound `
    -Protocol TCP -LocalPort 443 -Action Allow -ErrorAction SilentlyContinue | Out-Null
New-NetFirewallRule -DisplayName "Qlik AI HTTP redirect" -Direction Inbound `
    -Protocol TCP -LocalPort 80 -Action Allow -ErrorAction SilentlyContinue | Out-Null

Restart-WebItem "IIS:\Sites\$SiteName" -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. https://$HostName now forwards to 127.0.0.1:$AppPort" -ForegroundColor Green
Write-Host ""
Write-Host "Three things left:" -ForegroundColor Cyan
Write-Host "  1. Set COOKIE_SECURE=true in .env and restart the service."
Write-Host "     Do it now, not before - a Secure cookie is never sent over"
Write-Host "     plain HTTP, so setting it early makes every login appear to"
Write-Host "     succeed and every next request arrive signed out."
Write-Host "  2. Check $HostName resolves to this machine in DNS."
Write-Host "  3. Make sure Qlik AI is bound to 127.0.0.1, not 0.0.0.0 -"
Write-Host "     otherwise the app is still reachable without going through"
Write-Host "     this, and the TLS is decorative."
Write-Host ""
Write-Host "Test it:  Invoke-WebRequest https://$HostName/healthz -UseBasicParsing"
