#Requires -Version 5.1
<#
.SYNOPSIS
    One-click build pipeline: PyInstaller bundle -> Inno Setup installer -> GitHub Release.

.DESCRIPTION
    1. Cleans previous build artefacts (dist\, build\, output\)
    2. Runs PyInstaller using recipeparser.spec
    3. Compiles installer.iss with Inno Setup's ISCC.exe
    4. Creates a GitHub Release for the current git tag and uploads the installer
       (requires GitHub CLI: winget install GitHub.cli  then  gh auth login)

.PARAMETER SkipRelease
    Build the installer but do not create a GitHub Release.

.NOTES
    Prerequisites (one-time installs):
      pip install pyinstaller
      Inno Setup 6  - https://jrsoftware.org/isdl.php
      GitHub CLI    - winget install GitHub.cli  then  gh auth login
#>
param(
    [switch]$SkipRelease
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Paths ─────────────────────────────────────────────────────────────────────
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SpecFile    = Join-Path $ProjectRoot "recipeparser.spec"
$IssFile     = Join-Path $ProjectRoot "installer.iss"
$OutputDir   = Join-Path $ProjectRoot "output"
$GhExe       = "C:\Program Files\GitHub CLI\gh.exe"

# Standard Inno Setup install locations (try both 32-bit and 64-bit Program Files)
$IsccCandidates = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$IsccExe = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

# ── Preflight checks ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== RecipeParser installer build ===" -ForegroundColor Cyan

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error "pyinstaller not found on PATH. Run: pip install pyinstaller"
}

if (-not $IsccExe) {
    Write-Error "Inno Setup ISCC.exe not found. Download from https://jrsoftware.org/isdl.php"
}

$GhAvailable = Test-Path $GhExe
if (-not $SkipRelease -and -not $GhAvailable) {
    Write-Warning "GitHub CLI not found at $GhExe - release upload will be skipped."
    Write-Warning "Install with: winget install GitHub.cli  then  gh auth login"
    $SkipRelease = $true
}

Write-Host "  PyInstaller : $(pyinstaller --version 2>&1)" -ForegroundColor Green
Write-Host "  ISCC        : $IsccExe" -ForegroundColor Green
if ($GhAvailable -and -not $SkipRelease) {
    Write-Host "  GitHub CLI  : $GhExe" -ForegroundColor Green
}
Write-Host ""

# ── Step 1: Clean ─────────────────────────────────────────────────────────────
Write-Host "[1/4] Cleaning previous build artefacts..."
foreach ($dir in @("dist", "build", "output")) {
    $path = Join-Path $ProjectRoot $dir
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
        Write-Host "      Removed $dir\"
    }
}

# ── Step 2: PyInstaller ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/4] Running PyInstaller..."
Push-Location $ProjectRoot
try {
    pyinstaller $SpecFile
    if ($LASTEXITCODE -ne 0) {
        Write-Error "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$Bundle = Join-Path $ProjectRoot "dist\RecipeParser\RecipeParser.exe"
if (-not (Test-Path $Bundle)) {
    Write-Error "Expected bundle not found at: $Bundle"
}
Write-Host "      Bundle OK: dist\RecipeParser\" -ForegroundColor Green

# ── Step 3: Inno Setup ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/4] Compiling installer with Inno Setup..."
New-Item -ItemType Directory -Force $OutputDir | Out-Null

& $IsccExe $IssFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "Inno Setup failed with exit code $LASTEXITCODE"
}

$Installer = Get-ChildItem $OutputDir -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) {
    Write-Error "Installer .exe not found in $OutputDir - check Inno Setup output above."
}

# ── Step 4: GitHub Release ────────────────────────────────────────────────────
Write-Host ""
if ($SkipRelease) {
    Write-Host "[4/4] Skipping GitHub Release (use -SkipRelease:$false to enable)." -ForegroundColor Yellow
} else {
    Write-Host "[4/4] Creating GitHub Release..."

    # Derive the version tag from the installer filename (e.g. RecipeParser-Setup-2.0.0.exe -> v2.0.0)
    $Version = $Installer.BaseName -replace '^RecipeParser-Setup-', 'v'
    $Tag     = $Version

    # Check whether this tag already has a release
    $existing = & $GhExe release view $Tag --repo IanDBallard/RecipeParser 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      Release $Tag already exists - uploading asset..." -ForegroundColor Yellow
        & $GhExe release upload $Tag $Installer.FullName `
            --repo IanDBallard/RecipeParser --clobber
    } else {
        & $GhExe release create $Tag $Installer.FullName `
            --repo IanDBallard/RecipeParser `
            --title $Version `
            --generate-notes
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "GitHub Release step failed - installer is still at $($Installer.FullName)"
    } else {
        Write-Host "      Release OK: https://github.com/IanDBallard/RecipeParser/releases/tag/$Tag" -ForegroundColor Green
    }
}

# ── Report ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Cyan
$SizeMB = [math]::Round($Installer.Length / 1MB, 1)
Write-Host "  Installer : $($Installer.FullName)" -ForegroundColor Green
Write-Host "  Size      : ${SizeMB} MB"            -ForegroundColor Green
Write-Host ""
