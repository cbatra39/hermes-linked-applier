#Requires -Version 5.1
<#
.SYNOPSIS
    Hermes — build a portable bundle in .\dist for moving to another laptop.

.DESCRIPTION
    Two modes, and the choice is a real tradeoff:

      -Mode images  (default)  "works offline, big and slow to produce"
          docker save's every image the stack uses into one tar, plus a copy of
          the repo. The target machine needs Docker but no internet, no registry
          access, and no build toolchain. Expect roughly 3-6 GB and 5-15 minutes
          to write, mostly disk-bound. Restore is ~2-5 minutes of `docker load`.
          Pick this for an air-gapped machine, a slow/metered link, or when you
          want byte-identical images to the ones you tested.

      -Mode source             "small and fast, but rebuilds on arrival"
          Repo copy only, no image tar. Roughly 1-5 MB. The target machine
          rebuilds from the Dockerfiles, so it needs internet to pull base images
          and packages, and 5-15 minutes of build time. The rebuilt images are
          not guaranteed byte-identical (upstream base images and package
          versions move). Pick this for a normal machine on a decent connection.

    Never included: .env (it holds your freellmapi key), the SQLite database,
    node_modules, __pycache__, dist. Docker named volumes are NOT part of this
    bundle — use scripts\backup.ps1 for hermes-data and freellmapi-data.

    The `linkedin-session` volume is deliberately never shipped. See README.

.PARAMETER Mode
    'images' (default) or 'source'.

.PARAMETER OutDir
    Bundle destination. Default: <repo>\dist

.PARAMETER Force
    Overwrite a non-empty output directory without asking.

.PARAMETER Zip
    Also produce a .zip of the bundle. Skipped by default in images mode:
    Compress-Archive in Windows PowerShell 5.1 struggles past ~2 GB, and a
    docker save tar barely compresses (the layers are already gzipped).

.EXAMPLE
    .\scripts\ship.ps1

.EXAMPLE
    .\scripts\ship.ps1 -Mode source -Zip
