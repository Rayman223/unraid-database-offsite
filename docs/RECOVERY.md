# Disaster recovery

The public repository contains the software and generic examples. It does not
contain your database dumps, personal configuration, credentials or encryption
password. Recovery requires access to the external repository and the private
material you retained outside the failed server.

## Recovery kit to keep outside the server

Keep these in a recoverable password manager or another private offline system:

- Dropbox account access, recovery codes and rclone OAuth configuration.
- The Restic repository's exact URI, 64-character ID and password.
- Any GPG passphrase needed by current or older archives.
- The profile JSON and environment settings, generator YAML and documented
  source paths, schedules, timezones and restore assertions.
- The source commit, Docker image reference/ID, compatible MariaDB branch and
  known-good generator version/publication-contract evidence.
- Access to recreate Discord and Healthchecks notifications.
- Separate administrative instructions for creating MariaDB accounts, deploying
  applications, and restoring non-database data and application secrets.

Do not store the only copy of the Restic password inside the repository it
encrypts. The controller repository is not a backup of the entire Unraid host.

Optionally export preloaded controller and restore images to trusted private
storage to speed up recovery without rebuilding:

```bash
docker save --output /REPLACE_PRIVATE_BACKUP_PATH/controller-image.tar "$OFFSITE_IMAGE"
```

This exports the image, not runtime bind mounts. Preserve the matching CPU
architecture and the image reference/ID. MariaDB images can be exported
separately. A source rebuild still requires upstream images and package
registries; an image archive reduces that dependency.

## Restore checks on a replacement Linux computer

Use a trusted Linux Docker host with sufficient RAM and disk. These commands
do not connect to the original databases and do not need local dump folders or
the generator YAML. They read the existing remote repository; do not run `init`.

1. Clone the public repository and check out the recorded source commit.
   Build the controller with `docker build`, or load your preserved image with
   `docker load --input /PRIVATE_PATH/controller-image.tar`.
2. Create a new private profile with `config/` and `state/` in mode `700`.
   Restore the private files into `config/` in mode `600`; install `run.sh`.
3. Adapt all absolute file/state paths to the replacement host. Keep the exact
   existing repository URI and ID. Set an existing Docker network such as
   `bridge`. Set source/job environment paths even though their files are not
   required by the recovery commands.
4. Preload the recorded compatible MariaDB image if applicable. For SQLite,
   update `sqlite_restore_image` to the actual local controller image ID if it
   was rebuilt. Keep the recorded engine/database IDs so existing manifests can
   be authenticated against the expected sources.
5. Load the private profile and recover into the new empty state directory:

   ```bash
   source /REPLACE_PRIVATE_PROFILE_PATH/config/profile.env
   bash "$OFFSITE_ROOT/run.sh" recover-state
   bash "$OFFSITE_ROOT/run.sh" restore-check
   ```

`recover-state` re-reads copies and external restore reports. It never invents a
successful SQL import. Corrupt/absent copies remain reported; recovery continues
for other archives where possible. An actual `restore-check` performs new
isolated imports and checks the repository data.

Old state that is not bound to the repository must not be silently trusted.
Preserve it for investigation and use a new empty state directory. Keep its
missed-slot acknowledgements/audit history separate; remote recovery cannot
reconstruct copies or events that were never uploaded.

To test a particular historical snapshot, inspect private recovered state to
select its full ID, then run:

```bash
bash "$OFFSITE_ROOT/run.sh" restore --base REPLACE_BASE_ID \
  --snapshot REPLACE_FULL_SNAPSHOT_ID --confirm isolated
```

The command always destroys the disposable database when finished. An older
archive's successful manual test does not clear a failed weekly check of the
latest copy. Existing source schedules or encoding changes do not invalidate
the metadata recorded in historical archives; retain historical GPG keys.

## Return an application to service

Isolated restore checks prove the dump is recoverable. They intentionally do not
write to a running production/qualification database or leave an application
database behind. Choose the recovery point, provision an isolated replacement
application database and follow that application's restoration procedure.

For an administrative export of the verified archived dump, install trusted
Restic/rclone clients and use the private credentials locally:

```bash
umask 077
export RESTIC_REPOSITORY='REPLACE_EXISTING_REPOSITORY_URI'
export RESTIC_PASSWORD_FILE='/REPLACE_PRIVATE_PROFILE/config/restic-password'
export RCLONE_CONFIG='/REPLACE_PRIVATE_PROFILE/config/rclone.conf'
restic snapshots --json
restic dump REPLACE_FULL_SNAPSHOT_ID \
  '/REPLACE_SNAPSHOT_ROOT_PATH/manifest.json' > /REPLACE_PRIVATE_EXPORT/manifest.json
restic dump REPLACE_FULL_SNAPSHOT_ID \
  '/REPLACE_SNAPSHOT_ROOT_PATH/dump' > /REPLACE_PRIVATE_EXPORT/dump
```

Find `REPLACE_SNAPSHOT_ROOT_PATH` in that snapshot's `paths` entry; never guess
it from a source filename. Before importing, compare the downloaded dump's
SHA-256 and size with the authenticated manifest and require the correct base,
engine and recovery-point timestamp. The `dump` file has no extension; its
manifest says whether it is gzip or GPG-encrypted gzip. Check/decrypt it with the
retained keys and `gzip -t` before application-specific import.

Keep the export private and perform the actual import only into the chosen
recovery target. Recreate administrative accounts and application configuration
separately. For SQLite, deploy a newly imported SQLite database using the
application's required ownership and path; do not overwrite a live database
while it has open WAL/SHM files.

## Resume scheduled protection

Restore or recreate the generator jobs, source mounts and private environment
settings. Verify its actual image ID and completion contract again. Adjust
paths without reassigning an existing base ID to a different database/engine/
SQLite source. If the replacement uses a different generator SQLite path, give
that source a new ID and keep the previous source's archive inventory available
for recovery; removing an archived base from the inventory blocks retention.

Re-enable copy and monitoring first. Require a fresh remote readback and a
successful isolated restore for every base before enabling retention. Test
Discord notifications and observe automatic Healthchecks pings. Use the
retention dry run to review candidates; source deletion must never be treated as
an instruction to delete external backups.
