#Requires -Version 5.1
<#
.SYNOPSIS
    Local build-only script: PyInstaller bundle -> Inno Setup installer.

.DESCRIPTION
    Builds the Windows installer locally for development and testing.
    Does NOT create or update GitHub Releases — that is done by the
    GitHub Actions workflow when you push a tag.

    Use this for quick iteration when testing PyInstaller or Inno Setup
    changes. For official releases, push a version tag and let CI handle it.

.NOTES
    Prerequisites (one-time installs):
      pip install -r requirements.txt
      pip install -e . pyinstaller
      Inno Setup 6  - https://jrsoftware.org/isdl.php

    Canonical build: GitHub Actions (.github/workflows/build-installer.yml)
    runs on tag push — same Python version, same deps, validated output.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Paths ─────────────────────────────────────────────────────────────────────
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SpecFile    = Join-Path $ProjectRoot "recipeparser.spec"
$IssFile     = Join-Path $ProjectRoot "installer.iss"
$OutputDir   = Join-Path $ProjectRoot "output"

# Standard Inno Setup install locations (try both 32-bit and 64-bit Program Files)
$IsccCandidates = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)
$IsccExe = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

# ── Preflight checks ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== RecipeParser local build (dev only — releases go via GitHub Actions) ===" -ForegroundColor Cyan

$tkCheck = python -c "import tkinter; import customtkinter; print('ok')" 2>&1
if ($LASTEXITCODE -ne 0 -or $tkCheck -ne "ok") {
    Write-Host ""
    Write-Host "ERROR: Current Python lacks tkinter or customtkinter (required for GUI)." -ForegroundColor Red
    Write-Host "Install Python from https://www.python.org/downloads/ (ensure 'tcl/tk' is checked)" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error "pyinstaller not found. Run: pip install pyinstaller"
}

if (-not $IsccExe) {
    Write-Error "Inno Setup ISCC.exe not found. Download from https://jrsoftware.org/isdl.php"
}

Write-Host "  PyInstaller : $(pyinstaller --version 2>&1)" -ForegroundColor Green
Write-Host "  ISCC        : $IsccExe" -ForegroundColor Green
Write-Host ""

# ── Step 1: Clean ─────────────────────────────────────────────────────────────
Write-Host "[1/3] Cleaning previous build artefacts..."
foreach ($dir in @("dist", "build", "output")) {
    $path = Join-Path $ProjectRoot $dir
    if (Test-Path $path) {
        Remove-Item -Recurse -Force $path
        Write-Host "      Removed $dir\"
    }
}

# ── Step 2: PyInstaller ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/3] Running PyInstaller..."
Push-Location $ProjectRoot
try {
    pyinstaller $SpecFile --noconfirm
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
Write-Host "[3/3] Compiling installer with Inno Setup..."
New-Item -ItemType Directory -Force $OutputDir | Out-Null

& $IsccExe $IssFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "Inno Setup failed with exit code $LASTEXITCODE"
}

$Installer = Get-ChildItem $OutputDir -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Installer) {
    Write-Error "Installer .exe not found in $OutputDir"
}

# ── Report ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Cyan
$SizeMB = [math]::Round($Installer.Length / 1MB, 1)
Write-Host "  Installer : $($Installer.FullName)" -ForegroundColor Green
Write-Host "  Size      : ${SizeMB} MB"            -ForegroundColor Green
Write-Host ""
Write-Host "  (This build is local only. To publish a release, push a version tag" -ForegroundColor Gray
Write-Host "   and let GitHub Actions create the release.)" -ForegroundColor Gray
Write-Host ""
