#!/usr/bin/env bash
# Description: Apply external retention after successful backup and restore checks, preserving protected snapshots.
# Custom schedule: 0 5 * * 1 (only if the Unraid host timezone is UTC)
set -euo pipefail
umask 077
profile_file='/mnt/user/REPLACE_PRIVATE_SHARE/offsite/production/config/profile.env'
test -f "$profile_file"
# shellcheck source=/dev/null
source "$profile_file"
: "${OFFSITE_ROOT:?Set OFFSITE_ROOT in the private profile}"
if [[ ! "${OFFSITE_REPOSITORY_ID:-}" =~ ^[a-f0-9]{64}$ ]]; then
    echo 'Set the exact repository ID in the private profile before enabling retention.' >&2
    exit 1
fi
exec flock -w 3600 "$OFFSITE_ROOT/state/schedule.lock" \
    bash "$OFFSITE_ROOT/run.sh" retention --apply --confirm "$OFFSITE_REPOSITORY_ID"
