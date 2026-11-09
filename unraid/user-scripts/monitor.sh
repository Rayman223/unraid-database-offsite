#!/usr/bin/env bash
# Description: Check backup and restore freshness, report failures and send the monitoring heartbeat.
# Custom schedule: */15 * * * *
set -euo pipefail
umask 077
profile_file='/mnt/user/REPLACE_PRIVATE_SHARE/offsite/production/config/profile.env'
test -f "$profile_file"
# shellcheck source=/dev/null
source "$profile_file"
: "${OFFSITE_ROOT:?Set OFFSITE_ROOT in the private profile}"
# Keep monitoring independent from copy/restore/retention scheduling locks.
exec bash "$OFFSITE_ROOT/run.sh" monitor
