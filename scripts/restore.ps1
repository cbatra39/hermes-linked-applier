#Requires -Version 5.1
<#
.SYNOPSIS
    Hermes — restore the Docker named volumes that hold your data, from a
    backup produced by scripts\backup.ps1 (or scripts/backup.sh).

.DESCRIPTION
    The exact counterpart of scripts\backup.ps1. It reads a backup DIRECTORY
    (not a single tar) laid out the way backup.ps1 writes one:

        <backup dir>\
            hermes-data.tar.gz        -> <project>_hermes-data
            freellmapi-data.tar.gz    -> <project>_freellmapi-data
            MANIFEST.txt              (informational; written by backup)

    A Docker volume is not a host directory you can copy into, so the extract
    runs inside a throwaway helper container (alpine + tar) with the volume
    mounted read-write and this backup directory mounted READ-ONLY at /backup.

    ------------------------------------------------------------------------
    THIS IS DESTRUCTIVE. Each restored volume is EMPTIED first.
    ------------------------------------------------------------------------
    Restoring hermes-data replaces your SQLite DB, your generated resumes and
    your sandbox workspaces with the ones in the archive. There is no merge and
    no undo. You are asked to confirm per volume when the target volume is not
    empty; -Force skips those prompts.

    ------------------------------------------------------------------------
    linkedin-session IS NEVER RESTORED. There is no flag to override this.
    ------------------------------------------------------------------------
    backup.ps1 refuses to copy it, and this script refuses to write it even if
    you hand it a linkedin-session.tar.gz from somewhere else. That volume is a
    live, authenticated LinkedIn browser profile: session cookies, auth tokens
    and a device fingerprint. Moving it between machines is

      (a) FRAGILE — the fingerprint no longer matches the machine replaying it,
          so LinkedIn is more likely to invalidate the session outright or flag
          the account, and

      (b) A SECURITY RISK — those files are bearer credentials to your LinkedIn
          account, in plain form, in every copy of the backup you keep.

    The supported way to have a LinkedIn session on a machine is to log in on
    that machine:  .\scripts\linkedin-login.ps1  (about one minute, once).

    ------------------------------------------------------------------------
    ENCRYPTION_KEY
    ------------------------------------------------------------------------
    freellmapi encrypted the provider keys inside freellmapi-data with the
    ENCRYPTION_KEY from the SOURCE machine's .env. .env is not part of a backup.
    If the ENCRYPTION_KEY in this machine's .env differs, the restored provider
    keys are undecryptable and you must re-add them in the router's dashboard
    (http://localhost:3001). This script compares the two where it can and
    warns you.

    ------------------------------------------------------------------------
    THE STACK MUST BE DOWN
    ------------------------------------------------------------------------
    Restoring a volume out from under a running container gives you a container
    holding deleted inodes and, for SQLite, a good chance of a corrupt database.
    This script REFUSES to run while any container of this compose project is
    running, or while any container anywhere is using a target volume. Run
    `make down` (or `docker compose down`) first, or pass -Down to have this
    script bring the stack down for you.

.PARAMETER From
    The backup DIRECTORY to restore from. Default: the newest
    `hermes-backup-*` directory under <repo>\dist\backups.

.PARAMETER Project
    Compose project name override. Default: COMPOSE_PROJECT_NAME, else the
    `name:` key in docker-compose.yml, else the directory basename.

.PARAMETER HelperImage
    Image used to run tar. Default alpine:3.20. On an offline machine the script
    falls back to any Hermes image already present locally.

.PARAMETER Down
    Bring the stack down (`docker compose down --remove-orphans`) instead of
    refusing when containers are running. Volumes are kept — `down` never
    removes them without -v.

.PARAMETER Force
    Do not prompt before emptying a non-empty target volume. Does NOT make this
    script restore linkedin-session.

.PARAMETER DryRun
    Validate everything — archives, project name, volumes, running containers —
    and print the plan, but write nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1

.EXAMPLE
    .\scripts\restore.ps1 -From E:\hermes-backups\hermes-backup-20260902-101500 -Force
#>
[CmdletBinding()]
param(
    [string]$From = '',
    [string]$Project = '',
    [string]$HelperImage = 'alpine:3.20',
    [switch]$Down,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\common.ps1')

# Logical volume names as declared in docker-compose.yml, in restore order.
# linkedin-session is absent on purpose — see the .DESCRIPTION block.
$RestoreVolumes = @('hermes-data', 'freellmapi-data')
$RefusedVolume  = 'linkedin-session'

# Entry names that prove a hermes-data.tar.gz really came from Hermes. The
# archive is created with `tar -czf - -C /v .`, so paths look like `./hermes.db`.
$HermesDataMarkers = @('hermes.db', 'resumes', 'uploads', 'workspaces', 'renders')

# ---------------------------------------------------------------------------
# The REAL compose project name.
#
# Same helper as backup.ps1 / load.ps1: docker-compose.yml pins a `name:` key,
# and per the Compose Specification that beats the directory basename which
# Get-ComposeProjectName falls back to. Precedence:
#     -Project > COMPOSE_PROJECT_NAME (env) > COMPOSE_PROJECT_NAME (.env)
#     > `name:` in docker-compose.yml > directory basename
# Named volumes are prefixed with the winner, so getting this wrong means
# writing into the wrong project's volumes.
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
        already local (all of them ship tar + gzip).
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

function Find-LatestBackup {
    <# Newest <repo>\dist\backups\hermes-backup-* directory, or '' if none. #>
    $root = Join-Path $HermesRoot 'dist\backups'
    if (-not (Test-Path -LiteralPath $root)) { return '' }
    $dirs = @(Get-ChildItem -LiteralPath $root -Directory -Filter 'hermes-backup-*' -ErrorAction SilentlyContinue |
              Sort-Object -Property Name -Descending)
    if ($dirs.Count -eq 0) { return '' }
    return $dirs[0].FullName
}

function Get-ArchiveEntryCount {
    <#
        Number of entries in a .tar.gz inside the (read-only) /backup mount.
        Returns -1 when the listing failed, which means the archive is not a
        readable gzip tar.
    #>
    param(
        [string]$Helper,
        [string]$HostDir,
        [string]$FileName
    )
    $inner = 'tar -tzf "/backup/' + $FileName + '" | wc -l'
    $out = @(Get-NativeOutput -Exe 'docker' -Arguments @(
        'run', '--rm', '-v', ($HostDir + ':/backup:ro'), $Helper, 'sh', '-c', $inner
    ))
    if ($script:LastNativeExit -ne 0) { return -1 }
    if ($out.Count -eq 0) { return -1 }
    $n = 0
    if ([int]::TryParse($out[0].Trim(), [ref]$n)) { return $n }
    return -1
}

function Test-ArchiveMarkers {
    <# True when the archive listing contains at least one of $Markers. #>
    param(
        [string]$Helper,
        [string]$HostDir,
        [string]$FileName,
        [string[]]$Markers
    )
    $inner = 'tar -tzf "/backup/' + $FileName + '" 2>/dev/null | head -n 400'
    $out = @(Get-NativeOutput -Exe 'docker' -Arguments @(
        'run', '--rm', '-v', ($HostDir + ':/backup:ro'), $Helper, 'sh', '-c', $inner
    ))
    if ($script:LastNativeExit -ne 0) { return $false }
    foreach ($line in $out) {
        $entry = $line.Trim()
        if ($entry -eq '') { continue }
        # './hermes.db' -> 'hermes.db' ; './resumes/x.docx' -> 'resumes'
        $entry = $entry -replace '^\./', ''
        $entry = $entry.TrimEnd('/')
        $firstSeg = ($entry -split '/')[0]
        foreach ($m in $Markers) {
            if ($firstSeg -eq $m) { return $true }
        }
    }
    return $false
}

function Get-VolumeEntryCount {
    <# Number of top-level entries (dotfiles included) in a volume. -1 on error. #>
    param([string]$Helper, [string]$Volume)
    $out = @(Get-NativeOutput -Exe 'docker' -Arguments @(
        'run', '--rm', '-v', ($Volume + ':/v:ro'), $Helper, 'sh', '-c', 'ls -A /v | wc -l'
    ))
    if ($script:LastNativeExit -ne 0) { return -1 }
    if ($out.Count -eq 0) { return -1 }
    $n = 0
    if ([int]::TryParse($out[0].Trim(), [ref]$n)) { return $n }
    return -1
}

function Test-VolumeExists {
    param([string]$Name)
    $null = Get-NativeOutput -Exe 'docker' -Arguments @('volume', 'inspect', $Name)
    if ($script:LastNativeExit -eq 0) { return $true }
    return $false
}

function Get-ContainersUsingVolume {
    <# Ids of RUNNING containers that have $Name mounted. #>
    param([string]$Name)
    $out = @(Get-NativeOutput -Exe 'docker' -Arguments @('ps', '-q', '--filter', ('volume=' + $Name)))
    if ($script:LastNativeExit -ne 0) { return @() }
    return @($out | Where-Object { $_.Trim() -ne '' })
}

Write-Head 'HERMES RESTORE — data volumes'

Push-Location $HermesRoot
try {
    $null = Assert-DockerReady

    $envMap  = Read-DotEnv -Path $HermesEnvFile
    $project = Get-HermesProject -EnvMap $envMap -Override $Project
    Write-Ok ('Compose project name: ' + $project)

    # -----------------------------------------------------------------------
    # 1. Locate and validate the backup directory. Nothing is touched until
    #    every archive we intend to restore has been proved readable.
    # -----------------------------------------------------------------------
    if ($From -eq '') {
        Write-Step 'Looking for the newest backup under dist\backups'
        $From = Find-LatestBackup
        if ($From -eq '') {
            Stop-Hermes ('No backup directory given and none found under ' + (Join-Path $HermesRoot 'dist\backups') + '. Pass -From with the directory scripts\backup.ps1 wrote (it is named hermes-backup-<timestamp> and contains hermes-data.tar.gz).')
        }
        Write-Ok ('Using ' + $From)
    }

    if (-not (Test-Path -LiteralPath $From)) {
        Stop-Hermes ('Backup path does not exist: ' + $From)
    }
    $fromItem = Get-Item -LiteralPath $From
    if (-not $fromItem.PSIsContainer) {
        Stop-Hermes ('-From must be the backup DIRECTORY, not a single file. You gave: ' + $From + '. A Hermes backup is a folder containing hermes-data.tar.gz, freellmapi-data.tar.gz and MANIFEST.txt — pass the folder.')
    }
    $From = $fromItem.FullName
    Write-Ok ('Backup directory: ' + $From)

    # Does it look like a Hermes backup at all?
    Write-Step 'Validating the backup directory'
    $manifestPath = Join-Path $From 'MANIFEST.txt'
    if (Test-Path -LiteralPath $manifestPath) {
        $manifestText = (Get-Content -LiteralPath $manifestPath -Raw)
        if ($manifestText -match 'HERMES VOLUME BACKUP') {
            Write-Ok 'MANIFEST.txt present and recognised'
            foreach ($line in (Get-Content -LiteralPath $manifestPath)) {
                if ($line -match '^(created|source host|project name|stack quiesced)\s*:') {
                    Write-Info2 $line.Trim()
                }
            }
        } else {
            Write-Warn2 'MANIFEST.txt is present but does not look like a Hermes manifest.'
        }
    } else {
        Write-Warn2 'No MANIFEST.txt in this directory. Continuing on the archive names alone.'
    }

    $present = @()
    foreach ($logical in $RestoreVolumes) {
        $file = $logical + '.tar.gz'
        $path = Join-Path $From $file
        if (Test-Path -LiteralPath $path) {
            $size = (Get-Item -LiteralPath $path).Length
            Write-Ok ($file + '  ' + (Format-Bytes -Bytes $size))
            $present += @{ Logical = $logical; File = $file; Path = $path; Bytes = $size }
        } else {
            Write-Warn2 ($file + ' is not in this backup — ' + $logical + ' will be left exactly as it is.')
        }
    }
    if ($present.Count -eq 0) {
        Stop-Hermes ('This does not look like a Hermes backup: ' + $From + ' contains neither hermes-data.tar.gz nor freellmapi-data.tar.gz. Check the path, or re-create the backup with scripts\backup.ps1.')
    }

    # Refuse the session volume loudly, wherever the archive came from.
    $sessionArchive = Join-Path $From ($RefusedVolume + '.tar.gz')
    if (Test-Path -LiteralPath $sessionArchive) {
        Write-Host ''
        Write-Warn2 ($RefusedVolume + '.tar.gz is in this backup and will NOT be restored.')
        Write-Info2 'That volume is a live authenticated LinkedIn browser profile: session'
        Write-Info2 'cookies, auth tokens and a device fingerprint. Replaying it on another'
        Write-Info2 'machine is fragile (the fingerprint no longer matches, so LinkedIn is'
        Write-Info2 'more likely to invalidate the session or flag the account) AND a security'
        Write-Info2 'risk (those files are bearer credentials to your LinkedIn account, in'
        Write-Info2 'plain form). There is no flag that overrides this, including -Force.'
        Write-Host ''
        Write-Info2 'Log in on THIS machine instead, once:  .\scripts\linkedin-login.ps1'
        Write-Host ''
    }

    # -----------------------------------------------------------------------
    # 2. Refuse to restore over a running stack.
    # -----------------------------------------------------------------------
    Write-Step 'Checking that nothing is running'
    $running = @(Get-NativeOutput -Exe 'docker' -Arguments @('compose', 'ps', '-q'))
    if ($script:LastNativeExit -ne 0) { $running = @() }
    $running = @($running | Where-Object { $_.Trim() -ne '' })

    if ($running.Count -gt 0) {
        if ($Down) {
            Write-Warn2 ($running.Count.ToString() + ' container(s) running; bringing the stack down (-Down).')
            $null = Invoke-NativeChecked -Exe 'docker' -Arguments @(
                'compose', '--profile', 'build-only', '--profile', 'login',
                'down', '--remove-orphans'
            )
            Write-Ok 'Stack down (volumes kept)'
        } else {
            Write-Host ''
            Write-Bad ($running.Count.ToString() + ' Hermes container(s) are still running. Refusing to restore.')
            Write-Host ''
            Write-Info2 'Replacing a volume under a running container leaves that container holding'
            Write-Info2 'deleted inodes, and for the SQLite database it is a good way to end up with'
            Write-Info2 'a corrupt file. Bring the stack down first:'
            Write-Host ''
            Write-Info2 '    make down                 (or: docker compose down)'
            Write-Host ''
            Write-Info2 'Then re-run this script. Or pass -Down to have it do that for you.'
            Write-Host ''
            throw 'Stack is running; run `make down` first (or pass -Down).'
        }
    } else {
        Write-Ok 'No running containers in this compose project'
    }

    # -----------------------------------------------------------------------
    # 3. Resolve the target volumes.
    #
    #    EXACT NAMES ONLY. common.ps1's Resolve-DockerVolume has a last-resort
    #    suffix match ("any volume ending in _hermes-data"), which is fine for a
    #    read-only backup but dangerous here: on a machine that also has the
    #    separate hermes-agent project it can resolve to `hermes_hermes-data`,
    #    and we would wipe an unrelated project's data. So we only ever write
    #    <project>_<logical>.
    # -----------------------------------------------------------------------
    Write-Step 'Resolving target volumes'
    $plan = @()
    foreach ($item in $present) {
        $real = $project + '_' + $item.Logical
        $exists = Test-VolumeExists -Name $real
        if ($exists) {
            Write-Ok ($item.Logical + '  ->  ' + $real + ' (exists)')
        } else {
            Write-Warn2 ($item.Logical + '  ->  ' + $real + ' (does not exist yet; it will be created)')
        }

        $busy = Get-ContainersUsingVolume -Name $real
        if ($busy.Count -gt 0) {
            Stop-Hermes ('Volume ' + $real + ' is in use by ' + $busy.Count + ' running container(s) outside this compose project. Stop them first: docker ps --filter volume=' + $real)
        }

        $plan += @{
            Logical = $item.Logical
            File    = $item.File
            Bytes   = $item.Bytes
            Real    = $real
            Exists  = $exists
        }
    }

    # -----------------------------------------------------------------------
    # 4. Prove every archive is readable and really is what it claims to be,
    #    BEFORE emptying anything.
    # -----------------------------------------------------------------------
    $helper   = Resolve-HelperImage -Preferred $HelperImage
    $hostFrom = ConvertTo-DockerPath -Path $From

    Write-Step 'Verifying the archives'
    foreach ($p in $plan) {
        $entries = Get-ArchiveEntryCount -Helper $helper -HostDir $hostFrom -FileName $p.File
        if ($entries -lt 0) {
            Stop-Hermes ($p.File + ' is not a readable gzip tar. It is truncated, corrupt, or not a Hermes backup archive. Verify by hand with: docker run --rm -v "' + $hostFrom + ':/backup:ro" ' + $helper + ' tar -tzf /backup/' + $p.File)
        }
        if ($entries -eq 0) {
            Stop-Hermes ($p.File + ' contains zero entries. Restoring it would only empty ' + $p.Real + '. Refusing.')
        }
        $p.Entries = $entries

        if ($p.Logical -eq 'hermes-data') {
            $looksRight = Test-ArchiveMarkers -Helper $helper -HostDir $hostFrom -FileName $p.File -Markers $HermesDataMarkers
            if (-not $looksRight) {
                if ($Force) {
                    Write-Warn2 ($p.File + ' has none of ' + ($HermesDataMarkers -join ', ') + ' at its top level. Continuing anyway (-Force).')
                } else {
                    Stop-Hermes ($p.File + ' does not look like a Hermes hermes-data archive: none of ' + ($HermesDataMarkers -join ', ') + ' appear at its top level. Refusing to overwrite ' + $p.Real + ' with it. Pass -Force if you are certain.')
                }
            }
        }
        Write-Ok ($p.File + ': ' + $entries + ' entries, readable')
    }

    # -----------------------------------------------------------------------
    # 5. The plan, then consent.
    # -----------------------------------------------------------------------
    Write-Host ''
    Write-Host '  RESTORE PLAN' -ForegroundColor Cyan
    foreach ($p in $plan) {
        Write-Host ('    ' + $p.File.PadRight(24) + ' -> ' + $p.Real.PadRight(34) + ' (' + (Format-Bytes -Bytes $p.Bytes) + ', ' + $p.Entries + ' entries)')
    }
    Write-Host ('    ' + ($RefusedVolume + ' ').PadRight(24) + ' -> NOT RESTORED, deliberately')
    Write-Host ''

    if ($DryRun) {
        Write-Head 'DRY RUN — nothing was written'
        Write-Host '  Re-run without -DryRun to apply this plan.'
        Write-Host ''
        return
    }

    foreach ($p in $plan) {
        if (-not $p.Exists) {
            # Create it with the labels Compose stamps on its own volumes, so a
            # later `docker compose up` adopts it instead of complaining that a
            # volume of that name exists but was not created by Compose.
            Write-Step ('Creating volume ' + $p.Real)
            $null = Invoke-NativeChecked -Exe 'docker' -Arguments @(
                'volume', 'create',
                '--label', ('com.docker.compose.project=' + $project),
                '--label', ('com.docker.compose.volume=' + $p.Logical),
                $p.Real
            )
            Write-Ok ($p.Real + ' created (empty)')
            $p.Existing = 0
            continue
        }

        $count = Get-VolumeEntryCount -Helper $helper -Volume $p.Real
        $p.Existing = $count
        if ($count -gt 0) {
            Write-Warn2 ($p.Real + ' is NOT empty (' + $count + ' top-level entries). Restoring EMPTIES it first.')
            if ($p.Logical -eq 'hermes-data') {
                Write-Info2 'That is your SQLite database, your generated resumes and your sandbox'
                Write-Info2 'workspaces. There is no merge and no undo.'
            }
            if ($p.Logical -eq 'freellmapi-data') {
                Write-Info2 'That is the LLM router account and its encrypted provider keys.'
            }
            if (-not $Force) {
                $go = Read-YesNo -Question ('Empty ' + $p.Real + ' and replace it with ' + $p.File + '?') -DefaultYes $false
                if (-not $go) {
                    Write-Warn2 ('Skipping ' + $p.Logical + ' — left untouched.')
                    $p.Skip = $true
                }
            }
        } elseif ($count -eq 0) {
            Write-Ok ($p.Real + ' is empty; nothing to overwrite')
        } else {
            Write-Warn2 ($p.Real + ': could not read its current contents; continuing.')
        }
    }

    $todo = @($plan | Where-Object { -not $_.Skip })
    if ($todo.Count -eq 0) {
        Write-Head 'NOTHING RESTORED'
        Write-Host '  Every volume was skipped. No changes were made.'
        Write-Host ''
        return
    }

    # -----------------------------------------------------------------------
    # 6. Restore.
    #
    #    Why a bind mount rather than piping the archive on stdin: Windows
    #    PowerShell 5.1 pipes native I/O through its TEXT pipeline, which
    #    CORRUPTS binary data (re-encoding + CRLF translation). Reading the
    #    archive from inside the container off a read-only bind mount keeps the
    #    bytes exact. (scripts/restore.sh does the opposite for the opposite
    #    reason: Git Bash rewrites POSIX-looking -v paths.)
    #
    #    `find -mindepth 1 -maxdepth 1 -exec rm -rf {} +` empties the volume
    #    including dotfiles, without deleting the mount point itself.
    # -----------------------------------------------------------------------
    $results = @()
    foreach ($p in $todo) {
        Write-Step ('Restoring ' + $p.File + '  ->  ' + $p.Real)
        $inner = 'set -e; find /v -mindepth 1 -maxdepth 1 -exec rm -rf {} + ; tar -xzf "/backup/' + $p.File + '" -C /v ; ls -A /v | wc -l'
        $out = @(Get-NativeOutput -Exe 'docker' -Arguments @(
            'run', '--rm',
            '-v', ($p.Real + ':/v'),
            '-v', ($hostFrom + ':/backup:ro'),
            $helper, 'sh', '-c', $inner
        ))
        if ($script:LastNativeExit -ne 0) {
            Stop-Hermes ('Restore of ' + $p.Logical + ' FAILED. ' + $p.Real + ' may now be partially written — re-run this script to try again, or `docker volume rm ' + $p.Real + '` and let `make up` recreate it empty. Reproduce by hand with: docker run --rm -v ' + $p.Real + ':/v -v "' + $hostFrom + ':/backup:ro" ' + $helper + ' sh -c ''tar -xzf /backup/' + $p.File + ' -C /v''')
        }

        $after = 'unknown'
        if ($out.Count -gt 0) { $after = $out[$out.Count - 1].Trim() }
        Write-Ok ($p.Logical + ' restored (' + $after + ' top-level entries in the volume)')
        $results += @{ Logical = $p.Logical; Real = $p.Real; File = $p.File; Entries = $p.Entries; After = $after }
    }

    # -----------------------------------------------------------------------
    # 7. Post-restore sanity: is the SQLite file actually there?
    # -----------------------------------------------------------------------
    $restoredCore = @($results | Where-Object { $_.Logical -eq 'hermes-data' })
    if ($restoredCore.Count -gt 0) {
        Write-Step 'Checking the restored database file'
        $vol = $restoredCore[0].Real
        $out = @(Get-NativeOutput -Exe 'docker' -Arguments @(
            'run', '--rm', '-v', ($vol + ':/v:ro'), $helper,
            'sh', '-c', 'if [ -f /v/hermes.db ]; then wc -c < /v/hermes.db; else echo MISSING; fi'
        ))
        $verdict = 'unknown'
        if ($script:LastNativeExit -eq 0 -and $out.Count -gt 0) { $verdict = $out[0].Trim() }
        if ($verdict -eq 'MISSING') {
            Write-Warn2 'No /hermes.db in the restored volume. Hermes will create an EMPTY database on next start.'
            Write-Info2 'That is expected only if the backup was taken before the stack ever ran.'
        } elseif ($verdict -eq 'unknown') {
            Write-Warn2 'Could not inspect the restored volume; check it by hand after `make up`.'
        } else {
            $bytes = 0
            if ([int]::TryParse($verdict, [ref]$bytes)) {
                Write-Ok ('hermes.db present, ' + (Format-Bytes -Bytes $bytes))
            } else {
                Write-Ok ('hermes.db present (' + $verdict + ' bytes)')
            }
        }
    }

    # -----------------------------------------------------------------------
    # 8. ENCRYPTION_KEY cross-check for the router volume.
    # -----------------------------------------------------------------------
    $restoredLlm = @($results | Where-Object { $_.Logical -eq 'freellmapi-data' })
    if ($restoredLlm.Count -gt 0) {
        $key = Get-DotEnvValue -EnvMap $envMap -Key 'ENCRYPTION_KEY' -Default ''
        Write-Host ''
        if ($key -eq '') {
            Write-Warn2 'ENCRYPTION_KEY is not set in this machine''s .env.'
            Write-Info2 'freellmapi cannot even start without it, and the provider keys you just'
            Write-Info2 'restored were encrypted with the value from the SOURCE machine. Put that'
            Write-Info2 'exact value in .env before `make up`, or run `make bootstrap` to generate'
            Write-Info2 'a fresh one and re-add your provider keys at http://localhost:3001.'
        } else {
            Write-Warn2 'ENCRYPTION_KEY must MATCH the source machine, or the restored provider keys are unreadable.'
            Write-Info2 ('This machine''s .env has a key ending in ...' + $key.Substring([math]::Max(0, $key.Length - 6)))
            Write-Info2 'If that is not the same value the backup was taken under, the router will'
            Write-Info2 'start but every stored provider key will fail to decrypt — re-add them at'
            Write-Info2 'http://localhost:3001 and mint a fresh FREELLMAPI_KEY.'
        }
    }

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    Write-Head 'RESTORE COMPLETE'
    Write-Host ('  From     : ' + $From)
    Write-Host ('  Project  : ' + $project)
    Write-Host ('  Volumes  : ' + (($results | ForEach-Object { $_.Real }) -join ', '))
    Write-Host ''
    Write-Host '  Next:' -ForegroundColor Green
    Write-Host '    1) check ENCRYPTION_KEY in .env matches the source machine' -ForegroundColor Green
    Write-Host '    2) make up            (or: docker compose up -d)' -ForegroundColor Green
    Write-Host '    3) make health        (expect ok:true; llm/mcp may need setup)' -ForegroundColor Green
    Write-Host ''
    Write-Host ('  ' + $RefusedVolume + ' was NOT restored, deliberately. Log in on THIS machine:') -ForegroundColor Magenta
    Write-Host '    .\scripts\linkedin-login.ps1' -ForegroundColor Magenta
    Write-Host ''

} finally {
    Pop-Location
}
