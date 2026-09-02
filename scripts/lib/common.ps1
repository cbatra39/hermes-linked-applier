#Requires -Version 5.1
<#
    Hermes — shared helpers for all PowerShell scripts.

    Dot-source from a script in scripts/ :
        . (Join-Path $PSScriptRoot 'lib\common.ps1')

    COMPATIBILITY CONTRACT: Windows PowerShell 5.1.
      - no ternary  ( cond ? a : b )
      - no null-coalescing ( ?? , ?. )
      - no pipeline chain operators ( && , || )
      - no -AsHashtable on ConvertFrom-Json
    Use if/else and ';' sequencing only.
#>

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# $PSScriptRoot here is <repo>\scripts\lib
$script:HermesLibDir     = $PSScriptRoot
$script:HermesScriptsDir = Split-Path -Parent $script:HermesLibDir
$script:HermesRoot       = Split-Path -Parent $script:HermesScriptsDir

# Exported (script-scope is not visible to the dot-sourcing script's functions,
# so publish plain globals-in-caller-scope names too).
$HermesRoot       = $script:HermesRoot
$HermesScriptsDir = $script:HermesScriptsDir
$HermesEnvFile    = Join-Path $HermesRoot '.env'
$HermesEnvExample = Join-Path $HermesRoot '.env.example'

# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

function Write-Head {
    param([Parameter(Mandatory = $true)][string]$Text)
    $bar = '=' * 74
    Write-Host ''
    Write-Host $bar -ForegroundColor DarkCyan
    Write-Host ("  " + $Text) -ForegroundColor Cyan
    Write-Host $bar -ForegroundColor DarkCyan
}

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ("==> " + $Text) -ForegroundColor Cyan
}

function Write-Ok {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ("  [ ok ] " + $Text) -ForegroundColor Green
}

function Write-Warn2 {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ("  [warn] " + $Text) -ForegroundColor Yellow
}

function Write-Bad {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ("  [FAIL] " + $Text) -ForegroundColor Red
}

function Write-Info2 {
    param([Parameter(Mandatory = $true)][string]$Text)
    Write-Host ("         " + $Text) -ForegroundColor Gray
}

function Stop-Hermes {
    <# Fail loudly with a clear, actionable message. #>
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ''
    Write-Bad $Message
    Write-Host ''
    throw $Message
}

# ---------------------------------------------------------------------------
# Native process helpers
# ---------------------------------------------------------------------------

function Test-CommandExists {
    param([Parameter(Mandatory = $true)][string]$Name)
    $c = Get-Command $Name -ErrorAction SilentlyContinue
    if ($c) { return $true }
    return $false
}

function Invoke-Native {
    <#
        Run a native executable, stream its output to the console, and return the
        exit code. Never redirects stderr (Windows PowerShell 5.1 turns native
        stderr into NativeCommandError records when you do).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Exe @Arguments
    return $LASTEXITCODE
}

function Invoke-NativeChecked {
    <# Run a native exe and throw a readable error if it fails. #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$What = ''
    )
    if ($What -ne '') { Write-Step $What }
    $code = Invoke-Native -Exe $Exe -Arguments $Arguments
    if ($code -ne 0) {
        $joined = $Arguments -join ' '
        Stop-Hermes ("Command failed (exit " + $code + "): " + $Exe + " " + $joined)
    }
    return 0
}

function Get-NativeOutput {
    <#
        Run a native exe and capture stdout as an array of lines.
        Sets $script:LastNativeExit so callers can branch on failure.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $out = & $Exe @Arguments
    $script:LastNativeExit = $LASTEXITCODE
    if ($null -eq $out) { return @() }
    return @($out)
}

# ---------------------------------------------------------------------------
# Docker / compose discovery
# ---------------------------------------------------------------------------

# Filled in by Assert-DockerReady. Compose is invoked as:
#     & docker @($HermesComposeArgs + @('build'))
$HermesComposeArgs = @('compose')

