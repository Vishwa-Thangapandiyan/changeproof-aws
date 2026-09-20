# Bedrock explain function: deploy notes

One small Lambda that turns a stored evidence bundle into prose with Amazon Bedrock.
Everything else in ChangeProof stays local. Built and unit-tested offline; **not yet
deployed or called against real AWS.**

## What it is

- `backend/aws_adapters/lambda_explain.py` + `bedrock_core.py`, zipped to `build/explain.zip` (2 KB, no dependencies; boto3 is in the Lambda runtime).
- Input `{"evidence": {...}}`. Output `{"text", "model", "ungroundedFigures", "accepted"}`.
- The model only restates the evidence. If its reply contains a number that is not in the evidence, `accepted` is `false` and the caller uses the deterministic explanation instead. The function never decides or edits a verdict.

## Deploy (in the account where Bedrock works)

1. Prove Bedrock works there first, and pick a model:

       AWS_REGION=<region> python scripts/probe_bedrock.py

2. Build and create:

       python scripts/build_explain_lambda.py
       ACCT=$(aws sts get-caller-identity --query Account --output text)
       aws iam create-role --role-name changeproof-explain --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
       aws iam attach-role-policy --role-name changeproof-explain --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
       aws iam put-role-policy --role-name changeproof-explain --policy-name bedrock-invoke --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"bedrock:InvokeModel","Resource":"*"}]}'
       sleep 15
       aws lambda create-function --function-name changeproof-explain --runtime python3.12 --handler lambda_explain.handler --zip-file fileb://build/explain.zip --role "arn:aws:iam::${ACCT}:role/changeproof-explain" --timeout 30 --memory-size 256 --environment "Variables={CHANGEPROOF_BEDROCK_MODEL=<model-id>}"

3. Call it from the engine (the caller needs `lambda:InvokeFunction` on this one function, and no Bedrock permission):

       AWS_REGION=<region> CHANGEPROOF_EXPLAIN_FUNCTION=changeproof-explain python scripts/run_live.py --explainer bedrock-lambda

## Notes

- `Resource: "*"` on `bedrock:InvokeModel` is for getting started; tighten it to the chosen model or inference profile.
- Cost is cents: one short call per run, `maxTokens` 700.
- Tear down: `aws lambda delete-function`, then delete the role's policies and the role.
