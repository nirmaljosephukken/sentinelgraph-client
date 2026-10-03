# Cloud Monitoring for a SentinelGraph client deployment (run after deploy.ps1; Windows PowerShell, from the repo root).
#
#   .\deploy\monitoring.ps1 -Project sentinel-graph -Email you@example.com
#
# Creates or updates, for the services <Service>, <Service>-worker and <Service>-api:
#   * an email notification channel
#   * log-based metrics from the services' structured logs (agent/obs.py): deploy/monitoring/metrics/*.json
#   * uptime checks for the console and the intake API (every 15 minutes, from several regions)
#   * alert policies with runbooks: deploy/monitoring/policies/*.json
#   * a dashboard: deploy/monitoring/dashboard.json
# Re-running is safe: existing items with the same name are updated, nothing is duplicated.

param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Email,
    [string]$Region = "asia-south1",
    [string]$Service = "sentinelgraph-client"
)
$ErrorActionPreference = "Stop"
$here = Join-Path (Get-Location) "deploy\monitoring"
if (-not (Test-Path $here)) { throw "Run this from the repo root (deploy\monitoring not found)." }

gcloud config set project $Project | Out-Null
gcloud services enable monitoring.googleapis.com logging.googleapis.com | Out-Null
$token = gcloud auth print-access-token
$H = @{ Authorization = "Bearer $token"; "x-goog-user-project" = $Project }
$M3 = "https://monitoring.googleapis.com/v3/projects/$Project"
$M1 = "https://monitoring.googleapis.com/v1/projects/$Project"

function Api([string]$Method, [string]$Url, [string]$Body = $null) {
    $req = @{ Method = $Method; Uri = $Url; Headers = $H }
    if ($Body) { $req.Body = [Text.Encoding]::UTF8.GetBytes($Body); $req.ContentType = "application/json; charset=utf-8" }
    return Invoke-RestMethod @req
}
function Enc([string]$s) { return [uri]::EscapeDataString($s) }

$consoleUrl = gcloud run services describe $Service --region $Region --format="value(status.url)"
$apiUrl = gcloud run services describe "$Service-api" --region $Region --format="value(status.url)"
if (-not $consoleUrl) { throw "Service $Service not found in $Region. Run deploy.ps1 first." }
$prefix = $Service -replace "-", "_"
$vars = @{ "{{PROJECT}}" = $Project; "{{SERVICE}}" = $Service; "{{PREFIX}}" = $prefix;
           "{{SUBSCRIPTION}}" = "$Service-alerts-worker"; "{{CONSOLE_URL}}" = $consoleUrl }
function Fill([string]$text) { foreach ($k in $vars.Keys) { $text = $text.Replace($k, $vars[$k]) }; return $text }

Write-Host "1/5 Notification channel for $Email ..."
$chs = Api GET "$M3/notificationChannels?filter=$(Enc 'type="email"')"
$ch = @($chs.notificationChannels) | Where-Object { $_.labels.email_address -eq $Email } | Select-Object -First 1
if (-not $ch) {
    $ch = Api POST "$M3/notificationChannels" (@{ type = "email"; displayName = "SentinelGraph alerts ($Email)";
                                                  labels = @{ email_address = $Email } } | ConvertTo-Json)
    Write-Host "   created; Google sends a confirmation email to $Email"
}
$vars["{{CHANNEL}}"] = $ch.name

Write-Host "2/5 Log-based metrics ..."
$existing = @(gcloud logging metrics list --format="value(name)")
foreach ($f in Get-ChildItem (Join-Path $here "metrics") -Filter *.json) {
    $cfg = Fill (Get-Content $f.FullName -Raw)
    $name = ($cfg | ConvertFrom-Json).name
    $tmp = New-TemporaryFile
    [IO.File]::WriteAllText($tmp.FullName, $cfg)
    if ($existing -contains $name) { gcloud logging metrics update $name --config-from-file="$($tmp.FullName)" | Out-Null }
    else { gcloud logging metrics create $name --config-from-file="$($tmp.FullName)" | Out-Null }
    Remove-Item $tmp.FullName -Force
    Write-Host "   $name"
}

Write-Host "3/5 Uptime checks ..."
$checks = @(Api GET "$M3/uptimeCheckConfigs").uptimeCheckConfigs
function Uptime([string]$display, [string]$url, [string]$path) {
    $hostName = ([uri]$url).Host
    $found = @($checks) | Where-Object { $_.displayName -eq $display } | Select-Object -First 1
    if ($found) { return ($found.name -split "/")[-1] }
    $body = @{ displayName = $display; period = "900s"; timeout = "10s";
               monitoredResource = @{ type = "uptime_url"; labels = @{ project_id = $Project; host = $hostName } };
               httpCheck = @{ path = $path; port = 443; useSsl = $true; validateSsl = $true; requestMethod = "GET" } } |
            ConvertTo-Json -Depth 6
    $made = Api POST "$M3/uptimeCheckConfigs" $body
    return ($made.name -split "/")[-1]
}
$vars["{{CONSOLE_CHECK}}"] = Uptime "$Service console" $consoleUrl "/_stcore/health"
$vars["{{API_CHECK}}"] = if ($apiUrl) { Uptime "$Service intake API" $apiUrl "/v1/health" } else { "none" }
Write-Host "   console: $($vars['{{CONSOLE_CHECK}}'])  api: $($vars['{{API_CHECK}}'])"

Write-Host "4/5 Alert policies ..."
foreach ($f in Get-ChildItem (Join-Path $here "policies") -Filter *.json) {
    if ($f.BaseName -eq "api_down" -and -not $apiUrl) { continue }
    $body = Fill (Get-Content $f.FullName -Raw)
    $display = ($body | ConvertFrom-Json).displayName
    $same = Api GET "$M3/alertPolicies?filter=$(Enc ('display_name="' + $display + '"'))"
    $old = @($same.alertPolicies) | Select-Object -First 1
    if ($old) { Api PATCH "https://monitoring.googleapis.com/v3/$($old.name)" $body | Out-Null }
    else { Api POST "$M3/alertPolicies" $body | Out-Null }
    Write-Host "   $display"
}

Write-Host "5/5 Dashboard ..."
$dash = Fill (Get-Content (Join-Path $here "dashboard.json") -Raw)
$display = ($dash | ConvertFrom-Json).displayName
$old = @((Api GET "$M1/dashboards").dashboards) | Where-Object { $_.displayName -eq $display } | Select-Object -First 1
if ($old) {
    $obj = $dash | ConvertFrom-Json
    $obj | Add-Member -NotePropertyName etag -NotePropertyValue $old.etag -Force
    Api PATCH "https://monitoring.googleapis.com/v1/$($old.name)" ($obj | ConvertTo-Json -Depth 30) | Out-Null
} else { Api POST "$M1/dashboards" $dash | Out-Null }

Write-Host ""
Write-Host "Done. Alerts go to $Email (confirm the email Google sends). Dashboard: Cloud Console > Monitoring > Dashboards > $display"
