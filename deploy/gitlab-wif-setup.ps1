# One-time Google Cloud setup so GitLab CI/CD can deploy SentinelGraph to Cloud Run WITHOUT any service-account key.
# GitLab's job ID token (OIDC) is exchanged for a deploy service account through Workload Identity Federation.
#
#   .\deploy\gitlab-wif-setup.ps1 -Project <gcp-project-id> -GitLabProjectPath nekku-solo/sentinelgraph-client
#
# Run once from PowerShell with gcloud signed in as a project owner. Re-running is safe.
# At the end it prints the six CI/CD variables to add to the GitLab project (none of them is a secret).

param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$GitLabProjectPath,   # e.g. nekku-solo/sentinelgraph-client
    [string]$Region = "asia-south1",
    [string]$Pool = "gitlab-pool",
    [string]$Provider = "gitlab",
    [string]$ServiceAccount = "gitlab-deploy",
    [string]$GitLabHost = "gitlab.com"
)
$ErrorActionPreference = "Stop"

function Test-Gcloud([string[]]$GArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & gcloud @GArgs *> $null
    $ok = ($LASTEXITCODE -eq 0); $ErrorActionPreference = $old; return $ok
}

gcloud config set project $Project | Out-Null
$number = gcloud projects describe $Project --format="value(projectNumber)"
$sa = "$ServiceAccount@$Project.iam.gserviceaccount.com"

Write-Host "1/5 Enabling APIs ..."
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com `
    run.googleapis.com artifactregistry.googleapis.com cloudresourcemanager.googleapis.com | Out-Null

Write-Host "2/5 Workload Identity pool + GitLab OIDC provider ..."
if (-not (Test-Gcloud @("iam", "workload-identity-pools", "describe", $Pool, "--location=global"))) {
    gcloud iam workload-identity-pools create $Pool --location=global --display-name="GitLab CI" | Out-Null
}
# Only jobs from THIS GitLab project, on its protected default branch, may assume the identity.
$cond = "assertion.project_path == '$GitLabProjectPath' && assertion.ref_protected == 'true'"
$mapping = "google.subject=assertion.sub,attribute.project_path=assertion.project_path,attribute.ref=assertion.ref,attribute.ref_protected=assertion.ref_protected"
if (Test-Gcloud @("iam", "workload-identity-pools", "providers", "describe", $Provider, "--workload-identity-pool=$Pool", "--location=global")) {
    gcloud iam workload-identity-pools providers update-oidc $Provider --workload-identity-pool=$Pool --location=global `
        --issuer-uri="https://$GitLabHost" --allowed-audiences="https://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$Pool/providers/$Provider" `
        --attribute-mapping=$mapping --attribute-condition=$cond | Out-Null
} else {
    gcloud iam workload-identity-pools providers create-oidc $Provider --workload-identity-pool=$Pool --location=global `
        --display-name="GitLab" --issuer-uri="https://$GitLabHost" `
        --allowed-audiences="https://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$Pool/providers/$Provider" `
        --attribute-mapping=$mapping --attribute-condition=$cond | Out-Null
}

Write-Host "3/5 Deploy service account (least privilege) ..."
if (-not (gcloud iam service-accounts list --filter="email=$sa" --format="value(email)")) {
    gcloud iam service-accounts create $ServiceAccount --display-name="GitLab CI deploy" | Out-Null
}
foreach ($role in @("roles/run.developer", "roles/artifactregistry.writer", "roles/iam.serviceAccountUser")) {
    gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$sa" --role=$role --condition=None | Out-Null
}
# The Cloud Run services run as the app's own service account (created by deploy.ps1); the deployer must be allowed to use it.
$runtimeSa = gcloud iam service-accounts list --filter="email~^sentinelgraph.*-run@" --format="value(email)" | Select-Object -First 1
if ($runtimeSa) {
    gcloud iam service-accounts add-iam-policy-binding $runtimeSa --member="serviceAccount:$sa" --role="roles/iam.serviceAccountUser" | Out-Null
}
# Let the GitLab identity impersonate the deploy service account.
gcloud iam service-accounts add-iam-policy-binding $sa `
    --member="principalSet://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$Pool/attribute.project_path/$GitLabProjectPath" `
    --role="roles/iam.workloadIdentityUser" | Out-Null

Write-Host "4/5 Artifact Registry repository for the CI images ..."
if (-not (Test-Gcloud @("artifacts", "repositories", "describe", "sentinelgraph", "--location=$Region"))) {
    gcloud artifacts repositories create sentinelgraph --repository-format=docker --location=$Region `
        --description="SentinelGraph images deployed from GitLab CI" --quiet | Out-Null
}

Write-Host "5/5 Done. Add these CI/CD variables to the GitLab project (Settings > CI/CD > Variables, flags: Protected):"
Write-Host ""
Write-Host "  GCP_PROJECT_ID      = $Project"
Write-Host "  GCP_PROJECT_NUMBER  = $number"
Write-Host "  GCP_REGION          = $Region"
Write-Host "  GCP_WIF_POOL        = $Pool"
Write-Host "  GCP_WIF_PROVIDER    = $Provider"
Write-Host "  GCP_DEPLOY_SA       = $sa"
Write-Host ""
Write-Host "Nothing above is a secret: authentication happens with GitLab's short-lived OIDC token at job time."
Write-Host "The staging services are created on the first deploy; production services come from .\deploy\deploy.ps1."
