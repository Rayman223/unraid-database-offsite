#!/usr/bin/env bash
# Lanceur hôte Unraid. Aucun secret dans les arguments ou dans les logs Docker.
set -euo pipefail
umask 077
ops_root="${OFFSITE_ROOT:?Set OFFSITE_ROOT to the private profile directory}"
source_root="${OFFSITE_SOURCE:?Set OFFSITE_SOURCE to the database-backup output directory}"
jobs_file="${OFFSITE_JOBS:?Set OFFSITE_JOBS to the database-backup YAML file}"
network="${OFFSITE_NETWORK:?Set OFFSITE_NETWORK to an existing Docker network}"
image="${OFFSITE_IMAGE:?Set OFFSITE_IMAGE to the preloaded controller image}"
if [[ "$ops_root" == /mnt/user/* ]]; then
    mountpoint -q /mnt/user || { echo 'Array /mnt/user absent.' >&2; exit 1; }
fi
for path in "$ops_root" "$source_root" "$jobs_file"; do
    if [[ "$path" != /* || "$path" == *','* || "$path" == *$'\n'* ]]; then
        echo 'Chemin absolu sans virgule ni saut de ligne requis.' >&2
        exit 1
    fi
done
# Ne jamais remplacer silencieusement un volume absent par un dossier vide.
test -d "$ops_root/config"
test -d "$ops_root/state"
test -f "$ops_root/config/config.json"
mounts=()
case "${1:-}" in
    init|status|restore|restore-check|recover-state|ack-gap) ;;
    *)
        test -d "$source_root"
        test -f "$jobs_file"
        mounts+=(--mount "type=bind,source=$source_root,target=$source_root,readonly")
        mounts+=(--mount "type=bind,source=$jobs_file,target=$jobs_file,readonly")
        ;;
esac
exec docker run --rm --init --pull=never \
    --label bc.offsite=controller \
    --network "$network" --read-only --cap-drop ALL --cap-add DAC_READ_SEARCH \
    --memory "${OFFSITE_MEMORY:-1g}" --cpus 1 --pids-limit 256 \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=64m \
    --mount "type=bind,source=$ops_root/config,target=$ops_root/config" \
    --mount "type=bind,source=$ops_root/state,target=$ops_root/state" \
    "${mounts[@]}" \
    --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
    "$image" --config "$ops_root/config/config.json" "$@"
