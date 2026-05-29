#!/bin/bash
# One-time setup: deploy the paddler-report Lambda + HTTP API Gateway
# Run from the repo root:  bash scripts/deploy_api.sh
set -euo pipefail

REGION="us-east-2"
FUNCTION_NAME="gorgegauge-submit-report"
ROLE_NAME="gorgegauge-lambda-role"
API_NAME="gorgegauge-reports"
BUCKET="gorgegauge.com"

echo "=== GorgeGauge: deploy paddler-report API ==="

# ── IAM role ──────────────────────────────────────────────────────────────────

echo "→ IAM role..."
TRUST='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

ROLE_ARN=$(aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document "$TRUST" \
  --query Role.Arn --output text 2>/dev/null || \
  aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)

aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true

S3_POLICY=$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/husumReports.json"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${BUCKET}/husumGauge.json"
    }
  ]
}
POLICY
)

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name S3ReportsAccess \
  --policy-document "$S3_POLICY"

echo "   role: $ROLE_ARN"
echo "   waiting 10s for role to propagate..."
sleep 10

# ── Lambda ────────────────────────────────────────────────────────────────────

echo "→ Lambda function..."
cd src
zip -q report_lambda.zip submit_report.py
cd ..

LAMBDA_ARN=$(aws lambda create-function \
  --function-name "$FUNCTION_NAME" \
  --runtime python3.12 \
  --role "$ROLE_ARN" \
  --handler submit_report.handler \
  --zip-file fileb://src/report_lambda.zip \
  --region "$REGION" \
  --environment "Variables={OUTPUT_BUCKET=${BUCKET}}" \
  --query FunctionArn --output text 2>/dev/null || true)

if [ -z "$LAMBDA_ARN" ]; then
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file fileb://src/report_lambda.zip \
    --region "$REGION" > /dev/null
  LAMBDA_ARN=$(aws lambda get-function \
    --function-name "$FUNCTION_NAME" \
    --query Configuration.FunctionArn --output text --region "$REGION")
fi

rm -f src/report_lambda.zip
echo "   lambda: $LAMBDA_ARN"

# ── HTTP API Gateway ──────────────────────────────────────────────────────────

echo "→ API Gateway..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Check if API already exists
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text 2>/dev/null || echo "None")

if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  API_ID=$(aws apigatewayv2 create-api \
    --name "$API_NAME" \
    --protocol-type HTTP \
    --cors-configuration \
      AllowOrigins="https://gorgegauge.com",AllowMethods="POST,OPTIONS",AllowHeaders="content-type" \
    --query ApiId --output text --region "$REGION")
  echo "   created API: $API_ID"
else
  echo "   existing API: $API_ID"
fi

# Lambda invoke permission
aws lambda add-permission \
  --function-name "$FUNCTION_NAME" \
  --statement-id "apigateway-${API_ID}" \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*/report" \
  --region "$REGION" 2>/dev/null || true

# Integration
INTEGRATION_ID=$(aws apigatewayv2 create-integration \
  --api-id "$API_ID" \
  --integration-type AWS_PROXY \
  --integration-uri "$LAMBDA_ARN" \
  --payload-format-version 2.0 \
  --region "$REGION" \
  --query IntegrationId --output text)

# Route
aws apigatewayv2 create-route \
  --api-id "$API_ID" \
  --route-key "POST /report" \
  --target "integrations/${INTEGRATION_ID}" \
  --region "$REGION" > /dev/null

# Stage + auto-deploy
aws apigatewayv2 create-stage \
  --api-id "$API_ID" \
  --stage-name prod \
  --auto-deploy \
  --region "$REGION" > /dev/null 2>&1 || true

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/report"

echo ""
echo "✅  Done!"
echo ""
echo "   API endpoint: $API_URL"
echo ""
echo "   Add this line to site/index.html (in the <script> config block):"
echo "   window.REPORTS_API = \"${API_URL}\";"
echo ""