function Assert-DockerReady {
    <#
        Preflight: docker CLI on PATH, engine reachable, compose v2 available.
        Throws with a specific remedy for each failure mode.
    #>
    Write-Step 'Checking Docker'

    if (-not (Test-CommandExists 'docker')) {
        Stop-Hermes 'docker is not on PATH. Install Docker Desktop (Windows) and reopen this terminal.'
    }

    $ver = Get-NativeOutput -Exe 'docker' -Arguments @('info', '--format', '{{.ServerVersion}}')
    if ($script:LastNativeExit -ne 0) {
        Stop-Hermes 'Docker engine is not responding. Start Docker Desktop, wait for the whale icon to go steady, then re-run.'
    }
    if ($ver.Count -gt 0) { Write-Ok ('Docker engine ' + $ver[0]) } else { Write-Ok 'Docker engine reachable' }

    $cv = Get-NativeOutput -Exe 'docker' -Arguments @('compose', 'version', '--short')
    if ($script:LastNativeExit -ne 0) {
        Stop-Hermes 'Docker Compose v2 is missing (`docker compose version` failed). Hermes requires Compose v2 (the `docker compose` subcommand, not the legacy `docker-compose` binary). Update Docker Desktop.'
    }
    if ($cv.Count -gt 0) { Write-Ok ('Docker Compose ' + $cv[0]) } else { Write-Ok 'Docker Compose v2 present' }

    return $true
}

function Get-ComposeProjectName {
    <#
        Compose derives the project name from COMPOSE_PROJECT_NAME, else from the
        project directory basename (lowercased, illegal characters dropped).
        Needed to predict built image names and named-volume prefixes.
    #>
    param([hashtable]$EnvMap = $null)

    if ($env:COMPOSE_PROJECT_NAME) { return $env:COMPOSE_PROJECT_NAME }
    if ($EnvMap -and $EnvMap.ContainsKey('COMPOSE_PROJECT_NAME')) {
        if ($EnvMap['COMPOSE_PROJECT_NAME'] -ne '') { return $EnvMap['COMPOSE_PROJECT_NAME'] }
    }
    $base = (Split-Path -Leaf $HermesRoot).ToLowerInvariant()
    $clean = [System.Text.RegularExpressions.Regex]::Replace($base, '[^a-z0-9_-]', '')
    if ($clean -eq '') { $clean = 'hermes' }
    return $clean
}

function Get-ComposeImages {
    <#
        The set of images this compose project uses, including build-only and
        login profiles. Falls back to a predicted list if `config --images`
        is unavailable on this Compose build.
    #>
    $args1 = @('compose', '--profile', 'build-only', '--profile', 'login', 'config', '--images')
    $lines = Get-NativeOutput -Exe 'docker' -Arguments $args1
    if ($script:LastNativeExit -eq 0 -and $lines.Count -gt 0) {
        $set = New-Object System.Collections.Generic.List[string]
        foreach ($l in $lines) {
            $t = $l.Trim()
            if ($t -eq '') { continue }
            if (-not $set.Contains($t)) { $set.Add($t) }
        }
        return $set.ToArray()
    }

    Write-Warn2 '`docker compose config --images` unavailable; predicting image names from the project name.'
    $proj = Get-ComposeProjectName
    return @(
        'ghcr.io/tashfeenahmed/freellmapi:latest',
        'stickerdaniel/linkedin-mcp-server:4.23.2',
        ($proj + '-hermes-core'),
        ($proj + '-hermes-dashboard'),
        'hermes-linkedin/sandbox:latest'
    )
}

function Test-StackHasContainers {
    <# True when this compose project already has containers (running or not). #>
    $ids = Get-NativeOutput -Exe 'docker' -Arguments @('compose', 'ps', '-aq')
    if ($script:LastNativeExit -ne 0) { return $false }
    foreach ($i in $ids) { if ($i.Trim() -ne '') { return $true } }
    return $false
}

function Resolve-DockerVolume {
    <#
        Map a logical compose volume name (e.g. 'hermes-data') to the real Docker
        volume name. Compose v2 prefixes with the project name; a volume declared
        `external` or with an explicit `name:` keeps its bare name.
        Returns $null when nothing matches.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Logical,
        [string]$Project = ''
    )
    if ($Project -eq '') { $Project = Get-ComposeProjectName }

    $candidates = @(($Project + '_' + $Logical), $Logical)
    foreach ($c in $candidates) {
        $null = Get-NativeOutput -Exe 'docker' -Arguments @('volume', 'inspect', $c)
        if ($script:LastNativeExit -eq 0) { return $c }
    }

    # Last resort: any volume whose name ends with the logical name.
    $all = Get-NativeOutput -Exe 'docker' -Arguments @('volume', 'ls', '--format', '{{.Name}}')
    if ($script:LastNativeExit -eq 0) {
        foreach ($v in $all) {
            $t = $v.Trim()
            if ($t -eq '') { continue }
            if ($t.EndsWith('_' + $Logical)) { return $t }
        }
    }
    return $null
}

