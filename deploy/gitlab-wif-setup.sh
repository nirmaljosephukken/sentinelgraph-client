#!/usr/bin/env bash
# One-time Google Cloud setup so GitLab CI/CD can deploy SentinelGraph to Cloud Run WITHOUT any service-account key.
# GitLab's job ID token (OIDC) is exchanged for a deploy service account through Workload Identity Federation.
# Bash twin of gitlab-wif-setup.ps1 - handy from Cloud Shell:
#
#   bash deploy/gitlab-wif-setup.sh <gcp-project-id> nekku-solo/sentinelgraph-client [region]
#
# Re-running is safe. It ends by printing the six CI/CD variables to add to the GitLab project (none is a secret).
set -euo pipefail
PROJECT="${1:?project id}"; GL_PATH="${2:?gitlab project path}"; REGION="${3:-asia-south1}"
POOL="gitlab-pool"; PROVIDER="gitlab"; SA_NAME="gitlab-deploy"; GL_HOST="gitlab.com"

gcloud config set project "$PROJECT" >/dev/null
NUMBER=$(gcloud projects describe "$PROJECT" --format="value(projectNumber)")
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
AUD="https://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
COND="assertion.project_path == '$GL_PATH' && assertion.ref_protected == 'true'"
MAP="google.subject=assertion.sub,attribute.project_path=assertion.project_path,attribute.ref=assertion.ref,attribute.ref_protected=assertion.ref_protected"

echo "1/5 Enabling APIs ..."
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  run.googleapis.com artifactregistry.googleapis.com cloudresourcemanager.googleapis.com >/dev/null

echo "2/5 Workload Identity pool + GitLab OIDC provider ..."
gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" --location=global --display-name="GitLab CI" >/dev/null
# Only jobs from THIS GitLab project, on its protected default branch, may assume the identity.
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --workload-identity-pool="$POOL" --location=global \
    --issuer-uri="https://$GL_HOST" --allowed-audiences="$AUD" --attribute-mapping="$MAP" --attribute-condition="$COND" >/dev/null
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --workload-identity-pool="$POOL" --location=global \
    --display-name="GitLab" --issuer-uri="https://$GL_HOST" --allowed-audiences="$AUD" \
    --attribute-mapping="$MAP" --attribute-condition="$COND" >/dev/null
fi

echo "3/5 Deploy service account (least privilege) ..."
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name="GitLab CI deploy" >/dev/null
for ROLE in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$ROLE" --condition=None >/dev/null
done
# The Cloud Run services run as the app's own service account (created by deploy.ps1); the deployer must be allowed to use it.
RUNTIME_SA=$(gcloud iam service-accounts list --filter="email~^sentinelgraph.*-run@" --format="value(email)" | head -n1 || true)
if [ -n "$RUNTIME_SA" ]; then
  gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --member="serviceAccount:$SA" --role="roles/iam.serviceAccountUser" >/dev/null
fi
# Let the GitLab identity impersonate the deploy service account.
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --member="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.project_path/$GL_PATH" \
  --role="roles/iam.workloadIdentityUser" >/dev/null

echo "4/5 Artifact Registry repository for the CI images ..."
gcloud artifacts repositories describe sentinelgraph --location="$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create sentinelgraph --repository-format=docker --location="$REGION" \
    --description="SentinelGraph images deployed from GitLab CI" --quiet >/dev/null

echo "5/5 Done. Add these CI/CD variables to the GitLab project (Settings > CI/CD > Variables, flag: Protected):"
echo
echo "  GCP_PROJECT_ID      = $PROJECT"
echo "  GCP_PROJECT_NUMBER  = $NUMBER"
echo "  GCP_REGION          = $REGION"
echo "  GCP_WIF_POOL        = $POOL"
echo "  GCP_WIF_PROVIDER    = $PROVIDER"
echo "  GCP_DEPLOY_SA       = $SA"
echo
echo "Nothing above is a secret: authentication happens with GitLab's short-lived OIDC token at job time."
