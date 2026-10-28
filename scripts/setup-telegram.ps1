# setup-telegram.ps1
# Interactive setup script for Telegram push notifications (spec §8.4, CCC-59).
# Prompts for bot token and chat ID, saving the secret token to data/secrets.json
# (git-ignored) and configuration to data/settings.json.
#
# Settings precedence notes:
# - telegram_enabled uses an OR rule (the UI cannot switch Telegram off while
#   VIBEDESK_TELEGRAM_ENABLED=1).
# - A non-empty settings.json string cannot be cleared back to the env value
#   through the form.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$BotToken = "",

    [Parameter(Mandatory = $false)]
    [string]$ChatId = "",

    [Parameter(Mandatory = $false)]
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = Join-Path $RepoRoot "data"
}

Write-Host "=== Vibe Desk Telegram Setup ==="
Write-Host "This script configures push notifications for Verdicts, Holdout Requests,"
Write-Host "token warnings, and the 21:00 Bangkok daily digest."
Write-Host ""

if ([string]::IsNullOrWhiteSpace($BotToken)) {
    $BotToken = Read-Host -Prompt "Enter your Telegram Bot Token (from @BotFather)"
}

if ([string]::IsNullOrWhiteSpace($ChatId)) {
    $ChatId = Read-Host -Prompt "Enter your Telegram Chat ID"
}

$trimmedToken = $BotToken.Trim()
$trimmedChatId = $ChatId.Trim()

if ([string]::IsNullOrWhiteSpace($trimmedToken) -or [string]::IsNullOrWhiteSpace($trimmedChatId)) {
    Write-Error "Both Telegram Bot Token and Chat ID are required."
    exit 1
}

# Ensure data directory exists
if (-not (Test-Path -Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

$utf8NoBom = New-Object System.Text.UTF8Encoding $false

# 1. Save bot token to data/secrets.json (git-ignored, never logged)
$SecretsFile = Join-Path $DataDir "secrets.json"
$secrets = @{}
if (Test-Path -Path $SecretsFile) {
    try {
        $existing = Get-Content -Path $SecretsFile -Raw | ConvertFrom-Json
        if ($existing) {
            foreach ($prop in $existing.PSObject.Properties) {
                $secrets[$prop.Name] = [string]$prop.Value
            }
        }
    } catch {
        Write-Warning "Could not parse existing secrets.json; creating anew."
    }
}
$secrets["telegram_bot_token"] = $trimmedToken
$secretsJson = $secrets | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($SecretsFile, $secretsJson, $utf8NoBom)

# 2. Save chat_id and telegram_enabled to data/settings.json
$SettingsFile = Join-Path $DataDir "settings.json"
$settings = @{}
if (Test-Path -Path $SettingsFile) {
    try {
        $existingSettings = Get-Content -Path $SettingsFile -Raw | ConvertFrom-Json
        if ($existingSettings) {
            foreach ($prop in $existingSettings.PSObject.Properties) {
                $settings[$prop.Name] = $prop.Value
            }
        }
    } catch {
        Write-Warning "Could not parse existing settings.json; creating anew."
    }
}
$settings["telegram_chat_id"] = $trimmedChatId
$settings["telegram_enabled"] = $true
$settingsJson = $settings | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($SettingsFile, $settingsJson, $utf8NoBom)

Write-Host ""
Write-Host "Telegram configured successfully!"
Write-Host "- Chat ID: $trimmedChatId"
Write-Host "- Bot Token: saved to secrets.json (never printed or committed)"
Write-Host "- Enabled: true (in settings.json)"
Write-Host ""
