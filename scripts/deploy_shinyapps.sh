#!/usr/bin/env bash
# scripts/deploy_shinyapps.sh — Deploy transcomonitor to shinyapps.io
#
# Prerequisites :
#   - rsconnect-python installed : pip install rsconnect-python
#   - Account configured :
#       rsconnect add --account YOUR_ACCOUNT --name YOUR_ACCOUNT \
#         --token YOUR_TOKEN --secret YOUR_SECRET
#   - All required env vars set in the shinyapps.io app settings
#     (see .env.example for the full list) :
#       - WHO_CLIENT_ID / WHO_CLIENT_SECRET
#       - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / S3_BUCKET / S3_REGION
#       - DEFAULT_ADMIN_PASS / DB_ENCRYPTION_KEY
#
# Usage :
#   ./scripts/deploy_shinyapps.sh [--new]
#       --new : create the app on shinyapps.io (first deployment)
#       otherwise : update an existing deployment
#
# §14 #3 : max-instances=1 forced to keep SQLite+S3 persistence safe
# §14 #4 : data/seed/ XLSX bundled in the repo
set -euo pipefail

cd "$(dirname "$0")/.."

APP_NAME="${TRANSCOMONITOR_APP_NAME:-transcomonitor}"
ACCOUNT="${TRANSCOMONITOR_RSCONNECT_ACCOUNT:-fabatih}"

if [[ "${1:-}" == "--new" ]]; then
    echo "Creating new deployment '$APP_NAME' under account '$ACCOUNT'..."
    rsconnect deploy shiny . \
        --name "$ACCOUNT" \
        --title "$APP_NAME" \
        --new \
        --exclude '__pycache__/**' \
        --exclude '.git/**' \
        --exclude '.pytest_cache/**' \
        --exclude 'tests/**' \
        --exclude 'scripts/**' \
        --exclude 'docs/**' \
        --exclude '*.md' \
        --exclude '.env' \
        --exclude 'transcomonitor.sqlite*'
else
    echo "Updating existing deployment of '$APP_NAME'..."
    rsconnect deploy shiny . \
        --name "$ACCOUNT" \
        --app-id "$APP_NAME" \
        --exclude '__pycache__/**' \
        --exclude '.git/**' \
        --exclude '.pytest_cache/**' \
        --exclude 'tests/**' \
        --exclude 'scripts/**' \
        --exclude 'docs/**' \
        --exclude '*.md' \
        --exclude '.env' \
        --exclude 'transcomonitor.sqlite*'
fi

echo ""
echo "Done."
echo ""
echo "⚠️  IMPORTANT next steps (set via shinyapps.io dashboard, not CLI) :"
echo "   1. App settings → General → Max worker processes : 1"
echo "      (CRITICAL: forces max-instances=1 for SQLite+S3 safety)"
echo "   2. App settings → Variables : set all env vars from .env.example"
echo "   3. App settings → Advanced → Instance idle timeout : 15 min"
echo "      (longer = more responsive, but uses more active hours)"
