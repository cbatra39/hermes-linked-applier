#Requires -Version 5.1
<#
.SYNOPSIS
    Hermes — archive the Docker named volumes that hold your data.

.DESCRIPTION
    Backs up exactly two volumes, each into its own .tar.gz:

        <project>_hermes-data       -> hermes-data.tar.gz
            SQLite DB (profile, resumes, jobs, runs, run events, sandbox rows),
            rendered .docx/.pdf/.txt/.md files, per-run sandbox workspaces.

        <project>_freellmapi-data   -> freellmapi-data.tar.gz
            The LLM router's own store: its local account and your ENCRYPTED
            upstream provider keys.

    A Docker volume is not a host directory you can copy, so the archive is
    produced by a throwaway helper container (alpine + tar) that mounts the
    volume read-only and streams a tar into the output directory.

    ------------------------------------------------------------------------
    linkedin-session IS DELIBERATELY NOT BACKED UP. This is not an oversight.
    ------------------------------------------------------------------------
    That volume is a live, authenticated LinkedIn browser profile: session
    cookies, auth tokens, and a device fingerprint. Copying it is

      (a) FRAGILE — the fingerprint no longer matches the machine replaying it,
          so LinkedIn is more likely to invalidate the session outright or flag
          the account, and

      (b) A SECURITY RISK — those files are bearer credentials to your LinkedIn
          account, in plain form, sitting in every copy of the backup you keep.

    The supported way to have a LinkedIn session on a machine is to log in on
    that machine:  .\scripts\linkedin-login.ps1  (about one minute, once).
    This script will not copy it, and -Force does not change that.

    ------------------------------------------------------------------------
    ENCRYPTION_KEY
    ------------------------------------------------------------------------
    freellmapi encrypts the provider keys inside freellmapi-data with the
    ENCRYPTION_KEY from .env. .env is NOT part of this backup (it is a secret,
    and backups get emailed around). Restoring freellmapi-data WITHOUT the
    matching ENCRYPTION_KEY leaves undecryptable keys. Record that value
    somewhere safe — a password manager, not next to the backup.

.PARAMETER OutDir
    Destination directory. Default: <repo>\dist\backups\hermes-backup-<stamp>

.PARAMETER Project
    Compose project name override. Default: COMPOSE_PROJECT_NAME, else the
    `name:` key in docker-compose.yml (pinned to `hermes`).

.PARAMETER HelperImage
    Image used to run tar. Default alpine:3.20. On an offline machine the script
    falls back to any Hermes image already present locally.

.PARAMETER Live
    Do not stop the stack first. Faster, but the SQLite DB may be captured
    mid-write, which can produce a subtly corrupt restore. Not recommended.

.PARAMETER NoRestart
    If the stack was stopped for the snapshot, leave it stopped.

.PARAMETER Force
    Do not prompt; accept the recommended answers.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1

.EXAMPLE
    .\scripts\backup.ps1 -OutDir E:\hermes-backups\2026-09-02 -Force
