# Stores the relay's two secret keys in Cloudflare under the right names.
# Easiest: double-click "Set-Relay-Secrets.cmd" in this folder.
# Each key is asked for in a pop-up box (paste it into the Password field). Nothing is saved on this PC.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Ask-Secret($title, $message) {
    $cred = Get-Credential -UserName $title -Message $message
    if (-not $cred) { Write-Host 'Cancelled. Nothing was saved.' -ForegroundColor Yellow; exit 1 }
    return $cred.GetNetworkCredential().Password.Trim()
}

Write-Host 'Step 1 of 2: a pop-up box is asking for the GitHub token...' -ForegroundColor Cyan
$gh = Ask-Secret 'GitHub token' 'Paste the GitHub token (starts with github_pat_) into the PASSWORD box, then click OK.'
if (-not $gh.StartsWith('github_pat_')) { Write-Host 'That does not look like a GitHub token (should start with github_pat_). Nothing was saved.' -ForegroundColor Red; exit 1 }
$gh | npx.cmd wrangler secret put GH_TOKEN
if ($LASTEXITCODE -ne 0) { Write-Host 'Cloudflare did not accept the GitHub token. Nothing else was changed.' -ForegroundColor Red; exit 1 }
Remove-Variable gh

Write-Host ''
Write-Host 'Step 2 of 2: a pop-up box is asking for the Turnstile Secret Key...' -ForegroundColor Cyan
$ts = Ask-Secret 'Turnstile secret' 'Paste the Turnstile SECRET Key (starts with 0x) into the PASSWORD box, then click OK.'
if (-not $ts.StartsWith('0x')) { Write-Host 'That does not look like a Turnstile secret (should start with 0x). Nothing was saved.' -ForegroundColor Red; exit 1 }
$ts | npx.cmd wrangler secret put TURNSTILE_SECRET
if ($LASTEXITCODE -ne 0) { Write-Host 'Cloudflare did not accept the Turnstile secret.' -ForegroundColor Red; exit 1 }
Remove-Variable ts

Write-Host ''
Write-Host 'DONE. Both keys are stored in Cloudflare as GH_TOKEN and TURNSTILE_SECRET. Tell Jarvis "done".' -ForegroundColor Green
