#Requires -Version 5.1
<#
.SYNOPSIS
    Hermes — one-shot bootstrap: preflight, .env, build, up, then tell the human
    exactly which steps only they can do.

.DESCRIPTION
    Order of operations:
      1. Preflight   — Docker running? Compose v2? compose file present? ports free?
      2. Config      — copy .env.example -> .env, generate ENCRYPTION_KEY if blank.
      3. Build       — `docker compose build` plus the build-only sandbox image.
      4. Up          — `docker compose up -d` (hermes-core is held back until
                       FREELLMAPI_KEY is set, because it cannot do anything without it).
      5. Handover    — print the numbered manual checklist.

    Safe to re-run. Nothing here is destructive.

.PARAMETER SkipBuild
    Skip both build steps. Use when images are already built or were `docker load`ed.

.PARAMETER SkipPreflight
    Skip the Docker/port checks. Not recommended.

.PARAMETER Force
    Treat port conflicts as warnings instead of hard failures.

.PARAMETER All
    Start hermes-core even when FREELLMAPI_KEY is still blank. It will boot but
    every LLM call will fail until you set the key.

.PARAMETER Pull
    `docker compose pull` the upstream images (freellmapi, linkedin-mcp) first.

.PARAMETER NoCache
    Build without the layer cache.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

.EXAMPLE
    .\scripts\bootstrap.ps1 -SkipBuild -All
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipPreflight,
    [switch]$Force,
    [switch]$All,
    [switch]$Pull,
    [switch]$NoCache
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Head 'HERMES BOOTSTRAP'
Write-Info2 ('Project root: ' + $HermesRoot)