function ConvertTo-DockerPath {
    <#
        Docker Desktop accepts Windows absolute paths in -v when the separators
        are forward slashes (C:/Users/me/x). Backslashes get mangled by the shell
        layers in between, so normalise here.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    return ($full -replace '\\', '/')
}

# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------

function Read-DotEnv {
    <# Parse a .env file into a hashtable. Ignores blanks and # comments. #>
    param([Parameter(Mandatory = $true)][string]$Path)

    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }

    foreach ($raw in (Get-Content -LiteralPath $Path)) {
        $line = $raw.Trim()
        if ($line -eq '') { continue }
        if ($line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        if ($val.Length -ge 2) {
            $q1 = ($val.StartsWith('"') -and $val.EndsWith('"'))
            $q2 = ($val.StartsWith("'") -and $val.EndsWith("'"))
            if ($q1 -or $q2) { $val = $val.Substring(1, $val.Length - 2) }
        }
        $map[$key] = $val
    }
    return $map
}

function Get-DotEnvValue {
    <# Read a key with a default. Empty string counts as "not set". #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$EnvMap,
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$Default = ''
    )
    if (-not $EnvMap.ContainsKey($Key)) { return $Default }
    $v = $EnvMap[$Key]
    if ($null -eq $v) { return $Default }
    if ($v.Trim() -eq '') { return $Default }
    return $v
}

function Set-DotEnvValue {
    <#
        Set KEY=VALUE in a .env file in place, preserving comments and ordering.
        Appends the key when missing. Writes UTF-8 WITHOUT a BOM — a BOM makes
        Compose read the first variable name as "?HOST_BIND" and silently ignore it.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = @(Get-Content -LiteralPath $Path) }

    $out     = New-Object System.Collections.Generic.List[string]
    $written = $false
    foreach ($raw in $lines) {
        $trim = $raw.Trim()
        $isAssign = ($trim -ne '') -and (-not $trim.StartsWith('#')) -and ($trim.IndexOf('=') -gt 0)
        if ($isAssign) {
            $k = $trim.Substring(0, $trim.IndexOf('=')).Trim()
            if ($k -eq $Key) {
                if (-not $written) {
                    $out.Add($Key + '=' + $Value)
                    $written = $true
                }
                continue
            }
        }
        $out.Add($raw)
    }
    if (-not $written) { $out.Add($Key + '=' + $Value) }

    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $out.ToArray(), $enc)
}

function New-HexKey {
    <# Cryptographically strong hex string. 32 bytes => 64 hex chars. #>
    param([int]$Bytes = 32)
    $buf = New-Object 'byte[]' $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    $sb = New-Object System.Text.StringBuilder
    foreach ($b in $buf) { $null = $sb.Append($b.ToString('x2')) }
    return $sb.ToString()
}

function Get-EnvExampleFallback {
    <#
        Emergency .env template, used ONLY when .env.example is missing from the
        checkout. Mirrors the documented variable set so bootstrap can still run.
    #>
    return @'
# Hermes configuration. Generated by scripts/bootstrap because .env.example was missing.
HOST_BIND=127.0.0.1
ENCRYPTION_KEY=
FREELLMAPI_BASE_URL=http://freellmapi:3001/v1
FREELLMAPI_KEY=
HERMES_MODEL_PRIMARY=
HERMES_MODEL_FALLBACKS=
LINKEDIN_MCP_URL=http://linkedin-mcp:8000/mcp
HERMES_API_PORT=8080
HERMES_DASHBOARD_PORT=3000
FREELLMAPI_PORT=3001
LINKEDIN_VIEWER_PORT=6080
HERMES_SANDBOX_IMAGE=hermes-linkedin/sandbox:latest
HERMES_SANDBOX_MEMORY_MB=1024
HERMES_SANDBOX_CPUS=1.0
HERMES_SANDBOX_TIMEOUT_S=300
HERMES_SANDBOX_NETWORK=none
HERMES_SANDBOX_WORKSPACE=/data/workspaces
HERMES_DATA_DIR=/data
HERMES_DOCKER_HOST=unix:///var/run/docker.sock
LOG_LEVEL=INFO
'@
}

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