#>
[CmdletBinding()]
param(
    [string]$OutDir = '',
    [string]$Project = '',
    [string]$HelperImage = 'alpine:3.20',
    [switch]$Live,
    [switch]$NoRestart,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

# Logical volume names as declared in docker-compose.yml. linkedin-session is
# absent on purpose — see the .DESCRIPTION block above.
$BackupVolumes = @('hermes-data', 'freellmapi-data')
$RefusedVolume = 'linkedin-session'

# ---------------------------------------------------------------------------
# The REAL compose project name. docker-compose.yml pins `name: hermes`, which
# beats the directory basename that Get-ComposeProjectName falls back to.
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

function Resolve-HelperImage {
    <#
        An image with `tar` in it. alpine:3.20 is the default and is pulled if
        missing; on an air-gapped machine we fall back to a Hermes image that is
        already local (all three are Debian/Alpine based and ship tar + gzip).
    #>
    param([string]$Preferred)

    $null = Get-NativeOutput -Exe 'docker' -Arguments @('image', 'inspect', $Preferred, '--format', '{{.Id}}')
    if ($script:LastNativeExit -eq 0) {
        Write-Ok ('Helper image: ' + $Preferred + ' (already local)')
        return $Preferred
    }

    Write-Step ('Pulling the helper image ' + $Preferred)
    $code = Invoke-Native -Exe 'docker' -Arguments @('pull', $Preferred)
    if ($code -eq 0) {
        Write-Ok ('Helper image: ' + $Preferred)
        return $Preferred
    }

    Write-Warn2 ('Could not pull ' + $Preferred + ' (offline?). Looking for a local image with tar in it.')
    foreach ($cand in @('hermes-core:latest', 'hermes-linkedin/sandbox:latest', 'hermes-dashboard:latest', 'alpine:latest')) {
        $null = Get-NativeOutput -Exe 'docker' -Arguments @('image', 'inspect', $cand, '--format', '{{.Id}}')
        if ($script:LastNativeExit -eq 0) {
            Write-Ok ('Helper image: ' + $cand + ' (fallback)')
            return $cand
        }
    }

    Stop-Hermes ('No usable helper image. Either get network access so `docker pull ' + $Preferred + '` works, or pass -HelperImage with the name of a local image that contains tar (`docker image ls` to see what you have).')
}

Write-Head 'HERMES BACKUP — data volumes'

Push-Location $HermesRoot
try {
    $null = Assert-DockerReady

    $envMap  = Read-DotEnv -Path $HermesEnvFile
    $project = Get-HermesProject -EnvMap $envMap -Override $Project
    Write-Ok ('Compose project name: ' + $project)

    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    if ($OutDir -eq '') {
        $OutDir = Join-Path $HermesRoot ('dist\backups\hermes-backup-' + $stamp)
    }
    if (-not (Test-Path -LiteralPath $OutDir)) {
        $null = New-Item -ItemType Directory -Path $OutDir -Force
    }
    $OutDir = (Resolve-Path -LiteralPath $OutDir).Path
    Write-Ok ('Output directory: ' + $OutDir)

    # -----------------------------------------------------------------------
    # Resolve the volumes. A missing volume is not fatal: on a fresh machine
    # freellmapi-data may not exist yet.
    # -----------------------------------------------------------------------
    Write-Step 'Resolving Docker volumes'
    $plan = @()
    foreach ($logical in $BackupVolumes) {
        $real = Resolve-DockerVolume -Logical $logical -Project $project
        if ($real) {
            Write-Ok ($logical + '  ->  ' + $real)
            $plan += @{ Logical = $logical; Real = $real; File = ($logical + '.tar.gz') }
        } else {
            Write-Warn2 ($logical + ': no such volume (expected ' + $project + '_' + $logical + '). Nothing to back up for it — has the stack ever been started?')
        }
    }
    if ($plan.Count -eq 0) {
        Stop-Hermes ('Neither ' + ($BackupVolumes -join ' nor ') + ' exists for project "' + $project + '". Start the stack once (docker compose up -d) before backing it up, or pass -Project if your project name differs.')
    }

    $sessionVol = Resolve-DockerVolume -Logical $RefusedVolume -Project $project
    if ($sessionVol) {
        Write-Warn2 ($RefusedVolume + ' (' + $sessionVol + ') exists and is being SKIPPED on purpose.')
        Write-Info2 'It is a live authenticated LinkedIn browser profile. Copying it between'
        Write-Info2 'machines is fragile (device fingerprint mismatch -> session invalidated or'
        Write-Info2 'account flagged) AND a security risk (the files are bearer credentials to'
        Write-Info2 'your LinkedIn account). Re-run scripts\linkedin-login.ps1 on the target'
        Write-Info2 'machine instead. There is no flag to override this.'
    }

    # -----------------------------------------------------------------------
    # Quiesce the stack so SQLite is not captured mid-write.
    # -----------------------------------------------------------------------
    $stoppedByUs = $false
    $stackUp = Test-StackHasContainers
    $running = @()
    if ($stackUp) {
        # `docker compose ps -q` (without -a) lists only RUNNING service
        # containers on every Compose v2 release; --status/--format templates are
        # not consistently available across versions, so do not rely on them.
        $running = Get-NativeOutput -Exe 'docker' -Arguments @('compose', 'ps', '-q')
        if ($script:LastNativeExit -ne 0) { $running = @() }
        $running = @($running | Where-Object { $_.Trim() -ne '' })
    }

    if ($running.Count -gt 0) {
        if ($Live) {
            Write-Warn2 'Backing up with the stack RUNNING (-Live).'
            Write-Info2 'hermes-core writes SQLite in WAL mode, so this snapshot can land between a'
            Write-Info2 'commit and its WAL checkpoint. The restore usually works and occasionally'
            Write-Info2 'loses the last few writes. Do not use -Live for a migration you care about.'
        } else {
            Write-Step ('Stopping ' + $running.Count + ' running container(s) for a consistent snapshot')
            $doStop = $true
            if (-not $Force) {
                $doStop = Read-YesNo -Question 'Stop the Hermes containers while the snapshot is taken (recommended)?' -DefaultYes $true
            }
            if ($doStop) {
                $null = Invoke-NativeChecked -Exe 'docker' -Arguments @('compose', 'stop')
                $stoppedByUs = $true
                Write-Ok 'Stack stopped (containers kept, volumes untouched)'
            } else {
                Write-Warn2 'Continuing with the stack running; see the -Live caveat above.'
            }
        }
    } else {
        Write-Ok 'No running containers — snapshot will be consistent.'
    }

    try {
        $helper = Resolve-HelperImage -Preferred $HelperImage
        $hostOut = ConvertTo-DockerPath -Path $OutDir

        # -------------------------------------------------------------------
        # Archive each volume.
        #
        # Why a bind mount rather than streaming to stdout: Windows PowerShell
        # 5.1 pipes native output through its TEXT pipeline, so `docker run ... >
        # file` CORRUPTS binary output (re-encoding + CRLF translation). Writing
        # inside the container onto a bind-mounted host directory keeps the bytes
        # exact. (scripts/backup.sh does the opposite for the opposite reason:
        # Git Bash rewrites POSIX-looking -v paths.)
        # -------------------------------------------------------------------
        $results = @()
        foreach ($item in $plan) {
            $target = Join-Path $OutDir $item.File
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }

            Write-Step ('Archiving ' + $item.Real + '  ->  ' + $item.File)
            $inner = 'set -e; tar -czf "/backup/' + $item.File + '" -C /v . ; ls -l "/backup/' + $item.File + '"'
            $args1 = @(
                'run', '--rm',
                '-v', ($item.Real + ':/v:ro'),
                '-v', ($hostOut + ':/backup'),
                $helper, 'sh', '-c', $inner
            )
            $null = Invoke-NativeChecked -Exe 'docker' -Arguments $args1

            if (-not (Test-Path -LiteralPath $target)) {
                Stop-Hermes ('tar reported success but ' + $target + ' does not exist. If the output directory is on a network share or an unshared drive, Docker Desktop could not write to it — pass -OutDir on a local, file-shared drive.')
            }
            $size = (Get-Item -LiteralPath $target).Length
            if ($size -lt 100) {
                Write-Warn2 ($item.File + ' is only ' + $size + ' bytes — the volume is probably empty.')
            }
            Write-Ok ($item.File + '  ' + (Format-Bytes -Bytes $size))

            # Verify the archive is readable and count its entries.
            $verify = Get-NativeOutput -Exe 'docker' -Arguments @(
                'run', '--rm', '-v', ($hostOut + ':/backup:ro'), $helper,
                'sh', '-c', ('tar -tzf "/backup/' + $item.File + '" | wc -l')
            )
            $entries = 'unknown'
            if ($script:LastNativeExit -eq 0 -and $verify.Count -gt 0) { $entries = $verify[0].Trim() }
            if ($entries -eq 'unknown') {
                Write-Warn2 ($item.File + ': could not verify the archive listing.')
            } else {
                Write-Ok ($item.File + ': verified, ' + $entries + ' entries')
            }

            $results += @{ Logical = $item.Logical; Real = $item.Real; File = $item.File; Bytes = $size; Entries = $entries }
        }
    } finally {
        if ($stoppedByUs) {
            if ($NoRestart) {
                Write-Warn2 'Stack left stopped (-NoRestart). Start it with: docker compose up -d'
            } else {
                Write-Step 'Restarting the stack'
                $code = Invoke-Native -Exe 'docker' -Arguments @('compose', 'start')
                if ($code -ne 0) {
                    Write-Warn2 '`docker compose start` failed; try: docker compose up -d'
                } else {
                    Write-Ok 'Stack restarted'
                }
            }
        }
    }

    # -----------------------------------------------------------------------
    # MANIFEST.txt — a backup nobody can interpret is not a backup.
    # -----------------------------------------------------------------------
    Write-Step 'Writing MANIFEST.txt'
    $enc = New-Object System.Text.UTF8Encoding($false)
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('=============================================================================')
    $lines.Add(' HERMES VOLUME BACKUP')
    $lines.Add('=============================================================================')
    $lines.Add('created        : ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
    $lines.Add('source host    : ' + $env:COMPUTERNAME)
    $lines.Add('project name   : ' + $project)
    $lines.Add('stack quiesced : ' + $(if ($stoppedByUs) { 'yes (containers stopped for the snapshot)' } else { 'NO - taken live; the SQLite DB may be mid-write' }))
    $lines.Add('')
    $lines.Add('CONTENTS')
    foreach ($r in $results) {
        $lines.Add(('  {0,-24} {1,-28} {2,12}  {3} entries' -f $r.File, $r.Real, (Format-Bytes -Bytes $r.Bytes), $r.Entries))
    }
    $lines.Add('')
    $lines.Add('NOT INCLUDED, ON PURPOSE')
    $lines.Add('  ' + $project + '_' + $RefusedVolume)
    $lines.Add('      A live authenticated LinkedIn browser profile (cookies, tokens, device')
    $lines.Add('      fingerprint). Never copy it between machines: the fingerprint mismatch')
    $lines.Add('      makes LinkedIn more likely to invalidate the session or flag the account,')
    $lines.Add('      and the files are bearer credentials to your account in plain form.')
    $lines.Add('      On the target machine run:  scripts\linkedin-login.ps1')
    $lines.Add('  .env')
    $lines.Add('      Holds ENCRYPTION_KEY and FREELLMAPI_KEY. Secrets do not travel in a')
    $lines.Add('      backup archive.')
    $lines.Add('')
    $lines.Add('YOU MUST ALSO RECORD, SEPARATELY:')
    $lines.Add('      ENCRYPTION_KEY  (from .env on this machine)')
    $lines.Add('      freellmapi encrypted the provider keys inside freellmapi-data with it.')
    $lines.Add('      Restore that volume without the same ENCRYPTION_KEY and those keys are')
    $lines.Add('      unreadable — you would have to re-add them in the router dashboard.')
    $lines.Add('      Put it in a password manager, not in this folder.')
    $lines.Add('')
    $lines.Add('RESTORE')
    $lines.Add('      Windows :  .\scripts\restore.ps1 -From "' + $OutDir + '"')
    $lines.Add('      Linux   :  ./scripts/restore.sh --from <this directory>')
    $lines.Add('      The stack must be down; restore.ps1 brings it down for you.')
    $lines.Add('')
    $lines.Add('TREAT THIS FOLDER LIKE A PASSWORD FILE. It contains your scraped LinkedIn')
    $lines.Add('profile, your resumes, and your encrypted LLM provider keys.')
    $lines.Add('=============================================================================')

    $manifestPath = Join-Path $OutDir 'MANIFEST.txt'
    [System.IO.File]::WriteAllLines($manifestPath, $lines.ToArray(), $enc)
    Write-Ok ('Wrote ' + $manifestPath)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    $total = (Get-ChildItem -LiteralPath $OutDir -File | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $total) { $total = 0 }

    Write-Head 'BACKUP COMPLETE'
    Write-Host ('  Location : ' + $OutDir)
    Write-Host ('  Size     : ' + (Format-Bytes -Bytes $total))
    Write-Host ('  Volumes  : ' + (($results | ForEach-Object { $_.Logical }) -join ', '))
    Write-Host ''
    Write-Host '  Restore with:' -ForegroundColor Green
    Write-Host ('    .\scripts\restore.ps1 -From "' + $OutDir + '"') -ForegroundColor Green
    Write-Host ''
    Write-Host '  Record your ENCRYPTION_KEY from .env somewhere safe, or the restored' -ForegroundColor Yellow
    Write-Host '  freellmapi provider keys will be undecryptable.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host ('  ' + $RefusedVolume + ' was NOT copied, deliberately. Re-login on the target machine:') -ForegroundColor Magenta
    Write-Host '    .\scripts\linkedin-login.ps1' -ForegroundColor Magenta
    Write-Host ''

} finally {
    Pop-Location
}