#>
[CmdletBinding()]
param(
    [ValidateSet('images', 'source')]
    [string]$Mode = 'images',
    [string]$OutDir = '',
    [switch]$Force,
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Head ('HERMES SHIP — mode: ' + $Mode)

Push-Location $HermesRoot
try {
    $null = Assert-DockerReady

    if ($OutDir -eq '') { $OutDir = Join-Path $HermesRoot 'dist' }
    $repoOut = Join-Path $OutDir 'repo'
    $tarPath = Join-Path $OutDir 'hermes-images.tar'
    $project = Get-ComposeProjectName

    # -----------------------------------------------------------------------
    # Prepare the output directory
    # -----------------------------------------------------------------------
    if (Test-Path -LiteralPath $OutDir) {
        $existing = @(Get-ChildItem -LiteralPath $OutDir -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) {
            if (-not $Force) {
                Write-Warn2 ($OutDir + ' is not empty.')
                if (-not (Read-YesNo -Question 'Delete its contents and rebuild the bundle?' -DefaultYes $false)) {
                    Stop-Hermes 'Aborted: output directory not empty. Re-run with -Force or pass -OutDir <other path>.'
                }
            }
            Write-Step ('Clearing ' + $OutDir)
            Get-ChildItem -LiteralPath $OutDir -Force | Remove-Item -Recurse -Force
        }
    } else {
        $null = New-Item -ItemType Directory -Path $OutDir -Force
    }
    Write-Ok ('Bundle directory: ' + $OutDir)

    # -----------------------------------------------------------------------
    # Copy the repo (robocopy: reliable excludes, no symlink games)
    # -----------------------------------------------------------------------
    Write-Step 'Copying repository'
    $null = New-Item -ItemType Directory -Path $repoOut -Force

    $excludeDirs = @('dist', 'node_modules', '__pycache__', '.git', '.venv', 'venv',
                     '.pytest_cache', '.mypy_cache', '.ruff_cache', 'data', '.idea', '.vscode')
    $excludeFiles = @('.env', '*.pyc', '*.pyo', '*.log', '*.tar', '*.sqlite', '*.sqlite3',
                      '*.db', '*.db-wal', '*.db-shm')

    $rcArgs = @($HermesRoot, $repoOut, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
    $rcArgs += '/XD'
    foreach ($d in $excludeDirs) { $rcArgs += $d }
    $rcArgs += '/XF'
    foreach ($f in $excludeFiles) { $rcArgs += $f }

    $rc = Invoke-Native -Exe 'robocopy' -Arguments $rcArgs
    # robocopy: 0-7 are success codes (8+ means at least one copy failed)
    if ($rc -ge 8) {
        Stop-Hermes ('robocopy failed with exit code ' + $rc + '. Check for locked files (stop the stack and retry).')
    }
    Write-Ok 'Repository copied (.env, data, node_modules, __pycache__ excluded)'

    # Paranoia: prove .env did not slip through.
    $leakedEnv = Join-Path $repoOut '.env'
    if (Test-Path -LiteralPath $leakedEnv) {
        Remove-Item -LiteralPath $leakedEnv -Force
        Write-Warn2 'Removed a .env that slipped into the bundle.'
    }

    # Record the compose project name. Compose derives built image names from the
    # project directory basename, so the target must match it or set
    # COMPOSE_PROJECT_NAME — scripts\load.ps1 reads this file and handles it.
    $projFile = Join-Path $repoOut '.hermes-project-name'
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($projFile, $project, $enc)
    Write-Ok ('Recorded compose project name: ' + $project)

    # -----------------------------------------------------------------------
    # Images
    # -----------------------------------------------------------------------
    $images = @()
    if ($Mode -eq 'images') {
        Write-Step 'Resolving the image list'
        $images = Get-ComposeImages
        foreach ($i in $images) { Write-Info2 $i }

        Write-Step 'Verifying every image exists locally'
        $missing = @()
        foreach ($img in $images) {
            $null = Get-NativeOutput -Exe 'docker' -Arguments @('image', 'inspect', $img, '--format', '{{.Id}}')
            if ($script:LastNativeExit -ne 0) { $missing += $img }
        }
        if ($missing.Count -gt 0) {
            Write-Host ''
            foreach ($m in $missing) { Write-Bad ('missing image: ' + $m) }
            Write-Info2 'Build/pull them first:'
            Write-Info2 '    docker compose build'
            Write-Info2 '    docker compose --profile build-only build'
            Write-Info2 '    docker compose pull'
            Write-Info2 'Or ship source-only:  .\scripts\ship.ps1 -Mode source'
            Stop-Hermes ('Cannot save images: ' + $missing.Count + ' missing.')
        }
        Write-Ok ('All ' + $images.Count + ' images present')

        Write-Step ('Writing ' + $tarPath + '  (this is the slow part — several GB)')
        $saveArgs = @('save', '-o', $tarPath)
        foreach ($img in $images) { $saveArgs += $img }
        $null = Invoke-NativeChecked -Exe 'docker' -Arguments $saveArgs

        $tarInfo = Get-Item -LiteralPath $tarPath
        Write-Ok ('Image tar written: ' + (Format-Bytes -Bytes $tarInfo.Length))

        $manifest = Join-Path $OutDir 'IMAGES.txt'
        [System.IO.File]::WriteAllLines($manifest, $images, $enc)
    } else {
        Write-Warn2 'Source-only mode: no image tar. The target machine will rebuild from the Dockerfiles.'
        $images = Get-ComposeImages
    }

    # -----------------------------------------------------------------------
    # RESTORE.txt
    # -----------------------------------------------------------------------
    Write-Step 'Writing RESTORE.txt'

    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $imageLines = ''
    foreach ($i in $images) { $imageLines = $imageLines + '    ' + $i + "`r`n" }

    if ($Mode -eq 'images') {
        $modeBlock = @"
THIS BUNDLE CONTAINS PREBUILT IMAGES
    hermes-images.tar   all $($images.Count) images, exactly as tested
    IMAGES.txt          the image list
    repo\               source, compose file, scripts, docs

The target machine needs Docker. It does NOT need internet access, registry
credentials, or a build toolchain.

STEPS ON THE NEW MACHINE
------------------------
 1. Install Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 (Linux).
    Start it and wait until the engine is actually up.

 2. Copy this whole bundle directory to the new machine.

 3. Put the repo somewhere permanent, keeping the directory name:

        C:\Users\<you>\hermes-linkedin-applier

    THE DIRECTORY NAME MATTERS. Compose names locally built images
    <project>-<service>, and the project name defaults to the directory
    basename. This bundle was built with project name:

        $project

    If you rename the directory, Compose will look for images that do not
    exist and try to rebuild. scripts\load.ps1 detects this and writes
    COMPOSE_PROJECT_NAME into .env for you, so either name it the same or
    just let load.ps1 handle it.

 4. Load the images (2-5 minutes):

        cd <repo>
        powershell -ExecutionPolicy Bypass -File .\scripts\load.ps1

    load.ps1 finds hermes-images.tar next to the repo, runs `docker load`,
    creates .env, generates ENCRYPTION_KEY, and brings the stack up
    WITHOUT rebuilding.

    Manual equivalent:
        docker load -i ..\hermes-images.tar
        copy .env.example .env
        # set ENCRYPTION_KEY to 64 random hex chars
        docker compose up -d --no-build

 5. Do the manual steps. They are per-machine and cannot be copied:

    a. freellmapi key — open http://localhost:3001 , create the local
       account, add free provider keys (Google AI Studio, Groq, Cerebras,
       OpenRouter, Mistral, GitHub Models), copy the freellmapi-... key,
       paste it into .env as FREELLMAPI_KEY, then:
           docker compose up -d hermes-core

       If the browser cannot self-authorise (e.g. you opened it from another
       device), the one-time setup code is in:
           docker compose logs freellmapi

    b. LinkedIn login — run:
           .\scripts\linkedin-login.ps1
       then open http://localhost:6080/vnc.html and sign in BY HAND
       (password, 2FA, captcha). This must be redone on every machine.

 6. Open Hermes:  http://localhost:3000
"@
    } else {
        $modeBlock = @"
THIS BUNDLE IS SOURCE-ONLY
    repo\               source, compose file, scripts, docs
    (no image tar)

The target machine needs Docker AND internet access: it pulls the two upstream
images and builds hermes-core, hermes-dashboard and hermes-sandbox from the
Dockerfiles. Budget 5-15 minutes for the first build.

Tradeoff vs. the images bundle: this is ~1000x smaller, but it rebuilds on
arrival, needs a network, and the resulting images are not guaranteed
byte-identical to the ones tested here (upstream base images and package
versions move on). If that matters, re-run with -Mode images.

STEPS ON THE NEW MACHINE
------------------------
 1. Install Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 (Linux).
    Start it and wait until the engine is actually up.

 2. Copy repo\ to a permanent location, e.g.
        C:\Users\<you>\hermes-linkedin-applier

 3. Build and start everything:

        cd <repo>
        powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

    bootstrap.ps1 runs preflight, creates .env, generates ENCRYPTION_KEY,
    builds all images (including the build-only sandbox image), and starts
    the stack.

 4. Do the manual steps. They are per-machine and cannot be copied:

    a. freellmapi key — open http://localhost:3001 , create the local
       account, add free provider keys (Google AI Studio, Groq, Cerebras,
       OpenRouter, Mistral, GitHub Models), copy the freellmapi-... key,
       paste it into .env as FREELLMAPI_KEY, then:
           docker compose up -d hermes-core

       If the browser cannot self-authorise, the one-time setup code is in:
           docker compose logs freellmapi

    b. LinkedIn login — run:
           .\scripts\linkedin-login.ps1
       then open http://localhost:6080/vnc.html and sign in BY HAND
       (password, 2FA, captcha). This must be redone on every machine.

 5. Open Hermes:  http://localhost:3000

Images this project uses (for reference):
$imageLines
"@
    }

    $restore = @"
=============================================================================
 HERMES — RESTORE INSTRUCTIONS
 bundle built: $stamp
 mode:         $Mode
 source host:  $env:COMPUTERNAME
 project name: $project
=============================================================================

$modeBlock

-----------------------------------------------------------------------------
WHAT IS DELIBERATELY NOT IN THIS BUNDLE
-----------------------------------------------------------------------------

 .env
     Holds your FREELLMAPI_KEY and ENCRYPTION_KEY. Secrets do not travel in a
     bundle you might email to yourself. .env.example is included; recreate
     .env on the target (bootstrap.ps1 / load.ps1 do it for you).

 Docker named volumes (your data)
     hermes-data       SQLite DB: profile, resumes, jobs, runs + rendered files
     freellmapi-data   your provider keys and the freellmapi account
     Move these ONLY if you want your history and provider config to come with
     you, and use the dedicated tooling:
         old machine:  .\scripts\backup.ps1
         new machine:  .\scripts\restore.ps1 -From <backup dir>
     If you skip this, Hermes starts empty: re-import the profile, re-add the
     provider keys. That is a perfectly normal way to move.

 linkedin-session          <-- DO NOT COPY THIS ONE
     A live, authenticated LinkedIn browser profile: cookies, tokens, device
     fingerprint. Copying it between machines is
       (a) fragile  — the fingerprint no longer matches the new host, so
                      LinkedIn is more likely to invalidate the session or
                      flag the account, and
       (b) a security risk — the files are bearer credentials to your LinkedIn
                      account in plain form on disk and in your backups.
     Just run scripts\linkedin-login.ps1 on the new machine and sign in again.
     It takes a minute and is the supported path. backup.ps1 refuses to
     include it.

-----------------------------------------------------------------------------
TROUBLESHOOTING THE RESTORE
-----------------------------------------------------------------------------

 "compose is rebuilding even though I loaded the images"
     The project name does not match. Set COMPOSE_PROJECT_NAME=$project in
     .env, or rename the repo directory, then: docker compose up -d --no-build

 "docker load" says "no space left on device"
     The tar expands. Free at least 3x the tar size, or use -Mode source.

 "unauthorized" / 401 from the LLM
     FREELLMAPI_KEY is missing, stale, or from the other machine's freellmapi.
     Keys are per-freellmapi-instance: mint a new one in the new dashboard.

 MCP says "not authenticated"
     Expected on a fresh machine. Run scripts\linkedin-login.ps1.

 Ports 3000/3001/8080/6080 in use
     Change them in .env and re-run, or stop whatever owns them.

 Full reference: README.md , docs\RUNBOOK.md
=============================================================================
"@

    $restorePath = Join-Path $OutDir 'RESTORE.txt'
    [System.IO.File]::WriteAllText($restorePath, $restore, $enc)
    Write-Ok ('Wrote ' + $restorePath)

    # -----------------------------------------------------------------------
    # Optional zip
    # -----------------------------------------------------------------------
    if ($Zip) {
        if ($Mode -eq 'images') {
            Write-Warn2 'Zipping an images bundle: Compress-Archive in PowerShell 5.1 is unreliable above ~2 GB, and docker save tars barely compress. Consider shipping the folder as-is.'
        }
        Write-Step 'Compressing the bundle'
        $zipPath = Join-Path (Split-Path -Parent $OutDir) ((Split-Path -Leaf $OutDir) + '-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.zip')
        try {
            Compress-Archive -Path (Join-Path $OutDir '*') -DestinationPath $zipPath -CompressionLevel Optimal
            $zi = Get-Item -LiteralPath $zipPath
            Write-Ok ('Zip: ' + $zipPath + '  (' + (Format-Bytes -Bytes $zi.Length) + ')')
        } catch {
            Write-Warn2 ('Zip failed: ' + $_.Exception.Message)
            Write-Info2 'Copy the bundle folder directly instead — the zip is only a convenience.'
        }
    }

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    $total = (Get-ChildItem -LiteralPath $OutDir -Recurse -Force -File | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $total) { $total = 0 }

    Write-Head 'BUNDLE READY'
    Write-Host ('  Location: ' + $OutDir)
    Write-Host ('  Mode:     ' + $Mode)
    Write-Host ('  Size:     ' + (Format-Bytes -Bytes $total))
    Write-Host ''
    Write-Host '  Next:' -ForegroundColor Yellow
    Write-Host '    1. Copy this whole folder to the target machine (USB or file share).'
    if ($Mode -eq 'images') {
        Write-Host '    2. On the target:  cd <repo>  then  .\scripts\load.ps1' -ForegroundColor Green
    } else {
        Write-Host '    2. On the target:  cd <repo>  then  .\scripts\bootstrap.ps1' -ForegroundColor Green
    }
    Write-Host '    3. Read RESTORE.txt — it lists the manual per-machine steps.'
    Write-Host ''
    Write-Host '  Your data (SQLite DB, provider keys) is NOT in this bundle.' -ForegroundColor Yellow
    Write-Host '  If you want it: .\scripts\backup.ps1 here, .\scripts\restore.ps1 there.' -ForegroundColor Yellow
    Write-Host '  Never copy the linkedin-session volume — re-login instead.' -ForegroundColor Magenta
    Write-Host ''

} finally {
    Pop-Location
}
