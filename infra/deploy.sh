#!/usr/bin/env bash
# Deploy the demo to Azure Container Apps behind built-in authentication (Easy Auth).
# Usage: infra/deploy.sh [infra/config.local.env]
# The app is created with INTERNAL ingress, Easy Auth is configured, then ingress goes external,
# so it is never reachable without authentication. Ends with negative tests (infra/verify.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
CONFIG="${1:-infra/config.local.env}"
[[ -f "$CONFIG" ]] || { echo "missing $CONFIG (copy infra/config.example.env)"; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG"
DATABRICKS_SCOPE_ID=739272be-e143-11e8-9f32-f2801f1b9fd1      # AzureDatabricks user_impersonation
DATABRICKS_RESOURCE=2ff814a6-3304-4ab8-85cb-cd0e6f879c1d
az() { command az "$@" --only-show-errors 2> >(grep -v -E "SyntaxWarning|HKEY_LOCAL|_models.py|^ *the time zones" >&2); }
step() { echo; echo "== $*"; }
read -ra TAGS <<< "$AZ_TAGS"

step "subscription + resource group"
az account set --subscription "$AZ_SUBSCRIPTION"
az group create -n "$AZ_RESOURCE_GROUP" -l "$AZ_LOCATION" --tags "${TAGS[@]}" -o none

step "Container Apps environment"
if ! az containerapp env show -n "$ACA_ENVIRONMENT" -g "$AZ_RESOURCE_GROUP" -o none 2>/dev/null; then
  az containerapp env create -n "$ACA_ENVIRONMENT" -g "$AZ_RESOURCE_GROUP" -l "$AZ_LOCATION" \
    --logs-destination none --tags "${TAGS[@]}" -o none
fi
ENV_DOMAIN=$(az containerapp env show -n "$ACA_ENVIRONMENT" -g "$AZ_RESOURCE_GROUP" --query properties.defaultDomain -o tsv)
FQDN="$ACA_APP_NAME.$ENV_DOMAIN"
echo "app url: https://$FQDN"

step "image (ACR cloud build)"
az acr show -n "$ACR_NAME" -o none 2>/dev/null || az acr create -n "$ACR_NAME" -g "$AZ_RESOURCE_GROUP" --sku Basic -l "$AZ_LOCATION" --tags "${TAGS[@]}" -o none
az acr build -r "$ACR_NAME" -t "genie-obo:$IMAGE_TAG" . --no-logs -o none
IMAGE="$ACR_NAME.azurecr.io/genie-obo:$IMAGE_TAG"

step "Entra app registration"
if [[ -z "${ENTRA_APP_ID:-}" ]]; then
  ENTRA_APP_ID=$(az ad app create --display-name "$ENTRA_APP_NAME" --sign-in-audience AzureADMyOrg \
    --required-resource-accesses "[{\"resourceAppId\":\"$DATABRICKS_RESOURCE\",\"resourceAccess\":[{\"id\":\"$DATABRICKS_SCOPE_ID\",\"type\":\"Scope\"}]}]" \
    --query appId -o tsv)
  az ad sp create --id "$ENTRA_APP_ID" -o none
  echo "created app $ENTRA_APP_ID — set ENTRA_APP_ID in $CONFIG"
fi
# App callback (/auth/callback) + Easy Auth callback (/.auth/login/aad/callback); keep existing URIs.
URIS=(); while IFS= read -r u; do [[ -n "$u" ]] && URIS+=("$u"); done < <(az ad app show --id "$ENTRA_APP_ID" --query "web.redirectUris[]" -o tsv)
for u in "https://$FQDN/auth/callback" "https://$FQDN/.auth/login/aad/callback"; do
  [[ " ${URIS[*]:-} " == *" $u "* ]] || URIS+=("$u")
done
az ad app update --id "$ENTRA_APP_ID" --web-redirect-uris "${URIS[@]}" --enable-id-token-issuance true
# Rotate: delete this app's previous deploy credentials, then mint one (1 live secret per Container App).
for kid in $(az ad app credential list --id "$ENTRA_APP_ID" --query "[?displayName=='$ACA_APP_NAME'].keyId" -o tsv); do
  az ad app credential delete --id "$ENTRA_APP_ID" --key-id "$kid"
done
SECRET=$(az ad app credential reset --id "$ENTRA_APP_ID" --append --display-name "$ACA_APP_NAME" --years 1 --query password -o tsv)

step "container app (internal ingress until Easy Auth is on)"
SESSION_SECRET=$(openssl rand -hex 32)
ENV_VARS=(AZURE_TENANT_ID="$AZ_TENANT_ID" AZURE_CLIENT_ID="$ENTRA_APP_ID" AZURE_CLIENT_SECRET=secretref:client-secret
  SESSION_SECRET=secretref:session-secret REDIRECT_URI="https://$FQDN/auth/callback"
  DATABRICKS_HOST="$DATABRICKS_HOST" GENIE_SPACE_ID="$GENIE_SPACE_ID" HTTPS_ONLY=true REQUIRE_EASY_AUTH=true ENABLE_DEBUG="${ENABLE_DEBUG:-false}")
if az containerapp show -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" -o none 2>/dev/null; then
  az containerapp secret set -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" \
    --secrets client-secret="$SECRET" session-secret="$SESSION_SECRET" -o none
  az containerapp update -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" --image "$IMAGE" --set-env-vars "${ENV_VARS[@]}" -o none
else
  az containerapp create -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" --environment "$ACA_ENVIRONMENT" \
    --image "$IMAGE" --registry-server "$ACR_NAME.azurecr.io" --registry-identity system \
    --target-port 8000 --ingress internal --min-replicas "$ACA_MIN_REPLICAS" --max-replicas "$ACA_MAX_REPLICAS" \
    --cpu "$ACA_CPU" --memory "$ACA_MEMORY" --secrets client-secret="$SECRET" session-secret="$SESSION_SECRET" \
    --env-vars "${ENV_VARS[@]}" --tags "${TAGS[@]}" -o none
fi

step "Easy Auth: Entra provider + require authentication"
az containerapp auth microsoft update -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" \
  --client-id "$ENTRA_APP_ID" --client-secret-name client-secret \
  --issuer "https://login.microsoftonline.com/$AZ_TENANT_ID/v2.0" --yes -o none
az containerapp auth update -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" --enabled true \
  --unauthenticated-client-action RedirectToLoginPage --redirect-provider azureactivedirectory \
  --require-https true -o none
AUTH=$(az containerapp auth show -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" --query "[platform.enabled, globalValidation.unauthenticatedClientAction]" -o tsv | tr "\n\t" "  ")
[[ "$(echo "$AUTH" | tr "[:upper:]" "[:lower:]")" == *"true"*"redirecttologinpage"* ]] || { echo "Easy Auth not enforced ($AUTH) — leaving ingress internal"; exit 1; }
echo "easy auth enforced: $AUTH"

step "Entra: assignment required + assigned users"
SP_ID=$(az ad sp show --id "$ENTRA_APP_ID" --query id -o tsv)
az ad sp update --id "$SP_ID" --set appRoleAssignmentRequired=true
read -ra ASSIGNED <<< "$ENTRA_ASSIGNED_USERS"
for upn in "${ASSIGNED[@]}"; do
  UID_=$(az ad user show --id "$upn" --query id -o tsv)
  if [[ -z $(az rest --method GET --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" --query "value[?principalId=='$UID_'].id" -o tsv) ]]; then
    az rest --method POST --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
      --body "{\"principalId\":\"$UID_\",\"resourceId\":\"$SP_ID\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}" -o none
  fi
  echo "assigned: $upn"
done

step "ingress external"
az containerapp ingress enable -n "$ACA_APP_NAME" -g "$AZ_RESOURCE_GROUP" --type external --target-port 8000 --transport auto -o none

step "verify"
infra/verify.sh "https://$FQDN"
