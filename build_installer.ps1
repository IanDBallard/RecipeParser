#Requires -Version 5.1
<#
.SYNOPSIS
    One-click build pipeline: PyInstaller bundle -> Inno Setup installer.

.DESCRIPTION
    1. Cleans previous build artefacts (dist\, build\, output\)
    2. Runs PyInstaller using recipeparser.spec
    3. Compiles installer.iss with Inno Setup's ISCC.exe
    4. Reports the path of the finished installer

.NOTES
    Prerequisites (one-time installs):
      pip install pyinstaller
      Inno Setup 6  — https://jrsoftware.org/isdl.php
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
Write-Host "=== RecipeParser installer build ===" -ForegroundColor Cyan

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error "pyinstaller not found on PATH. Run: pip install pyinstaller"
}

if (-not $IsccExe) {
    Write-Error "Inno Setup ISCC.exe not found. Download from https://jrsoftware.org/isdl.php"
}

Write-Host "  PyInstaller : $(pyinstaller --version 2>&1)" -ForegroundColor Green
Write-Host "  ISCC        : $IsccExe"                      -ForegroundColor Green
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
Write-Host "[3/3] Compiling installer with Inno Setup..."
New-Item -ItemType Directory -Force $OutputDir | Out-Null

& $IsccExe $IssFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "Inno Setup failed with exit code $LASTEXITCODE"
}

# ── Report ────────────────────────────────────────────────────────────────────
$Installer = Get-ChildItem $OutputDir -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Cyan
if ($Installer) {
    $SizeMB = [math]::Round($Installer.Length / 1MB, 1)
    Write-Host "  Installer : $($Installer.FullName)" -ForegroundColor Green
    Write-Host "  Size      : ${SizeMB} MB"            -ForegroundColor Green
} else {
    Write-Warning "Installer .exe not found in $OutputDir - check Inno Setup output above."
}
Write-Host ""
