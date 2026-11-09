#!/usr/bin/env bash
# Description: Restore external backups in isolated disposable containers and verify schema and data.
# Custom schedule: 0 5 * * 0 (only if the Unraid host timezone is UTC)
set -euo pipefail
umask 077
profile_file='/mnt/user/REPLACE_PRIVATE_SHARE/offsite/production/config/profile.env'
test -f "$profile_file"
# shellcheck source=/dev/null
source "$profile_file"
: "${OFFSITE_ROOT:?Set OFFSITE_ROOT in the private profile}"
exec flock -w 3600 "$OFFSITE_ROOT/state/schedule.lock" \
    bash "$OFFSITE_ROOT/run.sh" restore-check
