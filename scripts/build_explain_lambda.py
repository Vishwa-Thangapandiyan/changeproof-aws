"""Package the Bedrock explain function as build/explain.zip.

    python scripts/build_explain_lambda.py

The zip holds two files and no third-party code; boto3 is already in the Lambda
Python runtime. Handler name: lambda_explain.handler
"""

import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / "build" / "explain.zip"
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("bedrock_core.py", "lambda_explain.py"):
        z.write(root / "backend" / "aws_adapters" / name, name)
print(f"wrote {out} ({out.stat().st_size} bytes): bedrock_core.py, lambda_explain.py")
