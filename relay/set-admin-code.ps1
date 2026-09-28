# Sets the owner code that unlocks editing and deleting recipes on the site.
# Double-click "Set-Admin-Code.cmd". The code is typed into a pop-up and stored only in Cloudflare.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Ask($msg) {
    $c = Get-Credential -UserName 'Owner code' -Message $msg
    if (-not $c) { Write-Host 'Cancelled. Nothing was saved.' -ForegroundColor Yellow; exit 1 }
    return $c.GetNetworkCredential().Password.Trim()
}
$a = Ask 'Type the code you want for editing recipes into the PASSWORD box (at least 8 characters), then click OK.'
if ($a.Length -lt 8) { Write-Host 'Use at least 8 characters. Nothing was saved.' -ForegroundColor Red; exit 1 }
$b = Ask 'Type the same code again to confirm.'
if ($a -ne $b) { Write-Host 'The two codes did not match. Nothing was saved.' -ForegroundColor Red; exit 1 }
$a | npx.cmd wrangler secret put ADMIN_CODE
if ($LASTEXITCODE -ne 0) { Write-Host 'Cloudflare did not accept it. Nothing was changed.' -ForegroundColor Red; exit 1 }
Remove-Variable a, b
Write-Host ''
Write-Host 'DONE. Your owner code is set. Tell Jarvis "code set".' -ForegroundColor Green
