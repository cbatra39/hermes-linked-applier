#Requires -Version 5.1
<#
.SYNOPSIS
    Hermes — interactive LinkedIn sign-in for the MCP scraper.

.DESCRIPTION
    Starts the one-shot `linkedin-login` service, which runs the MCP image with
    `--login --login-viewer` and publishes a noVNC desktop on LINKEDIN_VIEWER_PORT
    (default 6080). You open that URL in a browser and sign in to LinkedIn with
    your own hands: username, password, 2FA code, and any captcha.

    Hermes does not automate the sign-in and does not solve captchas. That is a
    deliberate design decision, not a missing feature.

    On success, the authenticated browser profile is written to
    /home/pwuser/.linkedin-mcp inside the NAMED VOLUME `linkedin-session`, so the
    long-running linkedin-mcp service picks it up on its next start.

    IMPORTANT: the login container and the linkedin-mcp service cannot use the
    same browser profile at the same time — Chromium takes an exclusive lock on
    the profile directory. This script therefore stops linkedin-mcp first and
    restarts it when you are done (override with -KeepRunning).

.PARAMETER KeepRunning
    Do not stop/restart the linkedin-mcp service around the login run. Only
    useful if linkedin-mcp is already stopped, or you are debugging.

.PARAMETER NoRestart
    Stop linkedin-mcp but do not bring it back up afterwards.

.EXAMPLE
    .\scripts\linkedin-login.ps1
