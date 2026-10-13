[CmdletBinding()]
param(
    [ValidateSet('x64', 'arm64')]
    [string]$Architecture = 'x64',
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopRoot = Join-Path $repositoryRoot 'desktop'

Push-Location $desktopRoot
try {
    if (-not $SkipInstall) {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) {
            throw "npm ci failed with exit code $LASTEXITCODE"
        }
    }

    & npm.cmd run make -- --platform=win32 --arch=$Architecture
    if ($LASTEXITCODE -ne 0) {
        throw "Electron Forge failed with exit code $LASTEXITCODE"
    }

    $installer = Get-ChildItem -Path (Join-Path $desktopRoot 'out\make') -Filter 'DrasilSetup.exe' -Recurse |
        Select-Object -First 1
    if (-not $installer) {
        throw 'The build completed but DrasilSetup.exe was not found.'
    }

    Write-Host "Drasil installer: $($installer.FullName)"
}
finally {
    Pop-Location
}
