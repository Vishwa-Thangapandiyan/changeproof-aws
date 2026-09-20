"""Find which Bedrock models this account can actually call. Read-only, ~5 tokens each.

    AWS_PROFILE=changeproof AWS_REGION=ap-south-1 python scripts/probe_bedrock.py
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError

MODELS = [
    "apac.amazon.nova-micro-v1:0",
    "apac.amazon.nova-lite-v1:0",
    "apac.anthropic.claude-3-haiku-20240307-v1:0",
    "global.amazon.nova-2-lite-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
]

region = os.environ.get("AWS_REGION", "ap-south-1")
client = boto3.client("bedrock-runtime", region_name=region)
print(f"region {region}\n")
working = []
for model in MODELS:
    try:
        reply = client.converse(
            modelId=model,
            messages=[{"role": "user", "content": [{"text": "Say OK."}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0},
        )
        text = reply["output"]["message"]["content"][0]["text"].strip()
        print(f"  WORKS   {model}   -> {text!r}")
        working.append(model)
    except ClientError as error:
        code = error.response["Error"]["Code"]
        print(f"  denied  {model}   {code}: {error.response['Error']['Message'][:90]}")
print("\nuse:", working[0] if working else "none of these worked")
sys.exit(0 if working else 1)