#>
[CmdletBinding()]
param(
    [switch]$KeepRunning,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Head 'HERMES — LINKEDIN INTERACTIVE LOGIN'

Push-Location $HermesRoot
try {
    $null = Assert-DockerReady

    $envMap = Read-DotEnv -Path $HermesEnvFile
    if (-not (Test-Path -LiteralPath $HermesEnvFile)) {
        Write-Warn2 '.env not found — run scripts\bootstrap.ps1 first. Falling back to default ports.'
    }
    $portViewer = [int](Get-DotEnvValue -EnvMap $envMap -Key 'LINKEDIN_VIEWER_PORT' -Default '6080')
    $hostBind   = Get-DotEnvValue -EnvMap $envMap -Key 'HOST_BIND' -Default '127.0.0.1'
    $probeHost  = $hostBind
    if ($probeHost -eq '0.0.0.0' -or $probeHost -eq '') { $probeHost = '127.0.0.1' }
    $viewerUrl  = 'http://' + $probeHost + ':' + $portViewer + '/vnc.html'

    # -----------------------------------------------------------------------
    # Release the browser-profile lock held by the running MCP service.
    # -----------------------------------------------------------------------
    $stopped = $false
    if (-not $KeepRunning) {
        Write-Step 'Stopping linkedin-mcp so the login container can take the browser profile lock'
        $code = Invoke-Native -Exe 'docker' -Arguments @('compose', 'stop', 'linkedin-mcp')
        if ($code -eq 0) {
            $stopped = $true
            Write-Ok 'linkedin-mcp stopped'
        } else {
            Write-Warn2 'Could not stop linkedin-mcp (it may not be running). Continuing.'
        }
    } else {
        Write-Warn2 'Leaving linkedin-mcp alone (-KeepRunning). If login fails with a profile lock error, stop it first.'
    }

    # Port sanity: the login container publishes the viewer port itself.
    if (-not (Test-PortFree -Port $portViewer)) {
        $owner = Get-PortOwner -Port $portViewer
        Write-Warn2 ('Port ' + $portViewer + ' is already in use by ' + $owner + '. The viewer may fail to publish.')
        Write-Info2 'If this is a leftover login container: docker compose --profile login rm -f linkedin-login'
    }

    # -----------------------------------------------------------------------
    # Instructions BEFORE the blocking run — the container output will scroll.
    # -----------------------------------------------------------------------
    Write-Host ''
    Write-Host '-------------------------------------------------------------------------' -ForegroundColor DarkCyan
    Write-Host ' READ THIS FIRST — then the container starts and takes over the terminal' -ForegroundColor Cyan
    Write-Host '-------------------------------------------------------------------------' -ForegroundColor DarkCyan
    Write-Host ''
    Write-Host '  1. Wait for a log line saying the viewer / noVNC is listening.'
    Write-Host '  2. Open this URL in your browser:'
    Write-Host ('       ' + $viewerUrl) -ForegroundColor Green
    Write-Host '     (If it asks for a VNC password, just press Connect — none is set.)'
    Write-Host ''
    Write-Host '  3. In the remote browser window, sign in to LinkedIn YOURSELF:' -ForegroundColor Yellow
    Write-Host '       - email + password'
    Write-Host '       - the 2FA / one-time code, if your account uses one'
    Write-Host '       - any captcha or "is this you?" challenge'
    Write-Host '     Hermes will not do this part for you and cannot solve captchas.'
    Write-Host ''
    Write-Host '  4. Land on your LinkedIn feed. That is what "logged in" means here.'
    Write-Host '  5. The container detects the session, saves the browser profile, and exits.'
    Write-Host '     If it does not exit on its own, press Ctrl+C once you are on the feed.'
    Write-Host ''
    Write-Host '  The session is stored in the Docker volume `linkedin-session`.' -ForegroundColor Gray
    Write-Host '  It survives restarts and reboots. It does NOT travel between machines —' -ForegroundColor Gray
    Write-Host '  see scripts\backup.ps1 and the README: re-login on the new laptop instead.' -ForegroundColor Gray
    Write-Host ''

    if (-not (Read-YesNo -Question 'Start the login container now?' -DefaultYes $true)) {
        Write-Warn2 'Aborted by user.'
        if ($stopped -and -not $NoRestart) {
            Write-Step 'Restarting linkedin-mcp'
            $null = Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', 'linkedin-mcp')
        }
        return
    }

    Write-Head 'LOGIN CONTAINER OUTPUT'
    Write-Host ('Open: ' + $viewerUrl) -ForegroundColor Green
    Write-Host ''

    # --service-ports is what actually publishes 6080 for a `run` (one-shot)
    # container; without it the viewer is unreachable from the host.
    $code = Invoke-Native -Exe 'docker' -Arguments @(
        'compose', '--profile', 'login', 'run', '--rm', '--service-ports', 'linkedin-login'
    )

    Write-Host ''
    if ($code -eq 0) {
        Write-Ok 'Login container exited cleanly.'
    } else {
        Write-Warn2 ('Login container exited with code ' + $code + '. If you completed the sign-in before Ctrl+C, the session was probably still saved — verify below.')
    }

    # -----------------------------------------------------------------------
    # Bring the scraper back up and verify.
    # -----------------------------------------------------------------------
    if ($stopped -and -not $NoRestart) {
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('compose', 'up', '-d', 'linkedin-mcp') -What 'Restarting linkedin-mcp'
    }

    Write-Head 'VERIFY'
    Write-Host 'Check the authenticated state from Hermes:'
    Write-Host ''
    $envMap2 = Read-DotEnv -Path $HermesEnvFile
    $portApi = [int](Get-DotEnvValue -EnvMap $envMap2 -Key 'HERMES_API_PORT' -Default '8080')
    $portDash = [int](Get-DotEnvValue -EnvMap $envMap2 -Key 'HERMES_DASHBOARD_PORT' -Default '3000')
    Write-Host ('  API:       curl http://' + $probeHost + ':' + $portApi + '/api/linkedin/status') -ForegroundColor Green
    Write-Host ('  Dashboard: http://' + $probeHost + ':' + $portDash + '  ->  LinkedIn page') -ForegroundColor Green
    Write-Host ''
    Write-Host 'Expected: {"reachable": true, "authenticated": true, ...}' -ForegroundColor Gray
    Write-Host 'If authenticated is false, re-run this script and make sure you reach the feed.' -ForegroundColor Gray
    Write-Host ''
    Write-Host 'Note: LinkedIn sessions expire and LinkedIn may invalidate them after' -ForegroundColor Yellow
    Write-Host 'unusual activity. Re-running this script is the normal fix.' -ForegroundColor Yellow
    Write-Host ''

} finally {
    Pop-Location
}
