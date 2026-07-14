#!/usr/bin/env bash
set -euo pipefail

readonly VERSION="v1.8.0"
readonly SHA256="1370446bbe74d562608e8005a6ccce02d146a661fbd78674e11cc70b9618d6cf"
readonly URL="https://github.com/modelcontextprotocol/registry/releases/download/${VERSION}/mcp-publisher_linux_amd64.tar.gz"
readonly DESTINATION="${1:?usage: install-mcp-publisher.sh OUTPUT_PATH}"

archive="$(mktemp)"
extract_dir="$(mktemp -d)"
trap 'rm -f "$archive"; rm -rf "$extract_dir"' EXIT

curl --fail --location --retry 3 --output "$archive" "$URL"
echo "${SHA256}  ${archive}" | sha256sum -c
mkdir -p "$(dirname "$DESTINATION")"
tar --extract --gzip --file "$archive" --directory "$extract_dir" mcp-publisher
rm -f "$DESTINATION"
mv "$extract_dir/mcp-publisher" "$DESTINATION"
chmod +x "$DESTINATION"
