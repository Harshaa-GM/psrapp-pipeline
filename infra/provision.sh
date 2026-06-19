#!/usr/bin/env bash
# =============================================================================
# infra/provision.sh — Provision all Azure resources required by the
#                      Artifact Ingestion Function App.
#
# Prerequisites:
#   • Azure CLI >= 2.57  (az login already done)
#   • Contributor or Owner on the target subscription
#   • Run once per environment (dev / staging / prod)
#
# Usage:
#   export AZURE_SUBSCRIPTION_ID="<sub-id>"
#   export ENVIRONMENT="dev"           # dev | staging | prod
#   bash infra/provision.sh
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
: "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID}"
: "${ENVIRONMENT:=dev}"

LOCATION="eastus"
PROJECT="axle-powerapp"
RG="rg-${PROJECT}-${ENVIRONMENT}"
SP_NAME="sp-${PROJECT}-${ENVIRONMENT}"
KV_NAME="kv-${PROJECT}-${ENVIRONMENT}"
SA_NAME="${PROJECT//-/}${ENVIRONMENT}sa"   # storage account (no hyphens, max 24 chars)
FA_NAME="${PROJECT}-ingest-${ENVIRONMENT}" # function app name
CONTAINER_NAME="powerapps-artifacts"
AI_NAME="${PROJECT}-appinsights-${ENVIRONMENT}"
PLAN_NAME="${PROJECT}-plan-${ENVIRONMENT}"

az account set --subscription "$AZURE_SUBSCRIPTION_ID"

echo "==> Creating resource group: $RG"
az group create \
  --name "$RG" \
  --location "$LOCATION" \
  --output table

# ── 1. Service Principal ──────────────────────────────────────────────────────
echo ""
echo "==> Creating Service Principal: $SP_NAME"
SP_JSON=$(az ad sp create-for-rbac \
  --name "$SP_NAME" \
  --role "Contributor" \
  --scopes "/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/$RG" \
  --output json)

SP_APP_ID=$(echo "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['appId'])")
SP_TENANT=$(echo "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant'])")
SP_SECRET=$(echo "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

echo "   SP App ID : $SP_APP_ID"
echo "   Tenant    : $SP_TENANT"
echo "   !! Save the client secret — it will not be shown again !!"
echo "   Client Secret: $SP_SECRET"

# ── 2. Storage Account ────────────────────────────────────────────────────────
echo ""
echo "==> Creating Storage Account: $SA_NAME"
az storage account create \
  --name "$SA_NAME" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku "Standard_LRS" \
  --kind "StorageV2" \
  --https-only true \
  --min-tls-version "TLS1_2" \
  --output table

# Create the blob container
az storage container create \
  --name "$CONTAINER_NAME" \
  --account-name "$SA_NAME" \
  --auth-mode login \
  --output table

SA_URL="https://${SA_NAME}.blob.core.windows.net"

# ── 3. Key Vault ──────────────────────────────────────────────────────────────
echo ""
echo "==> Creating Key Vault: $KV_NAME"
az keyvault create \
  --name "$KV_NAME" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --enable-rbac-authorization true \
  --output table

KV_URL="https://${KV_NAME}.vault.azure.net/"

# Store SP secret in Key Vault
echo "==> Storing SP client secret in Key Vault"
az keyvault secret set \
  --vault-name "$KV_NAME" \
  --name "sp-client-secret" \
  --value "$SP_SECRET" \
  --output table

echo "   !! Store your GitHub token manually: !!"
echo "   az keyvault secret set --vault-name $KV_NAME --name github-app-token --value <token>"

# ── 4. Application Insights ───────────────────────────────────────────────────
echo ""
echo "==> Creating Application Insights: $AI_NAME"
az monitor app-insights component create \
  --app "$AI_NAME" \
  --location "$LOCATION" \
  --resource-group "$RG" \
  --application-type "web" \
  --output table

AI_CONN_STR=$(az monitor app-insights component show \
  --app "$AI_NAME" \
  --resource-group "$RG" \
  --query "connectionString" -o tsv)

# ── 5. Function App (Consumption plan) ───────────────────────────────────────
echo ""
echo "==> Creating Function App: $FA_NAME"
az functionapp plan create \
  --name "$PLAN_NAME" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku "EP1" \
  --is-linux true \
  --output table

az functionapp create \
  --name "$FA_NAME" \
  --resource-group "$RG" \
  --plan "$PLAN_NAME" \
  --runtime "python" \
  --runtime-version "3.11" \
  --storage-account "$SA_NAME" \
  --functions-version "4" \
  --assign-identity "[system]" \
  --output table

FA_PRINCIPAL_ID=$(az functionapp identity show \
  --name "$FA_NAME" \
  --resource-group "$RG" \
  --query "principalId" -o tsv)

echo "   Function App Managed Identity: $FA_PRINCIPAL_ID"

# ── 6. RBAC assignments ───────────────────────────────────────────────────────
echo ""
echo "==> Assigning RBAC roles to Function App Managed Identity"

SA_ID=$(az storage account show --name "$SA_NAME" --resource-group "$RG" --query "id" -o tsv)
KV_ID=$(az keyvault show --name "$KV_NAME" --resource-group "$RG" --query "id" -o tsv)

# Blob Storage — Storage Blob Data Contributor
az role assignment create \
  --assignee "$FA_PRINCIPAL_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "$SA_ID" \
  --output table

# Key Vault — Key Vault Secrets User
az role assignment create \
  --assignee "$FA_PRINCIPAL_ID" \
  --role "Key Vault Secrets User" \
  --scope "$KV_ID" \
  --output table

# ── 7. Function App settings ──────────────────────────────────────────────────
echo ""
echo "==> Configuring Function App settings"
az functionapp config appsettings set \
  --name "$FA_NAME" \
  --resource-group "$RG" \
  --settings \
    "AZURE_TENANT_ID=${SP_TENANT}" \
    "AZURE_CLIENT_ID=${SP_APP_ID}" \
    "AZURE_CLIENT_SECRET=@Microsoft.KeyVault(VaultName=${KV_NAME};SecretName=sp-client-secret)" \
    "AZURE_KEY_VAULT_URL=${KV_URL}" \
    "AZURE_STORAGE_ACCOUNT_URL=${SA_URL}" \
    "BLOB_CONTAINER_NAME=${CONTAINER_NAME}" \
    "APPLICATIONINSIGHTS_CONNECTION_STRING=${AI_CONN_STR}" \
    "FUNCTIONS_WORKER_RUNTIME=python" \
    "GITHUB_OWNER=AxleNet" \
    "GITHUB_REPO=APCMS" \
    "CANVAS_APPS_PATH=APCMS_PSRIntegration/CanvasApps" \
  --output table

echo ""
echo "============================================================"
echo "  Provisioning complete!"
echo "  Function App : $FA_NAME"
echo "  Key Vault    : $KV_URL"
echo "  Storage      : $SA_URL"
echo "  Next step: Add github-app-token secret to Key Vault"
echo "============================================================"