function Test-PortFree {
    <#
        True when nothing is LISTENING on the TCP port (any local address).
        Prefers Get-NetTCPConnection; falls back to parsing netstat.
    #>
    param([Parameter(Mandatory = $true)][int]$Port)

    if (Test-CommandExists 'Get-NetTCPConnection') {
        try {
            $conns = Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $Port }
            if ($conns) { return $false }
            return $true
        } catch {
            # fall through to netstat
        }
    }

    $rows = Get-NativeOutput -Exe 'netstat' -Arguments @('-ano', '-p', 'tcp')
    $needle = ':' + [string]$Port
    foreach ($r in $rows) {
        if ($r -notmatch 'LISTENING') { continue }
        $parts = ($r.Trim() -split '\s+')
        if ($parts.Count -lt 2) { continue }
        $local = $parts[1]
        $i = $local.LastIndexOf(':')
        if ($i -lt 0) { continue }
        if ($local.Substring($i) -eq $needle) { return $false }
    }
    return $true
}

function Get-PortOwner {
    <# Best-effort "what is holding this port" string, for error messages. #>
    param([Parameter(Mandatory = $true)][int]$Port)

    if (-not (Test-CommandExists 'Get-NetTCPConnection')) { return 'unknown process' }
    try {
        $conn = Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { $_.LocalPort -eq $Port } |
                Select-Object -First 1
        if (-not $conn) { return 'unknown process' }
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) { return ($proc.ProcessName + ' (PID ' + $conn.OwningProcess + ')') }
        return ('PID ' + $conn.OwningProcess)
    } catch {
        return 'unknown process'
    }
}

# ---------------------------------------------------------------------------
# HTTP probe (no external deps)
# ---------------------------------------------------------------------------

function Test-HttpOk {
    <# True when a GET returns any HTTP status (i.e. something is answering). #>
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSec = 5
    )
    try {
        $null = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
        return $true
    } catch {
        # A 4xx/5xx still proves the port is answering HTTP.
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

function Wait-HttpOk {
    <# Poll a URL until it answers or the budget runs out. #>
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSec = 90,
        [string]$Label = ''
    )
    if ($Label -eq '') { $Label = $Url }
    Write-Step ('Waiting for ' + $Label)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpOk -Url $Url -TimeoutSec 4) {
            Write-Ok ($Label + ' is answering')
            return $true
        }
        Start-Sleep -Seconds 3
    }
    Write-Warn2 ($Label + ' did not answer within ' + $TimeoutSec + 's (it may still be starting).')
    return $false
}

function Read-YesNo {
    <# Interactive y/n prompt. Returns $true for yes. #>
    param(
        [Parameter(Mandatory = $true)][string]$Question,
        [bool]$DefaultYes = $false
    )
    $suffix = ' [y/N] '
    if ($DefaultYes) { $suffix = ' [Y/n] ' }
    while ($true) {
        $ans = Read-Host ($Question + $suffix)
        if ($null -eq $ans) { $ans = '' }
        $ans = $ans.Trim().ToLowerInvariant()
        if ($ans -eq '') {
            return $DefaultYes
        }
        if ($ans -eq 'y' -or $ans -eq 'yes') { return $true }
        if ($ans -eq 'n' -or $ans -eq 'no')  { return $false }
        Write-Host 'Please answer y or n.' -ForegroundColor Yellow
    }
}

function Format-Bytes {
    param([Parameter(Mandatory = $true)][double]$Bytes)
    if ($Bytes -ge 1073741824) { return ([math]::Round($Bytes / 1073741824, 2)).ToString() + ' GB' }
    if ($Bytes -ge 1048576)    { return ([math]::Round($Bytes / 1048576, 1)).ToString() + ' MB' }
    if ($Bytes -ge 1024)       { return ([math]::Round($Bytes / 1024, 1)).ToString() + ' KB' }
    return $Bytes.ToString() + ' B'
}
