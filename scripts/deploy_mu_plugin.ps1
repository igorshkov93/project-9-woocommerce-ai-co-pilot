<#
.SYNOPSIS
    Deploys the Copilot must-use plugin from the repository into the LocalWP site.

.DESCRIPTION
    Files in wp-content/mu-plugins are not tracked by git, so the repository is
    the source of truth and this script pushes changes into WordPress. Run it
    after every edit to the plugin, otherwise WordPress keeps executing the
    previous version and debugging becomes guesswork.

    Dry-run by default, consistent with every other script in this project.
    Pass -Apply to actually copy files.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\deploy_mu_plugin.ps1
    powershell -ExecutionPolicy Bypass -File scripts\deploy_mu_plugin.ps1 -Apply
#>

[CmdletBinding()]
param(
    [string]$SitePublicPath = "C:\Users\Admin\Local Sites\woocommerceautomationtraining\app\public",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Join-Path $repoRoot "wp-plugins"
$sourceLoader = Join-Path $sourceRoot "copilot-loader.php"
$sourcePluginDir = Join-Path $sourceRoot "copilot-abandoned-carts"
$muPluginsDir = Join-Path $SitePublicPath "wp-content\mu-plugins"

Write-Host "Repository root : $repoRoot"
Write-Host "Source          : $sourceRoot"
Write-Host "Target          : $muPluginsDir"
Write-Host ""

if (-not (Test-Path $sourceLoader)) { throw "Loader not found: $sourceLoader" }
if (-not (Test-Path $sourcePluginDir)) { throw "Plugin folder not found: $sourcePluginDir" }
if (-not (Test-Path $SitePublicPath)) { throw "Site path not found: $SitePublicPath" }

if (-not $Apply) {
    Write-Host "DRY RUN. Nothing was copied. Re-run with -Apply to deploy." -ForegroundColor Yellow
    Write-Host "Would copy: copilot-loader.php"
    Write-Host "Would copy: copilot-abandoned-carts\ (recursive)"
    exit 0
}

if (-not (Test-Path $muPluginsDir)) {
    New-Item -ItemType Directory -Force -Path $muPluginsDir | Out-Null
    Write-Host "Created mu-plugins directory."
}

Copy-Item -Path $sourceLoader -Destination $muPluginsDir -Force
Copy-Item -Path $sourcePluginDir -Destination $muPluginsDir -Recurse -Force

Write-Host "Deployed." -ForegroundColor Green
Get-ChildItem -Path $muPluginsDir -Recurse -Filter *.php | Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize