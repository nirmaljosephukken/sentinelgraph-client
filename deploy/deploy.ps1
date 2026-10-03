# Deploy SentinelGraph (client edition) to Cloud Run. Service, topic, subscription, secret and Firestore
# collection names all derive from -Service (default sentinelgraph-client), so it never touches the hackathon deployment.
# (Windows PowerShell, run from the repo root).
#
#   .\deploy\deploy.ps1 -Project my-gcp-project
#   .\deploy\deploy.ps1 -Project my-gcp-project -Region asia-south1 -Model gemini-3.5-flash-lite
#
# What it does:
#   1. enables Cloud Run, Cloud Build, Artifact Registry, Secret Manager and Vertex AI
#   2. copies TG_SECRET from .env into Secret Manager (value never printed); Gemini runs on Vertex AI, no API key
#   3. creates a service account that may read those secrets and call Gemini on Vertex AI
#   4. builds the Dockerfile with Cloud Build and deploys the console to Cloud Run
#   5. deploys the same image as a private worker, with a Firestore database and a Pub/Sub topic + push
#      subscription for the live alert stream
#   6. deploys the same image as the client intake API (public URL, every call needs an API key)
#
# Client data goes into its own TigerGraph graph. Create it once (python graph/setup_graph.py --empty with
# TG_GRAPH=<name> in .env), then deploy with -ClientGraph <name>. Without it the deployment runs on the demo graph
# and the intake refuses to load client data.
# Re-running is safe: existing secrets get a new version, existing roles are kept.

param(
    [Parameter(Mandatory = $true)][string]$Project,
    [string]$Region = "asia-south1",
    [string]$Service = "sentinelgraph-client",
    [string]$Model = "",
    [string]$VertexLocation = "global",
    [switch]$AllowDevAuth,
    [string]$ClientGraph = ""
)
$ErrorActionPreference = "Stop"

# ---- read .env (KEY=VALUE lines) without echoing anything
$envFile = Join-Path (Get-Location) ".env"
if (-not (Test-Path $envFile)) { throw "No .env in $(Get-Location). Run this from the repo root." }
$cfg = @{}
Get-Content $envFile | ForEach-Object {
    $l = $_.Trim()
    if ($l -and -not $l.StartsWith("#") -and $l.Contains("=")) {
        $k, $v = $l.Split("=", 2)
        $cfg[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
    }
}
foreach ($k in @("TG_HOST", "TG_SECRET")) { if (-not $cfg[$k]) { throw "$k missing in .env" } }
$oidc = [bool]($cfg["AUTH_CLIENT_ID"] -and $cfg["AUTH_CLIENT_SECRET"] -and $cfg["ADMIN_EMAILS"])
if (-not $oidc -and -not $AllowDevAuth) {
    throw ("Sign-in is not configured. Add AUTH_CLIENT_ID, AUTH_CLIENT_SECRET and ADMIN_EMAILS to .env " +
           "(see docs/production.md, 'Sign-in'), or pass -AllowDevAuth for a demo deployment without real sign-in.")
}
$graph = if ($cfg["TG_GRAPH"]) { $cfg["TG_GRAPH"] } else { "FraudGraph" }
if ($ClientGraph) {
    if ($ClientGraph -eq "FraudGraph") { throw "-ClientGraph must not be the demo graph FraudGraph." }
    $graph = $ClientGraph
}
if (-not $Model) { $Model = if ($cfg["GEMINI_MODEL"]) { $cfg["GEMINI_MODEL"] } else { "gemini-3.5-flash-lite" } }

function Test-Gcloud([string[]]$GArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & gcloud @GArgs *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $old
    return $ok
}

gcloud config set project $Project | Out-Null
$topic = "$Service-alerts"; $sub = "$Service-alerts-worker"; $tgSecret = "$Service-tg-secret"; $collection = "$Service-live-alerts"
$number = gcloud projects describe $Project --format="value(projectNumber)"

Write-Host "1/5 Enabling APIs ..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    secretmanager.googleapis.com aiplatform.googleapis.com pubsub.googleapis.com firestore.googleapis.com `
    cloudscheduler.googleapis.com

Write-Host "2/5 Secrets ..."
function Set-Secret([string]$name, [string]$value) {
    $tmp = New-TemporaryFile
    [IO.File]::WriteAllText($tmp.FullName, $value)   # no trailing newline
    if (Test-Gcloud @("secrets", "describe", $name)) { gcloud secrets versions add $name --data-file="$($tmp.FullName)" | Out-Null }
    else { gcloud secrets create $name --replication-policy=automatic --data-file="$($tmp.FullName)" | Out-Null }
    Remove-Item $tmp.FullName -Force
}
Set-Secret $tgSecret $cfg["TG_SECRET"]
$secrets = "TG_SECRET=${tgSecret}:latest"
# No Gemini API key in the cloud: the agent and the case writer both call Gemini on Vertex AI as the service account.

$authVars = ",AUTH_MODE=dev,ALLOW_DEV_AUTH=1"
if ($oidc) {
    Set-Secret "$Service-auth-client-secret" $cfg["AUTH_CLIENT_SECRET"]
    if (-not (Test-Gcloud @("secrets", "describe", "$Service-auth-cookie"))) {
        $b = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
        Set-Secret "$Service-auth-cookie" ([Convert]::ToBase64String($b))
    }
    $secrets += ",AUTH_CLIENT_SECRET=$Service-auth-client-secret:latest,AUTH_COOKIE_SECRET=$Service-auth-cookie:latest"
    $redirect = "https://$Service-$number.$Region.run.app/oauth2callback"
    $admins = $cfg["ADMIN_EMAILS"] -replace ",", ";"
    $authVars = ",AUTH_MODE=oidc,AUTH_CLIENT_ID=$($cfg['AUTH_CLIENT_ID']),AUTH_REDIRECT_URI=$redirect,ADMIN_EMAILS=$admins"
    Write-Host "   Sign-in: Google OIDC, redirect URI $redirect (add it to the OAuth client if you have not)"
} else {
    Write-Host "   WARNING: deploying with development sign-in (-AllowDevAuth). Not for real users."
}

# Assist mode: actions go to the bank's systems through a signed webhook (docs/production.md, "Operating modes")
$hookVars = ""
$hookSecret = $false
if ($cfg["ACTION_WEBHOOK_URL"]) {
    $hookVars = ",ACTION_WEBHOOK_URL=$($cfg['ACTION_WEBHOOK_URL'])"
    if ($cfg["ACTION_WEBHOOK_SECRET"]) {
        Set-Secret "$Service-webhook-secret" $cfg["ACTION_WEBHOOK_SECRET"]
        $secrets += ",ACTION_WEBHOOK_SECRET=$Service-webhook-secret:latest"
        $hookSecret = $true
    }
}

Write-Host "3/5 Service account, Firestore, Pub/Sub ..."
$sa = "$Service-run@$Project.iam.gserviceaccount.com"
$saExists = gcloud iam service-accounts list --filter="email=$sa" --format="value(email)"
if (-not $saExists) { gcloud iam service-accounts create "$Service-run" --display-name="SentinelGraph Cloud Run" | Out-Null }
foreach ($sec in @($tgSecret) + $(if ($oidc) { @("$Service-auth-client-secret", "$Service-auth-cookie") } else { @() }) +
                 $(if ($hookSecret) { @("$Service-webhook-secret") } else { @() })) {
    gcloud secrets add-iam-policy-binding $sec --member="serviceAccount:$sa" `
        --role="roles/secretmanager.secretAccessor" --condition=None | Out-Null
}
foreach ($role in @("roles/aiplatform.user", "roles/pubsub.publisher", "roles/datastore.user")) {
    gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$sa" --role=$role `
        --condition=None | Out-Null
}

# live alert stream: Firestore database + Pub/Sub topic
if (-not (Test-Gcloud @("firestore", "databases", "describe", "--database=(default)"))) {
    gcloud firestore databases create --location=$Region --type=firestore-native --quiet
}
if (-not (Test-Gcloud @("pubsub", "topics", "describe", $topic))) {
    gcloud pubsub topics create $topic | Out-Null
}
# Cloud Build (source deploys) runs as the default compute service account on newer projects
gcloud projects add-iam-policy-binding $Project `
    --member="serviceAccount:$number-compute@developer.gserviceaccount.com" `
    --role="roles/run.builder" --condition=None | Out-Null

Write-Host "4/6 Build and deploy the console (takes a few minutes) ..."
# create the image repository up front: letting `run deploy` create it on a new project can time out
if (-not (Test-Gcloud @("artifacts", "repositories", "describe", "cloud-run-source-deploy", "--location=$Region"))) {
    gcloud artifacts repositories create cloud-run-source-deploy --repository-format=docker `
        --location=$Region --description="Cloud Run source deploys" --quiet
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Artifact Registry repository. Wait a few minutes and run the script again." }
}
$vars = "TG_HOST=$($cfg['TG_HOST']),TG_GRAPH=$graph,GOOGLE_GENAI_USE_VERTEXAI=TRUE," +
        "GOOGLE_CLOUD_PROJECT=$Project,GOOGLE_CLOUD_LOCATION=$VertexLocation,ADK_MODEL=$Model,GEMINI_MODEL=$Model," +
        "LLM_PROVIDER=gemini,LIVE_BACKEND=gcp,PUBSUB_TOPIC=$topic,FIRESTORE_COLLECTION=$collection,ACCESS_PREFIX=$Service" + $authVars +
        $(if ($ClientGraph) { ",DATA_SOURCE=client" } else { "" }) + $hookVars
gcloud run deploy $Service --source . --region $Region --service-account $sa `
    --set-env-vars $vars --set-secrets $secrets `
    --allow-unauthenticated --session-affinity --timeout 3600 `
    --cpu 2 --memory 2Gi --min-instances 0 --max-instances 2 --concurrency 20
if ($LASTEXITCODE -ne 0) { throw "Deploy failed (see the error above). Running the script again is safe." }

Write-Host "5/6 Live stream worker ..."
# same image as the console, started as the FastAPI worker; private, called only by Pub/Sub
$image = gcloud run services describe $Service --region $Region --format="value(spec.template.spec.containers[0].image)"
$worker = "$Service-worker"
gcloud run deploy $worker --image $image --region $Region --service-account $sa `
    --command uvicorn --args "worker.app:app,--host,0.0.0.0,--port,8080" `
    --set-env-vars $vars --set-secrets $secrets --no-allow-unauthenticated `
    --timeout 600 --cpu 2 --memory 2Gi --min-instances 0 --max-instances 3 --concurrency 1
if ($LASTEXITCODE -ne 0) { throw "Worker deploy failed (see the error above). Running the script again is safe." }
gcloud run services add-iam-policy-binding $worker --region $Region --member="serviceAccount:$sa" `
    --role="roles/run.invoker" | Out-Null
$workerUrl = gcloud run services describe $worker --region $Region --format="value(status.url)"
if (Test-Gcloud @("pubsub", "subscriptions", "describe", $sub)) {
    gcloud pubsub subscriptions update $sub --push-endpoint="$workerUrl/pubsub" `
        --push-auth-service-account=$sa --push-auth-token-audience=$workerUrl | Out-Null
} else {
    gcloud pubsub subscriptions create $sub --topic=$topic --push-endpoint="$workerUrl/pubsub" `
        --push-auth-service-account=$sa --push-auth-token-audience=$workerUrl --ack-deadline=600 --min-retry-delay=10s --max-retry-delay=300s | Out-Null
}

# R4: every 15 minutes, cases whose cardholder reply is overdue continue as "no reply"
$job = "$Service-reply-sweep"
if (Test-Gcloud @("scheduler", "jobs", "describe", $job, "--location=$Region")) {
    gcloud scheduler jobs update http $job --location=$Region --schedule="*/15 * * * *" --uri="$workerUrl/tasks/sweep" `
        --http-method=POST --oidc-service-account-email=$sa --oidc-token-audience=$workerUrl | Out-Null
} else {
    gcloud scheduler jobs create http $job --location=$Region --schedule="*/15 * * * *" --uri="$workerUrl/tasks/sweep" `
        --http-method=POST --oidc-service-account-email=$sa --oidc-token-audience=$workerUrl | Out-Null
}

Write-Host "6/6 Intake API ..."
# public URL so the client's systems can reach it; app-level API keys (Team & access page) guard every call
$api = "$Service-api"
gcloud run deploy $api --image $image --region $Region --service-account $sa `
    --command uvicorn --args "api.app:app,--host,0.0.0.0,--port,8080" `
    --set-env-vars $vars --set-secrets $secrets --allow-unauthenticated `
    --timeout 900 --cpu 2 --memory 2Gi --min-instances 0 --max-instances 2 --concurrency 4
if ($LASTEXITCODE -ne 0) { throw "API deploy failed (see the error above). Running the script again is safe." }
$apiUrl = gcloud run services describe $api --region $Region --format="value(status.url)"

$url = gcloud run services describe $Service --region $Region --format="value(status.url)"
Write-Host ""
Write-Host "Deployed: $url"
Write-Host "Worker:   $workerUrl (private, fed by Pub/Sub topic $topic)"
Write-Host "API:      $apiUrl/docs (create a key on the console's Team & access page)"
if (-not $ClientGraph) { Write-Host "Note: running on the demo graph; client data loads are refused until you deploy with -ClientGraph." }
Write-Host "Monitoring: .\deploy\monitoring.ps1 -Project $Project -Email <your email>   (once; re-run after changes)"
