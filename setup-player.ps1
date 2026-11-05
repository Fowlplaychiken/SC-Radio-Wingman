$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'player-windows.json') -Raw | ConvertFrom-Json

foreach ($location in $manifest.existing_locations) {
    $base = [Environment]::GetEnvironmentVariable($location[0])
    if ($base -and [IO.Path]::IsPathRooted($base)) {
        $existing = Join-Path $base $location[1]
        if (Test-Path -LiteralPath $existing -PathType Leaf) {
            Write-Output "Using existing mpv at $existing. Leave the Wingman mpv executable field blank."
            return
        }
    }
}

if (-not $env:LOCALAPPDATA -or -not [IO.Path]::IsPathRooted($env:LOCALAPPDATA)) {
    throw 'Windows did not supply a local app-data folder.'
}
$player = Join-Path $env:LOCALAPPDATA $manifest.managed_location
$destination = Split-Path -Parent $player
if (Test-Path -LiteralPath $destination) {
    if ((Test-Path -LiteralPath $player -PathType Leaf) -and
        (Get-FileHash -LiteralPath $player -Algorithm SHA256).Hash -eq $manifest.executable_sha256) {
        Write-Output "SC Radio's mpv is already ready. Leave the Wingman mpv executable field blank."
        return
    }
    throw "An incomplete or modified SC Radio player is present at $destination. Remove that folder before retrying."
}

$architecture = $env:PROCESSOR_ARCHITEW6432
if (-not $architecture) { $architecture = $env:PROCESSOR_ARCHITECTURE }
if ($architecture -ne 'AMD64') {
    throw 'The setup download supports Windows x64. Install mpv for your architecture and select its path in Wingman.'
}

$root = Split-Path -Parent $destination
New-Item -ItemType Directory -Path $root -Force | Out-Null
$stage = Join-Path $root ('setup-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $archive = Join-Path $stage 'mpv.zip'
    $expanded = Join-Path $stage 'expanded'
    Write-Output 'Downloading the verified mpv release from mpv-player/mpv...'
    Invoke-WebRequest -UseBasicParsing -Uri $manifest.archive_url -OutFile $archive -TimeoutSec 120
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $manifest.archive_sha256) {
        throw 'The mpv archive SHA-256 did not match. No player was installed.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
    $stagedPlayer = Join-Path $expanded 'mpv.exe'
    if ((Get-FileHash -LiteralPath $stagedPlayer -Algorithm SHA256).Hash -ne $manifest.executable_sha256) {
        throw 'The extracted mpv executable SHA-256 did not match. No player was installed.'
    }
    [IO.Directory]::Move($expanded, $destination)
    Write-Output "SC Radio's mpv is ready. Leave the Wingman mpv executable field blank."
} finally {
    if ((Split-Path -Parent ([IO.Path]::GetFullPath($stage))) -eq [IO.Path]::GetFullPath($root)) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
