#Requires -Version 5.1
<#
.SYNOPSIS
    Hermes — the receiving half of `ship`: docker load the image tar, verify the
    stack's images are all present, then bring everything up WITHOUT rebuilding.

.DESCRIPTION
    Run this on the TARGET machine, from inside the unpacked repo, after copying
    over a bundle produced by scripts\ship.ps1 -Mode images.

    Steps:
      1. Locate hermes-images.tar (bundle root, .\dist, or -TarPath).
      2. Align the compose project name with the one recorded by ship, so the
         named volumes keep the names the bundle's docs and .env refer to.
      3. `docker load -i <tar>`  (2-5 minutes; it is mostly disk).
      4. Verify every image the compose file needs now exists locally —
         normally 5: hermes-core, hermes-dashboard, hermes-sandbox, freellmapi
         and linkedin-mcp-server.
      5. Create .env from .env.example and generate ENCRYPTION_KEY if blank.
      6. `docker compose up -d --no-build`.
      7. Print the manual, per-machine steps that no script can do for you.

    Safe to re-run. `docker load` on already-present layers is a no-op.

    This does NOT restore your data. The SQLite DB and the freellmapi provider
    keys live in Docker volumes, which are not part of a ship bundle — use
    scripts\backup.ps1 on the old machine and scripts\restore.ps1 here. The
    LinkedIn session is never transferred: re-run scripts\linkedin-login.ps1.

.PARAMETER TarPath
    Explicit path to hermes-images.tar. Default: searched, see above.

.PARAMETER Project
    Override the compose project name. Default: COMPOSE_PROJECT_NAME, else the
    `name:` key in docker-compose.yml (which is pinned to `hermes`).

.PARAMETER SkipLoad
    Skip `docker load` and only verify + start. Use when the images are already
    loaded and you just want the rest of the sequence.

.PARAMETER All
    Start hermes-core even when FREELLMAPI_KEY is still blank. It boots, but
    every LLM-backed feature fails until you set the key.

.PARAMETER NoUp
    Load and verify only; do not start anything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\load.ps1

.EXAMPLE
    .\scripts\load.ps1 -TarPath D:\usb\hermes-bundle\hermes-images.tar
