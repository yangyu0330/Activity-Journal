param()

$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ExtensionDir = Join-Path $Repo "browser_extension\chatgpt-live-capture"

if (-not (Test-Path -LiteralPath $ExtensionDir)) {
    throw "Extension directory not found: $ExtensionDir"
}

Write-Host "Opening Chrome extension management."
Write-Host "Enable Developer mode, choose Load unpacked, then select:"
Write-Host "  $ExtensionDir"
Write-Host "The local receiver should already be running at http://127.0.0.1:8765."

Start-Process "chrome.exe" "chrome://extensions/"
Start-Process $ExtensionDir
