"""Public, unauthenticated install-script delivery.

These routes hand back a plain-text setup script so a fresh machine can
bootstrap a client with a single `irm <url> | iex`. They carry NO secrets and
no per-session identifiers: the script prompts for both the endpoint URL and
the access token at runtime, so the same hosted file works for anyone.
"""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()

# The on-box coder is pinned server-side; the proxy overwrites the caller's
# `model` (external_llm.py) and only advertises the real one via /v1/models.
# So the script discovers the name at runtime rather than hard-coding it.
_GROK_PS1 = r"""# =============================================
# Grok Build CLI + Hopkins Brain Setup
# Run in normal PowerShell (no admin needed):
#   irm https://hopkinsbrain.com/api/install/grok.ps1 | iex
# =============================================

$ErrorActionPreference = "Stop"
Write-Host "=== Setting up Grok Build CLI for Hopkins Brain ===" -ForegroundColor Green

# 1. Endpoint URL: prompt for the OpenAI base URL (the one ending in /v1)
$BaseUrl = (Read-Host "Hopkins Brain endpoint URL (ends in /v1)").Trim().TrimEnd("/")
if ([string]::IsNullOrWhiteSpace($BaseUrl)) { throw "No URL provided." }
if ($BaseUrl -notmatch "^https?://") { throw "URL must start with http:// or https://" }
if ($BaseUrl -notmatch "/v1$") {
    Write-Host "Note: URL does not end in /v1." -ForegroundColor Yellow
}

# 2. Install / update Grok Build (idempotent)
Write-Host "Installing/updating Grok Build CLI..." -ForegroundColor Cyan
irm https://x.ai/cli/install.ps1 | iex

# 3. Token: prompt, store as a persistent USER env var (no profile edits)
$existing = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
if ($existing) {
    Write-Host "Found an existing OPENAI_API_KEY. Press Enter to keep it, or paste a new one." `
        -ForegroundColor Yellow
}
$ApiKey = Read-Host "Hopkins Brain token (Enter to keep existing)"
if ([string]::IsNullOrWhiteSpace($ApiKey)) { $ApiKey = $existing }
if ([string]::IsNullOrWhiteSpace($ApiKey)) { throw "No token provided." }

[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $ApiKey, "User")
$env:OPENAI_API_KEY = $ApiKey   # current session too
Write-Host "Token stored as a User environment variable." -ForegroundColor Green

# 4. Connectivity check + discover the model name the server pins
Write-Host "Checking endpoint and discovering model..." -ForegroundColor Cyan
$AuthHeader = @{ Authorization = "Bearer $ApiKey" }
try {
    $models = Invoke-RestMethod -Uri "$BaseUrl/models" -Headers $AuthHeader
    $ModelName = $models.data[0].id
    Write-Host "Endpoint OK. Server model: $ModelName" -ForegroundColor Green
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 401) { throw "401 Unauthorized - the token is wrong, disabled, or expired." }
    elseif ($code -eq 503) { throw "503 - coder model not loaded; start it on the box, re-run." }
    else { throw "Could not reach the endpoint: $_" }
}

# 5. Write config.toml (UTF-8, no BOM - some TOML parsers choke on a BOM)
$ConfigDir = Join-Path $HOME ".grok"
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
$ConfigPath = Join-Path $ConfigDir "config.toml"
$ConfigContent = @"
[model.hopkinsbrain]
model = "$ModelName"
base_url = "$BaseUrl"
name = "Hopkins Brain Custom"
env_key = "OPENAI_API_KEY"

[models]
default = "hopkinsbrain"
"@
[System.IO.File]::WriteAllText($ConfigPath, $ConfigContent)
Write-Host "Config written to: $ConfigPath" -ForegroundColor Green

# 6. Verify + launch
Write-Host "`nVerifying..." -ForegroundColor Cyan
grok inspect
Write-Host "`n=== Done. Launching Grok ===" -ForegroundColor Green
grok
"""


@router.get("/install/grok.ps1")
async def grok_install_script() -> PlainTextResponse:
    """Windows PowerShell setup script for the Grok Build CLI.

    Public on purpose so `irm .../install/grok.ps1 | iex` works on a bare box.
    """
    return PlainTextResponse(_GROK_PS1, media_type="text/plain; charset=utf-8")