Push-Location $HermesRoot
try {

    # -----------------------------------------------------------------------
    # 1. PREFLIGHT
    # -----------------------------------------------------------------------

    $composeFile = $null
    foreach ($candidate in @('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')) {
        $p = Join-Path $HermesRoot $candidate
        if (Test-Path -LiteralPath $p) { $composeFile = $p; break }
    }
    if (-not $composeFile) {
        Stop-Hermes ('No compose file found in ' + $HermesRoot + '. Expected docker-compose.yml. Are you running this from a complete Hermes checkout?')
    }
    Write-Ok ('Compose file: ' + (Split-Path -Leaf $composeFile))

    if (-not $SkipPreflight) {
        $null = Assert-DockerReady
    } else {
        Write-Warn2 'Preflight skipped (-SkipPreflight).'
        if (-not (Test-CommandExists 'docker')) {
            Stop-Hermes 'docker is not on PATH; even with -SkipPreflight this cannot continue.'
        }
    }

    # -----------------------------------------------------------------------
    # 2. CONFIG (.env)
    # -----------------------------------------------------------------------

    Write-Step 'Preparing .env'

    if (-not (Test-Path -LiteralPath $HermesEnvFile)) {
        if (Test-Path -LiteralPath $HermesEnvExample) {
            Copy-Item -LiteralPath $HermesEnvExample -Destination $HermesEnvFile
            Write-Ok 'Created .env from .env.example'
        } else {
            Write-Warn2 '.env.example is missing from this checkout — writing a built-in default .env instead.'
            $enc = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($HermesEnvFile, (Get-EnvExampleFallback), $enc)
            Write-Ok 'Created .env from the built-in template'
        }
    } else {
        Write-Ok '.env already exists (left untouched except for blank ENCRYPTION_KEY)'
    }

    # ENCRYPTION_KEY: freellmapi encrypts stored provider keys with it. Blank key
    # => the container refuses to start or stores secrets unencrypted.
    $envMap = Read-DotEnv -Path $HermesEnvFile
    $encKey = Get-DotEnvValue -EnvMap $envMap -Key 'ENCRYPTION_KEY' -Default ''
    if ($encKey -eq '') {
        $newKey = New-HexKey -Bytes 32
        Set-DotEnvValue -Path $HermesEnvFile -Key 'ENCRYPTION_KEY' -Value $newKey
        Write-Ok 'Generated a new 64-char hex ENCRYPTION_KEY into .env'
        Write-Info2 'Back this up. Losing it makes the provider keys stored inside freellmapi unreadable.'
    } else {
        if ($encKey.Length -ne 64) {
            Write-Warn2 ('ENCRYPTION_KEY is ' + $encKey.Length + ' chars; freellmapi expects 64 hex characters. Leaving it as-is.')
        } else {
            Write-Ok 'ENCRYPTION_KEY already set'
        }
    }

    # Re-read so downstream logic sees the generated key.
    $envMap = Read-DotEnv -Path $HermesEnvFile

    $portDash   = [int](Get-DotEnvValue -EnvMap $envMap -Key 'HERMES_DASHBOARD_PORT' -Default '3000')
    $portLlm    = [int](Get-DotEnvValue -EnvMap $envMap -Key 'FREELLMAPI_PORT'       -Default '3001')
    $portApi    = [int](Get-DotEnvValue -EnvMap $envMap -Key 'HERMES_API_PORT'       -Default '8080')
    $portViewer = [int](Get-DotEnvValue -EnvMap $envMap -Key 'LINKEDIN_VIEWER_PORT'  -Default '6080')
    $hostBind   = Get-DotEnvValue -EnvMap $envMap -Key 'HOST_BIND' -Default '127.0.0.1'
    $freeKey    = Get-DotEnvValue -EnvMap $envMap -Key 'FREELLMAPI_KEY' -Default ''

    # -----------------------------------------------------------------------
    # PREFLIGHT (ports) — done after .env so we check the configured ports
    # -----------------------------------------------------------------------

    if (-not $SkipPreflight) {
        Write-Step 'Checking ports'

        # If our own stack is already up, it legitimately owns these ports.
        $stackUp = Test-StackHasContainers
        $portList = @(
            @{ Port = $portDash;   Name = 'hermes-dashboard' },
            @{ Port = $portLlm;    Name = 'freellmapi' },
            @{ Port = $portApi;    Name = 'hermes-core' },
            @{ Port = $portViewer; Name = 'linkedin login viewer (noVNC)' }
        )

        $conflicts = @()
        foreach ($entry in $portList) {
            $p = [int]$entry.Port
            if (Test-PortFree -Port $p) {
                Write-Ok ('port ' + $p + ' free (' + $entry.Name + ')')
            } else {
                $owner = Get-PortOwner -Port $p
                if ($stackUp) {
                    Write-Warn2 ('port ' + $p + ' in use by ' + $owner + ' — most likely this Hermes stack already running.')
                } else {
                    Write-Bad ('port ' + $p + ' in use by ' + $owner + ' (' + $entry.Name + ')')
                    $conflicts += $p
                }
            }
        }

        if ($conflicts.Count -gt 0 -and -not $Force) {
            Write-Host ''
            Write-Info2 'Fix options:'
            Write-Info2 '  a) stop whatever owns the port, or'
            Write-Info2 '  b) change the port in .env (HERMES_DASHBOARD_PORT / FREELLMAPI_PORT /'
            Write-Info2 '     HERMES_API_PORT / LINKEDIN_VIEWER_PORT) and re-run, or'
            Write-Info2 '  c) re-run with -Force to continue anyway.'
            Stop-Hermes ('Ports already in use: ' + ($conflicts -join ', '))
        }
    }

    # -----------------------------------------------------------------------
    # 3. BUILD
    # -----------------------------------------------------------------------

    if ($Pull) {
        Write-Step 'Pulling upstream images (freellmapi, linkedin-mcp)'
        # Non-fatal: locally built services have nothing to pull.
        $code = Invoke-Native -Exe 'docker' -Arguments @('compose', 'pull', '--ignore-buildable')
        if ($code -ne 0) {
            Write-Warn2 'compose pull reported an error; continuing (build/up will surface anything fatal).'
        }
    }

    if (-not $SkipBuild) {
        $buildArgs = @('compose', 'build')
        if ($NoCache) { $buildArgs += '--no-cache' }
        Write-Info2 'First build downloads base images and installs dependencies. Expect 5-15 minutes.'
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments $buildArgs -What 'Building hermes-core and hermes-dashboard'

        # The sandbox image is profile-gated so it never runs as a service, but
        # hermes-core needs it present locally to spawn ephemeral containers.
        $sandboxArgs = @('compose', '--profile', 'build-only', 'build')
        if ($NoCache) { $sandboxArgs += '--no-cache' }
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments $sandboxArgs -What 'Building the sandbox image (profile build-only)'
    } else {
        Write-Warn2 'Build skipped (-SkipBuild).'
    }

    # -----------------------------------------------------------------------
    # 4. UP
    # -----------------------------------------------------------------------

    $coreDeferred = $false
    if ($freeKey -eq '' -and -not $All) {
        $coreDeferred = $true
        Write-Step 'Starting freellmapi, linkedin-mcp and hermes-dashboard'
        Write-Info2 'hermes-core is held back: FREELLMAPI_KEY is still blank, so it has no LLM to talk to.'
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('compose', 'up', '-d', 'freellmapi', 'linkedin-mcp', 'hermes-dashboard')
    } else {
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('compose', 'up', '-d') -What 'Starting the stack'
    }

    Write-Step 'Container status'
    $null = Invoke-Native -Exe 'docker' -Arguments @('compose', 'ps')

    # -----------------------------------------------------------------------
    # 5. HEALTH PROBES (best effort — never fatal)
    # -----------------------------------------------------------------------

    $probeHost = $hostBind
    if ($probeHost -eq '0.0.0.0' -or $probeHost -eq '') { $probeHost = '127.0.0.1' }

    $llmUrl  = 'http://' + $probeHost + ':' + $portLlm + '/api/ping'
    $dashUrl = 'http://' + $probeHost + ':' + $portDash + '/'
    $apiUrl  = 'http://' + $probeHost + ':' + $portApi + '/api/health'

    $null = Wait-HttpOk -Url $llmUrl  -TimeoutSec 90 -Label ('freellmapi (' + $llmUrl + ')')
    $null = Wait-HttpOk -Url $dashUrl -TimeoutSec 60 -Label ('dashboard (' + $dashUrl + ')')
    if (-not $coreDeferred) {
        $null = Wait-HttpOk -Url $apiUrl -TimeoutSec 90 -Label ('hermes-core (' + $apiUrl + ')')
    }

    # -----------------------------------------------------------------------
    # 6. HANDOVER
    # -----------------------------------------------------------------------

    $dashRoot   = 'http://' + $probeHost + ':' + $portDash
    $llmRoot    = 'http://' + $probeHost + ':' + $portLlm
    $viewerRoot = 'http://' + $probeHost + ':' + $portViewer + '/vnc.html'

    Write-Head 'WHAT YOU MUST DO BY HAND'
    Write-Host ''
    Write-Host 'These steps cannot be automated: they need a human account, a human' -ForegroundColor White
    Write-Host 'password, and a human clicking "I accept". Do them in this order.' -ForegroundColor White
    Write-Host ''

    Write-Host '  (1) MINT THE LLM ROUTER KEY' -ForegroundColor Yellow
    Write-Host ('      a. Open the freellmapi dashboard:  ' + $llmRoot)
    Write-Host '      b. Create the first local account (you are the only user; this account'
    Write-Host '         lives inside your own container, not on anyone else''s server).'
    Write-Host '         If you opened it from another device on your LAN, freellmapi prints a'
    Write-Host '         one-time setup code to its log instead of trusting the browser:'
    Write-Host '             docker compose logs freellmapi'
    Write-Host '      c. Add free provider keys. Each is free to create and independently'
    Write-Host '         rate-limited, so add several and let the router fail over:'
    Write-Host '             Google AI Studio, Groq, Cerebras, OpenRouter, Mistral, GitHub Models'
    Write-Host '      d. Create/copy the unified client key. It looks like:  freellmapi-xxxxxxxx'
    Write-Host '      e. Paste it into .env :'
    Write-Host ('             FREELLMAPI_KEY=freellmapi-...        (file: ' + $HermesEnvFile + ')')
    Write-Host '      f. Start (or restart) the orchestrator so it picks up the key:'
    Write-Host '             docker compose up -d hermes-core' -ForegroundColor Green
    Write-Host ''

    Write-Host '  (2) LOG IN TO LINKEDIN (interactive, once per machine)' -ForegroundColor Yellow
    Write-Host '      Run:'
    Write-Host '             .\scripts\linkedin-login.ps1' -ForegroundColor Green
    Write-Host ('      Then open ' + $viewerRoot + ' and sign in by hand.')
    Write-Host '      You must complete 2FA / captcha yourself — Hermes does not and will not'
    Write-Host '      solve those. The authenticated browser profile persists in the'
    Write-Host '      `linkedin-session` Docker volume, so this survives restarts.'
    Write-Host ''

    Write-Host '  (3) OPEN HERMES' -ForegroundColor Yellow
    Write-Host ('             ' + $dashRoot) -ForegroundColor Green
    Write-Host '      Overview -> confirm LLM + LinkedIn + Docker are all green, then:'
    Write-Host '      LinkedIn page -> Import profile; Resume page -> Generate; Jobs page -> Search.'
    Write-Host ''

    Write-Host '  REMINDER: Hermes never submits an application for you.' -ForegroundColor Magenta
    Write-Host '  It ranks jobs, tailors a resume, and hands you an apply link. You click it.' -ForegroundColor Magenta
    Write-Host ''

    if ($coreDeferred) {
        Write-Warn2 'hermes-core is NOT running yet. Finish step (1) then: docker compose up -d hermes-core'
    }

    Write-Host 'Docs: README.md  |  docs/ARCHITECTURE.md  |  docs/RUNBOOK.md' -ForegroundColor Gray
    Write-Host ''

} finally {
    Pop-Location
}
