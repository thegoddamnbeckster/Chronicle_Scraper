# Chronicle Scraper - Build Script
# Reads id/version from each addon's own addon.xml automatically. Builds
# BOTH addons -- script.chronicle.scraper.movie (this directory) and
# script.chronicle.scraper.tv (tv_addon/) -- since Kodi resolves which
# script to run for a scrape by addon id alone, not by which extension
# point triggered the call: one addon declaring both
# xbmc.metadata.scraper.movies and xbmc.metadata.scraper.tvshows silently
# runs the movies script for every scrape, TV included (confirmed live,
# 2026-08-23 -- see addon.xml's own v3.0.0 changelog entry). They have to
# ship as two separate addon packages; this script reflects that.
# Outputs both ZIPs to C:\Temp\.
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$repoRoot  = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputDir = "C:\Temp"

function Build-ChronicleAddon {
    param(
        [Parameter(Mandatory)][string]$AddonRoot,   # folder containing this addon's own addon.xml
        [Parameter(Mandatory)][string[]]$RootFiles   # root-level files this addon actually has (varies: the
                                                       # movie addon has default.py/service.py, the TV addon doesn't)
    )

    $addonXml = Join-Path $AddonRoot "addon.xml"
    if (-not (Test-Path $addonXml)) {
        Write-Error "No addon.xml found at $AddonRoot"
        exit 1
    }

    $xml     = [xml](Get-Content $addonXml -Encoding UTF8)
    $version = $xml.addon.version
    $addonId = $xml.addon.id
    if (-not $version) { Write-Error "Could not read version from $addonXml"; exit 1 }
    if (-not $addonId) { Write-Error "Could not read addon id from $addonXml"; exit 1 }

    Write-Host "======================================"
    Write-Host " $addonId v$version - Build"
    Write-Host "======================================"

    $buildTemp  = Join-Path $repoRoot "build_temp_$addonId"
    $sourcePath = Join-Path $buildTemp $addonId
    $outputZip  = Join-Path $outputDir "$addonId-$version.zip"

    # Step 1: Prepare build_temp
    Write-Host ""
    Write-Host "--- Preparing build_temp ---"
    if (Test-Path $buildTemp) {
        Remove-Item $buildTemp -Recurse -Force
        Write-Host "  Cleaned old build_temp"
    }
    New-Item -ItemType Directory -Path $sourcePath -Force | Out-Null

    foreach ($f in $RootFiles) {
        $src = Join-Path $AddonRoot $f
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $sourcePath $f)
            Write-Host "  Copied: $f"
        } else {
            Write-Warning "  Missing expected file: $f"
        }
    }

    # Copy lib/, python/, and resources/ (excluding __pycache__) -- every
    # Chronicle Scraper addon has all three, unlike the root-file list above.
    foreach ($dir in @("lib", "python", "resources")) {
        Copy-Item (Join-Path $AddonRoot $dir) (Join-Path $sourcePath $dir) -Recurse -Force
    }
    Get-ChildItem -Path $sourcePath -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force
    Write-Host "  Copied: lib/, python/, resources/ (cleaned __pycache__)"
    Write-Host "  build_temp ready."

    # Step 2: Create ZIP
    Write-Host ""
    Write-Host "--- Building ZIP ---"
    if (-not (Test-Path $outputDir)) {
        New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    }
    if (Test-Path $outputZip) {
        Remove-Item $outputZip -Force
        Write-Host "  Removed old ZIP"
    }

    $zip   = [System.IO.Compression.ZipFile]::Open($outputZip, 'Create')
    $files = Get-ChildItem -Path $sourcePath -Recurse -File
    $count = 0
    foreach ($file in $files) {
        $relativePath = $file.FullName.Substring($buildTemp.Length + 1)
        $entryName    = $relativePath.Replace("\", "/")
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $entryName, 'Optimal') | Out-Null
        Write-Host "  + $entryName"
        $count++
    }
    $zip.Dispose()

    $zipInfo = Get-Item $outputZip
    $zipSize = [math]::Round($zipInfo.Length / 1024, 2)

    Write-Host ""
    Write-Host " BUILD COMPLETE: $addonId"
    Write-Host " Version: $version"
    Write-Host " Output:  $outputZip"
    Write-Host " Files:   $count"
    Write-Host " Size:    $zipSize KB"
    Write-Host ""

    Remove-Item $buildTemp -Recurse -Force
    Write-Host "  Cleaned build_temp"
    Write-Host ""

    return $outputZip
}

$movieZip = Build-ChronicleAddon -AddonRoot $repoRoot -RootFiles @("addon.xml", "default.py", "service.py", "icon.png", "LICENSE")
$tvZip    = Build-ChronicleAddon -AddonRoot (Join-Path $repoRoot "tv_addon") -RootFiles @("addon.xml", "icon.png", "LICENSE")

Write-Host "======================================"
Write-Host " ALL BUILDS COMPLETE"
Write-Host "======================================"
Write-Host "Movie addon: $movieZip"
Write-Host "TV addon:    $tvZip"
