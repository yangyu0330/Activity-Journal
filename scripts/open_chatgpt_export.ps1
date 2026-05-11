param(
    [switch]$PrivacyPortalOnly
)

$ErrorActionPreference = "Stop"

$PrivacyPortal = "https://privacy.openai.com/"
$ChatGptSettings = "https://chatgpt.com/"

Write-Host "Opening the official OpenAI privacy/export flow."
Write-Host "No password, token, or browser cookie is stored by this script."
Write-Host "After the export email arrives, place the .zip or conversations.json under imports\chatgpt."

Start-Process $PrivacyPortal

if (-not $PrivacyPortalOnly) {
    Start-Process $ChatGptSettings
}
