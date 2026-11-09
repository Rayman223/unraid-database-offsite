#!/usr/bin/env bash
# Description: Encrypt completed database dumps, copy them to Dropbox and verify the copies.
# Custom schedule: */15 * * * *
set -euo pipefail
umask 077
profile_file='/mnt/user/REPLACE_PRIVATE_SHARE/offsite/production/config/profile.env'
test -f "$profile_file"
# shellcheck source=/dev/null
source "$profile_file"
: "${OFFSITE_ROOT:?Set OFFSITE_ROOT in the private profile}"
exec flock -n -E 0 "$OFFSITE_ROOT/state/schedule.lock" \
    bash "$OFFSITE_ROOT/run.sh" run