#>
[CmdletBinding()]
param(
    [string]$TarPath = '',
    [string]$Project = '',
    [switch]$SkipLoad,
    [switch]$All,
    [switch]$NoUp
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

# ---------------------------------------------------------------------------
# Local helper: the REAL compose project name.
#
# docker-compose.yml pins `name: hermes` at the top level, and that beats the
# directory basename that Get-ComposeProjectName falls back to. Precedence, per
# the Compose Specification: -p flag > COMPOSE_PROJECT_NAME > `name:` in the
# compose file > directory basename. Volumes are prefixed with the winner, so
# getting this wrong means hunting for volumes that do not exist.
# ---------------------------------------------------------------------------
function Get-HermesProject {
    param([hashtable]$EnvMap = $null, [string]$Override = '')

    if ($Override -ne '') { return $Override }
    if ($env:COMPOSE_PROJECT_NAME) { return $env:COMPOSE_PROJECT_NAME }
    if ($EnvMap -and $EnvMap.ContainsKey('COMPOSE_PROJECT_NAME')) {
        if ($EnvMap['COMPOSE_PROJECT_NAME'] -ne '') { return $EnvMap['COMPOSE_PROJECT_NAME'] }
    }

    $composePath = Join-Path $HermesRoot 'docker-compose.yml'
    if (Test-Path -LiteralPath $composePath) {
        foreach ($line in (Get-Content -LiteralPath $composePath)) {
            $m = [System.Text.RegularExpressions.Regex]::Match($line, '^name:\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*$')
            if ($m.Success) { return $m.Groups[1].Value }
        }
    }
    return (Get-ComposeProjectName -EnvMap $EnvMap)
}

Write-Head 'HERMES LOAD — offline install from an image tar'

Push-Location $HermesRoot
try {
    $null = Assert-DockerReady

    $enc = New-Object System.Text.UTF8Encoding($false)

    # -----------------------------------------------------------------------
    # 1. Locate the image tar
    # -----------------------------------------------------------------------
    if (-not $SkipLoad) {
        if ($TarPath -eq '') {
            Write-Step 'Looking for hermes-images.tar'
            $parent = Split-Path -Parent $HermesRoot
            $candidates = @(
                (Join-Path $HermesRoot 'dist\hermes-images.tar'),
                (Join-Path $HermesRoot 'hermes-images.tar')
            )
            if ($parent) {
                # ship.ps1 lays out <bundle>\hermes-images.tar next to <bundle>\repo\,
                # and you are usually standing in <bundle>\repo.
                $candidates += (Join-Path $parent 'hermes-images.tar')
                $candidates += (Join-Path $parent 'dist\hermes-images.tar')
            }
            foreach ($c in $candidates) {
                Write-Info2 ('checking ' + $c)
                if (Test-Path -LiteralPath $c) { $TarPath = $c; break }
            }
        }

        if ($TarPath -eq '' -or -not (Test-Path -LiteralPath $TarPath)) {
            Write-Host ''
            Write-Info2 'Searched:'
            Write-Info2 '    .\dist\hermes-images.tar'
            Write-Info2 '    .\hermes-images.tar'
            Write-Info2 '    ..\hermes-images.tar          (the layout scripts\ship.ps1 produces)'
            Write-Info2 '    ..\dist\hermes-images.tar'
            Write-Host ''
            Write-Info2 'Either point at it explicitly:'
            Write-Info2 '    .\scripts\load.ps1 -TarPath <path to hermes-images.tar>'
            Write-Info2 'or, if you have no bundle, build from source instead:'
            Write-Info2 '    .\scripts\bootstrap.ps1'
            Stop-Hermes 'hermes-images.tar not found.'
        }

        $tarItem = Get-Item -LiteralPath $TarPath
        Write-Ok ('Image tar: ' + $tarItem.FullName + '  (' + (Format-Bytes -Bytes $tarItem.Length) + ')')
    } else {
        Write-Warn2 'Skipping docker load (-SkipLoad).'
    }

    # -----------------------------------------------------------------------
    # 2. .env (needed before the project-name check, which reads it)
    # -----------------------------------------------------------------------
    Write-Step 'Preparing .env'
    if (-not (Test-Path -LiteralPath $HermesEnvFile)) {
        if (Test-Path -LiteralPath $HermesEnvExample) {
            Copy-Item -LiteralPath $HermesEnvExample -Destination $HermesEnvFile
            Write-Ok 'Created .env from .env.example'
        } else {
            Write-Warn2 '.env.example is missing from this checkout — writing the built-in default instead.'
            [System.IO.File]::WriteAllText($HermesEnvFile, (Get-EnvExampleFallback), $enc)
            Write-Ok 'Created .env from the built-in template'
        }
    } else {
        Write-Ok '.env already exists (left untouched except for a blank ENCRYPTION_KEY)'
    }

    $envMap = Read-DotEnv -Path $HermesEnvFile
    $encKey = Get-DotEnvValue -EnvMap $envMap -Key 'ENCRYPTION_KEY' -Default ''
    if ($encKey -eq '') {
        Set-DotEnvValue -Path $HermesEnvFile -Key 'ENCRYPTION_KEY' -Value (New-HexKey -Bytes 32)
        Write-Ok 'Generated a new 64-char hex ENCRYPTION_KEY into .env'
        Write-Info2 'If you are ALSO restoring the freellmapi-data volume from another machine,'
        Write-Info2 'that volume was encrypted with the OTHER machine ENCRYPTION_KEY. Copy that'
        Write-Info2 'value into .env instead, or re-add the provider keys by hand.'
    } elseif ($encKey.Length -ne 64) {
        Write-Warn2 ('ENCRYPTION_KEY is ' + $encKey.Length + ' chars; freellmapi expects 64 hex characters. Leaving it as-is.')
    } else {
        Write-Ok 'ENCRYPTION_KEY already set'
    }
    $envMap = Read-DotEnv -Path $HermesEnvFile

    # -----------------------------------------------------------------------
    # 3. Compose project name alignment
    # -----------------------------------------------------------------------
    $resolvedProject = Get-HermesProject -EnvMap $envMap -Override $Project
    Write-Ok ('Compose project name: ' + $resolvedProject)

    $stampFile = Join-Path $HermesRoot '.hermes-project-name'
    if (Test-Path -LiteralPath $stampFile) {
        $shipped = (Get-Content -LiteralPath $stampFile -Raw).Trim()
        if ($shipped -ne '' -and $shipped -ne $resolvedProject) {
            Write-Warn2 ('This bundle was built with compose project name "' + $shipped + '", this machine resolves to "' + $resolvedProject + '".')
            Write-Info2 'Docker prefixes named volumes with the project name, so the two would use'
            Write-Info2 'different volumes. Pinning COMPOSE_PROJECT_NAME in .env to match the bundle'
            Write-Info2 'so backups taken on the other machine restore into the right place.'
            Set-DotEnvValue -Path $HermesEnvFile -Key 'COMPOSE_PROJECT_NAME' -Value $shipped
            $env:COMPOSE_PROJECT_NAME = $shipped
            $resolvedProject = $shipped
            Write-Ok ('COMPOSE_PROJECT_NAME=' + $shipped + ' written to .env')
        }
    }

    if ($Project -ne '') { $env:COMPOSE_PROJECT_NAME = $Project }

    # -----------------------------------------------------------------------
    # 4. docker load
    # -----------------------------------------------------------------------
    if (-not $SkipLoad) {
        Write-Step 'docker load — this takes 2-5 minutes and is disk-bound'
        Write-Info2 'The tar expands as it loads. Make sure you have ~3x its size free.'
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('load', '-i', $TarPath)
        Write-Ok 'Images loaded'
    }

    # -----------------------------------------------------------------------
    # 5. Verify the images
    # -----------------------------------------------------------------------
    Write-Step 'Verifying the images the compose file needs'

    # Prefer the manifest ship wrote next to the tar; it is the exact list that
    # was saved. Fall back to asking compose.
    $expected = @()
    if ($TarPath -ne '') {
        $manifest = Join-Path (Split-Path -Parent $TarPath) 'IMAGES.txt'
        if (Test-Path -LiteralPath $manifest) {
            foreach ($l in (Get-Content -LiteralPath $manifest)) {
                $t = $l.Trim()
                if ($t -ne '') { $expected += $t }
            }
            if ($expected.Count -gt 0) { Write-Info2 ('using the bundle manifest: ' + $manifest) }
        }
    }
    if ($expected.Count -eq 0) { $expected = Get-ComposeImages }

    $missing = @()
    foreach ($img in $expected) {
        $ids = Get-NativeOutput -Exe 'docker' -Arguments @('image', 'inspect', $img, '--format', '{{.Id}}')
        if ($script:LastNativeExit -eq 0) {
            $short = ''
            if ($ids.Count -gt 0) { $short = ' ' + $ids[0].Substring(0, [Math]::Min(19, $ids[0].Length)) }
            Write-Ok ($img + $short)
        } else {
            Write-Bad ('MISSING  ' + $img)
            $missing += $img
        }
    }

    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Info2 'A complete offline Hermes needs all five images:'
        Write-Info2 '    hermes-core:latest                         (built here)'
        Write-Info2 '    hermes-dashboard:latest                    (built here)'
        Write-Info2 '    hermes-linkedin/sandbox:latest                      (built here, profile build-only)'
        Write-Info2 '    ghcr.io/tashfeenahmed/freellmapi:latest    (pulled)'
        Write-Info2 '    stickerdaniel/linkedin-mcp-server:4.23.2   (pulled)'
        Write-Host ''
        Write-Info2 'Fix options:'
        Write-Info2 '  a) the bundle was made with -Mode source (no tar): run .\scripts\bootstrap.ps1'
        Write-Info2 '  b) the bundle is incomplete: re-run .\scripts\ship.ps1 -Mode images on the source machine'
        Write-Info2 '  c) you have internet: docker compose --profile build-only build; docker compose pull'
        Stop-Hermes ([string]$missing.Count + ' image(s) are still missing after load; the stack cannot start offline.')
    }
    Write-Ok ('All ' + $expected.Count + ' images present')

    if ($NoUp) {
        Write-Warn2 'Stopping here (-NoUp). Start the stack with:  docker compose up -d --no-build'
        return
    }

    # -----------------------------------------------------------------------
    # 6. Up — explicitly --no-build, the whole point of loading
    # -----------------------------------------------------------------------
    $freeKey = Get-DotEnvValue -EnvMap (Read-DotEnv -Path $HermesEnvFile) -Key 'FREELLMAPI_KEY' -Default ''
    $coreDeferred = $false

    if ($freeKey -eq '' -and -not $All) {
        $coreDeferred = $true
        Write-Step 'Starting freellmapi, linkedin-mcp and hermes-dashboard'
        Write-Info2 'hermes-core is held back: FREELLMAPI_KEY is blank, so it has no LLM to talk to.'
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('compose', 'up', '-d', '--no-build', 'freellmapi', 'linkedin-mcp', 'hermes-dashboard')
    } else {
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('compose', 'up', '-d', '--no-build') -What 'Starting the stack (no build)'
    }

    Write-Step 'Container status'
    $null = Invoke-Native -Exe 'docker' -Arguments @('compose', 'ps')

    # -----------------------------------------------------------------------
    # 7. Handover
    # -----------------------------------------------------------------------
    $envMap = Read-DotEnv -Path $HermesEnvFile
    $portDash = [int](Get-DotEnvValue -EnvMap $envMap -Key 'HERMES_DASHBOARD_PORT' -Default '3000')
    $portLlm  = [int](Get-DotEnvValue -EnvMap $envMap -Key 'FREELLMAPI_PORT'       -Default '3001')
    $portApi  = [int](Get-DotEnvValue -EnvMap $envMap -Key 'HERMES_API_PORT'       -Default '8080')
    $hostBind = Get-DotEnvValue -EnvMap $envMap -Key 'HOST_BIND' -Default '127.0.0.1'
    $probeHost = $hostBind
    if ($probeHost -eq '0.0.0.0' -or $probeHost -eq '') { $probeHost = '127.0.0.1' }

    $null = Wait-HttpOk -Url ('http://' + $probeHost + ':' + $portLlm + '/api/ping') -TimeoutSec 90 -Label 'freellmapi'
    $null = Wait-HttpOk -Url ('http://' + $probeHost + ':' + $portDash + '/') -TimeoutSec 60 -Label 'dashboard'
    if (-not $coreDeferred) {
        $null = Wait-HttpOk -Url ('http://' + $probeHost + ':' + $portApi + '/api/health') -TimeoutSec 90 -Label 'hermes-core'
    }

    Write-Head 'LOADED — NOW THE PARTS ONLY YOU CAN DO'
    Write-Host ''
    Write-Host '  (1) MINT A NEW freellmapi KEY ON THIS MACHINE' -ForegroundColor Yellow
    Write-Host ('      Open  http://' + $probeHost + ':' + $portLlm)
    Write-Host '      Create the local account, add free provider keys, copy the'
    Write-Host '      freellmapi-... token into .env as FREELLMAPI_KEY, then:'
    Write-Host '          docker compose up -d --no-build hermes-core' -ForegroundColor Green
    Write-Host '      Tokens are per-instance: the old machine key will NOT work here.'
    Write-Host '      If the browser cannot self-authorise (you opened it from another device),'
    Write-Host '      the one-time setup code is printed in:  docker compose logs freellmapi'
    Write-Host ''
    Write-Host '  (2) LOG IN TO LINKEDIN, BY HAND, ON THIS MACHINE' -ForegroundColor Yellow
    Write-Host '          .\scripts\linkedin-login.ps1' -ForegroundColor Green
    Write-Host '      The session is never shipped or restored. This is deliberate: it is a live'
    Write-Host '      authenticated browser profile, and copying it is both fragile and a'
    Write-Host '      credential leak.'
    Write-Host ''
    Write-Host '  (3) OPTIONAL — BRING YOUR DATA OVER' -ForegroundColor Yellow
    Write-Host '      On the old machine:  .\scripts\backup.ps1'
    Write-Host '      Here:                .\scripts\restore.ps1 -From <backup dir>'
    Write-Host '      Skip this and Hermes just starts empty; re-import the profile.'
    Write-Host ''
    Write-Host ('  (4) OPEN HERMES:  http://' + $probeHost + ':' + $portDash) -ForegroundColor Green
    Write-Host ''
    Write-Host '  Hermes never submits an application for you. It ranks jobs, tailors a' -ForegroundColor Magenta
    Write-Host '  resume, and hands you an apply link. You click it.' -ForegroundColor Magenta
    Write-Host ''
    if ($coreDeferred) {
        Write-Warn2 'hermes-core is NOT running yet. Finish step (1), then: docker compose up -d --no-build hermes-core'
    }
    Write-Host 'Docs: README.md  |  docs\ARCHITECTURE.md  |  docs\RUNBOOK.md' -ForegroundColor Gray
    Write-Host ''

} finally {
    Pop-Location
}
