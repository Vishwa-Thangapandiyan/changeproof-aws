#!/usr/bin/env bash
#
# Package the demo application into the zip that terraform/main.tf uploads.
#
# There are no dependencies to vendor: the handlers use boto3 only, which the Lambda
# Python runtime already provides. The package is therefore one file, and building it
# needs no network, no AWS account and no virtualenv.
#
# The timestamp is pinned so that rebuilding unchanged source produces a
# byte-identical zip. Two experiments that ran the same code should be provably
# running the same code.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_file="${repo_root}/workload/src/index.py"
build_dir="${repo_root}/terraform/build"
output="${build_dir}/function.zip"

if [ ! -f "${source_file}" ]; then
  echo "error: ${source_file} not found" >&2
  exit 1
fi

mkdir -p "${build_dir}"
rm -f "${output}"

staging="$(mktemp -d)"
trap 'rm -rf "${staging}"' EXIT
cp "${source_file}" "${staging}/index.py"
touch -t 200001010000 "${staging}/index.py"

(cd "${staging}" && zip -q -X "${output}" index.py)

echo "wrote ${output}"
echo "sha256: $(shasum -a 256 "${output}" | cut -d' ' -f1)"
