#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# REFERENCE TEMPLATE — copied verbatim from marin-community/evalchemy.
# Adapt project/service-account/repo values before running against a new repo.
# ---------------------------------------------------------------------------
# Provision keyless GitHub Actions -> GCP auth (Workload Identity Federation) for the
# e2e workflows, so they impersonate the github-iris service account with no downloaded
# JSON key. Idempotent: every step is create-if-absent, so it is safe to re-run and it
# doubles as the source-of-truth for what was configured by hand.
#
# Run once by a hai-gcp-models admin (needs rights to manage workload identity pools and
# to set IAM on the service account). The workflows then reference the provider by the
# resource name this prints at the end.
#
# Sets up:
#   A. a Workload Identity Pool + GitHub OIDC provider, restricted to the
#      ${GITHUB_ORG} org via an attribute condition; and
#   B. a workloadIdentityUser binding that lets ONLY the ${REPO} repository impersonate
#      ${SERVICE_ACCOUNT} (scoped by the attribute.repository principalSet).
#
# NOT handled here (controller-side, resource-scoped): github-iris also needs
# roles/iap.httpsResourceAccessor on the Iris controller's backend service plus the
# controller's job-submit ACL entry -- the same access the controller owner grants a
# human. Grant that separately.
set -euo pipefail

PROJECT="${PROJECT:-hai-gcp-models}"
POOL="${POOL:-github-pool}"
PROVIDER="${PROVIDER:-github-oidc}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-github-iris@${PROJECT}.iam.gserviceaccount.com}"
GITHUB_ORG="${GITHUB_ORG:-marin-community}"
REPO="${REPO:-marin-community/evalchemy}"
ISSUER="https://token.actions.githubusercontent.com"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
echo "project=$PROJECT ($PROJECT_NUMBER)  pool=$POOL  provider=$PROVIDER"
echo "service_account=$SERVICE_ACCOUNT  repo=$REPO"

# --- A1. the pool -------------------------------------------------------------
if gcloud iam workload-identity-pools describe "$POOL" \
     --project="$PROJECT" --location=global >/dev/null 2>&1; then
  echo "pool '$POOL' already exists"
else
  gcloud iam workload-identity-pools create "$POOL" \
    --project="$PROJECT" --location=global --display-name="GitHub Actions"
fi

# --- A2. the GitHub OIDC provider ---------------------------------------------
# The attribute mapping exposes repository / repository_owner so B can scope to one
# repo. The attribute condition is the coarse gate: without it, a token from ANY GitHub
# repo could exchange here.
if gcloud iam workload-identity-pools providers describe "$PROVIDER" \
     --project="$PROJECT" --location=global --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  echo "provider '$PROVIDER' already exists"
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --project="$PROJECT" --location=global --workload-identity-pool="$POOL" \
    --display-name="GitHub OIDC" \
    --issuer-uri="$ISSUER" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner == '${GITHUB_ORG}'"
fi

# --- B. let only ${REPO} impersonate the SA (keyless) -------------------------
# add-iam-policy-binding is idempotent. The principalSet's attribute.repository segment
# is the real per-repo restriction; other repos in the org cannot match it.
MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
gcloud iam service-accounts add-iam-policy-binding "$SERVICE_ACCOUNT" \
  --project="$PROJECT" \
  --role=roles/iam.workloadIdentityUser \
  --member="$MEMBER"

PROVIDER_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
cat <<EOF

Done. Reference these in google-github-actions/auth (see .github/workflows/e2e-*.yaml):
  workload_identity_provider: ${PROVIDER_RESOURCE}
  service_account:            ${SERVICE_ACCOUNT}
EOF
