#!/usr/bin/env bash
# Install the pinned, checksum-verified Linux x86_64 scanner outside the repo.
set -euo pipefail
destination="${1:?Usage: install-gitleaks.sh /absolute/tool-directory}"
if [[ "$destination" != /* || "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo 'An absolute destination and Linux x86_64 are required.' >&2
    exit 1
fi
download_dir="$(mktemp -d)"
trap 'rm -rf -- "$download_dir"' EXIT
archive="$download_dir/gitleaks.tar.gz"
curl --fail --location --silent --show-error \
    'https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz' \
    --output "$archive"
printf '%s  %s\n' \
    '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb' \
    "$archive" | sha256sum --check --status
tar -xzf "$archive" -C "$download_dir" gitleaks
install -d -m 700 "$destination"
install -m 700 "$download_dir/gitleaks" "$destination/gitleaks"
"$destination/gitleaks" version
