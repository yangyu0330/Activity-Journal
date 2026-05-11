param(
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

if ($VerifyOnly) {
    if ([Environment]::GetEnvironmentVariable("NOTION_TOKEN", "User")) {
        Write-Host "NOTION_TOKEN is set for the current Windows user."
    } else {
        Write-Host "NOTION_TOKEN is missing for the current Windows user."
    }
    exit 0
}

$SecureToken = Read-Host "Paste Notion integration token, then press Enter" -AsSecureString
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)

try {
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    throw "Token is empty. Nothing was saved."
}

[Environment]::SetEnvironmentVariable("NOTION_TOKEN", $Token, "User")
[Environment]::SetEnvironmentVariable("NOTION_TOKEN", $Token, "Process")

$SavedUserToken = [Environment]::GetEnvironmentVariable("NOTION_TOKEN", "User")
$SavedProcessToken = [Environment]::GetEnvironmentVariable("NOTION_TOKEN", "Process")

if ([string]::IsNullOrWhiteSpace($SavedUserToken)) {
    throw "Failed to save NOTION_TOKEN for the current Windows user."
}

Write-Host "Saved NOTION_TOKEN for the current Windows user."
Write-Host "User token length: $($SavedUserToken.Length)"
Write-Host "Current PowerShell token length: $($SavedProcessToken.Length)"
Write-Host "Scheduled tasks may need a fresh logon session on some Windows setups."
