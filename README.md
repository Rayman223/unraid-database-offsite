# Unraid Database Offsite

Encrypted offsite backups and verified restores for **MariaDB and SQLite** on
Unraid, using **Restic**, **rclone** and **Dropbox**.

The controller reads completed SQL dumps produced by
[database-backup](https://github.com/Greite/database-backup). It encrypts and
uploads them, reads them back to verify their contents, and regularly restores
them in disposable containers. It does not replace the dump generator.

## How it works

```text
MariaDB / SQLite
       │  database-backup creates completed dumps
       ▼
Local dumps ──► validate + hash ──► Restic encryption ──► Dropbox via rclone
                                      │
                               download + verify
                                      │
                           isolated restore + SQL checks
                                      │
                         encrypted external restore report

Independent monitor ──► Discord alerts + Healthchecks heartbeats
```

- MariaDB discovery detects databases missing from the expected inventory.
- SQLite jobs are explicitly listed and checked against their configured path.
- Every scheduled dump has separate generation and transfer deadlines. A later
  successful dump does not silently clear an earlier missed backup.
- Restore containers have no network, host mounts, source credentials or Docker
  socket. SQLite also uses the CLI safe mode and checks integrity/foreign keys.
- Retention keeps 30 days, one copy per calendar month for the last 12 months,
  and the latest externally verified and successfully restored dumps.
- Retention stops when sources, copies, restore proofs or repository checks fail.

See [source provenance](NOTICE.md) and the [MIT license](LICENSE). The historical
`mariadb-offsite-v1` repository suffix and internal `bc` identifiers remain for
archive compatibility, including when a profile contains SQLite.

## Requirements

- A Linux Docker host; Unraid additionally needs the **User Scripts** plugin,
  Bash and `flock`.
- An existing database-backup container, its job YAML, and its completed dump
  folders. The generator must have an explicit `TZ` environment variable.
- A working rclone Dropbox remote, and a **new, dedicated Restic repository**.
- A private Discord webhook and two Healthchecks.io checks.
- For MariaDB: an inventory account with global `SHOW DATABASES`, and a
  compatible preloaded MariaDB restore image. This version checks MariaDB 11.8
  in its supplied tests; choose a branch matching your source server.
- Private storage for the configuration, local state, and temporary decompressed
  dumps. Disposable databases live in memory-backed tmpfs storage.

The controller is an administrative tool with Docker socket access. Run it only
from a trusted administrative environment. Keep its runtime directory outside
this checkout and outside web-served folders. Never upload dumps, OAuth tokens,
passwords, webhook URLs or live configuration to this repository.

## Installation

### 1. Get the source and build the image

```bash
git clone https://github.com/Rayman223/unraid-database-offsite.git
cd unraid-database-offsite
docker build -t unraid-database-offsite:local .
docker run --rm unraid-database-offsite:local --help
```

The Dockerfile pins its base images by digest and Python dependencies by
version. It also installs the Docker CLI, SQLite, GPG and timezone data.
Building requires access to the upstream container and package registries.
The source remains sufficient to rebuild the controller without a published
controller image. Record the exact source commit and image ID you deploy.

### 2. Create a private profile

Choose an existing private Unraid share and replace every `REPLACE_*` value in
the examples. Examples intentionally fail validation until configured.

```bash
umask 077
export OFFSITE_ROOT='/mnt/user/REPLACE_PRIVATE_SHARE/offsite/production'
install -d -m 700 "$OFFSITE_ROOT" "$OFFSITE_ROOT/config" "$OFFSITE_ROOT/state"
install -m 700 deploy/offsite/run.sh "$OFFSITE_ROOT/run.sh"
install -m 600 deploy/offsite/config.example.json "$OFFSITE_ROOT/config/config.json"
install -m 600 unraid/profile.env.example "$OFFSITE_ROOT/config/profile.env"
```

For an existing installation, preserve its configuration, passwords and state:
do not run the example-file installation commands over those files.

Edit the private `profile.env` file to select the runtime directory, dump
directory, job YAML, existing Docker network and preloaded controller image.
It contains environment settings, not credentials. Load it before manual runs:

```bash
source "$OFFSITE_ROOT/config/profile.env"
```

The launcher requires explicit `OFFSITE_ROOT`, `OFFSITE_SOURCE`, `OFFSITE_JOBS`,
`OFFSITE_NETWORK` and `OFFSITE_IMAGE`. The JSON source and job paths must match
those environment settings. Use a Docker network that can reach the MariaDB
inventory endpoint if the profile includes MariaDB. You can run separate
profiles, repositories and passwords for production and qualification.

### 3. Store secrets in private files

Create these regular files under the profile's `config/` directory in mode
`600`; keep the directories in mode `700`. Use an editor or hidden input,
never a command containing the secret in its arguments or shell history.

| File | Contents |
| --- | --- |
| `rclone.conf` | Private copy of the Dropbox remote and any alias/crypt dependencies |
| `restic-password` | Strong, unique password for this new Restic repository |
| `inventory-password` | MariaDB inventory password; omit for SQLite-only profiles |
| `discord-url` | Private official `https://discord.com/api/webhooks/...` URL |
| `healthcheck-url` | Monitoring check's private `https://hc-ping.com/...` URL |
| `restore-healthcheck-url` | Weekly restore check's private ping URL |

`rclone config file` and `rclone listremotes --long` help identify the existing
configuration without dumping its tokens. Copy only the required remote
sections. Its private copy must be writable so rclone can refresh OAuth tokens.
The controller verifies that the remote actually resolves to Dropbox.

For a single-line secret, the following prompts without displaying the value:

```bash
read -r -s -p 'Restic password: ' offsite_secret
printf '\n'
printf '%s\n' "$offsite_secret" > "$OFFSITE_ROOT/config/restic-password"
unset offsite_secret
chmod 600 "$OFFSITE_ROOT/config/restic-password"
```

Retain the Restic password, repository identity, Dropbox recovery access, rclone
configuration, optional GPG passphrases and sanitized installation notes in a
password manager accessible **without this server**. Losing the Restic password
makes the encrypted backups unrecoverable. See [disaster recovery](docs/RECOVERY.md).

### 4. Configure inventories, jobs and restore checks

Edit `config/config.json` using the
[configuration guide](docs/CONFIGURATION.md). Set the dedicated repository URI,
absolute private paths, expected databases and exclusions, job schedules,
timezones, actual filename format, completion contract and data checks.

For MariaDB, preload a matching restore image and record a fixed digest where
possible:

```bash
docker pull mariadb:11.8
docker image inspect mariadb:11.8 --format '{{index .RepoDigests 0}}'
```

Use that reference in `restore_image`. For SQLite, include an adapted object
from [sqlite-base.example.json](deploy/offsite/sqlite-base.example.json) in
`bases`, and set the root-level `sqlite_restore_image` to the exact local
controller image ID:

```bash
docker image inspect "$OFFSITE_IMAGE" --format '{{.Id}}'
```

SQLite restore containers reuse this image, overridden to run only the isolated
SQLite import helper. Keep the referenced image locally; restore runs never
pull an image. A SQLite-only profile can omit `inventory` and `restore_image`.

### 5. Initialize and prove the first backup

Set `repository_id` to `SET_AFTER_INIT`. Initialize only the new dedicated
destination, supplying the exact URI already recorded in your JSON:

```bash
bash "$OFFSITE_ROOT/run.sh" init --confirm 'rclone:REPLACE_REMOTE:REPLACE_FOLDER/mariadb-offsite-v1'
```

Replace `repository_id` in the private JSON with the printed 64-character ID,
and copy it to `OFFSITE_REPOSITORY_ID` in the private profile for retention.
Never reinitialize an existing repository during recovery.

Run these commands separately and inspect each result:

```bash
bash "$OFFSITE_ROOT/run.sh" preflight
bash "$OFFSITE_ROOT/run.sh" run
bash "$OFFSITE_ROOT/run.sh" restore-check
bash "$OFFSITE_ROOT/run.sh" run
bash "$OFFSITE_ROOT/run.sh" monitor
bash "$OFFSITE_ROOT/run.sh" status
bash "$OFFSITE_ROOT/run.sh" retention --dry-run
```

The first `run` remains unsuccessful until the initial restore is proven, even
if upload and readback succeeded. Before scheduling retention, require a
verified external copy and successful restore date for **every** configured
base, and no unresolved monitoring or notification failures.

The controller currently reports operational messages in French. `status`
shows `local` (latest completed dump), `externe` (verified external copy), and
`restauration` (successful restore check).

## Unraid User Scripts

Copy the four templates from [unraid/user-scripts](unraid/user-scripts) into
separate User Scripts entries. In each template, replace `profile_file` with
your actual private profile path. Paste its short `Description` comment into
the plugin's description field if useful.

| Template | Purpose | Example custom cron |
| --- | --- | --- |
| `copy.sh` | Encrypt, upload and verify completed dumps | `*/15 * * * *` |
| `monitor.sh` | Detect stale/failed protection and send alerts | `*/15 * * * *` |
| `restore-check.sh` | Check repository data and prove isolated restores | `0 5 * * 0` |
| `retention.sh` | Apply retention after successful checks | `0 5 * * 1` |

The weekly examples mean 05:00 **in the host's timezone**. Use them unchanged
only when the Unraid host uses UTC; otherwise convert them deliberately and
account for daylight-saving changes. Generator schedules independently use the
generator's explicit `TZ`, matched by each base's `timezone` in the JSON.

Copy runs skip if the scheduling lock is busy, then retry at the next interval.
Restore and retention wait up to an hour for that lock. Monitoring stays
independent and continues during long copy/restore operations. Stagger weekly
tasks if several profiles share the same host resources.

Configure two Healthchecks checks: monitoring every 15 minutes with a 30-minute
grace, and a weekly restore check matching the actual UTC schedule with a
24-hour grace. Connect and test their Discord integration: a heartbeat service
must detect host outages when the local controller cannot send any alert.
After scheduling, verify that automatic successful pings actually arrive.

## Commands

| Command | Purpose |
| --- | --- |
| `init --confirm URI` | Initialize the explicitly confirmed new repository |
| `preflight` | Check inventory, generator jobs, paths, notifications and repository identity |
| `run` | Collect completed dumps, copy and read them back, update protection state |
| `monitor` | Independently check freshness and report alerts/heartbeat |
| `status` | Print local, external and restore timestamps plus known issues |
| `restore-check` | Read-check the repository and restore the latest copy of each base |
| `restore --base ID --snapshot SHA --confirm isolated` | Test an explicitly selected historical snapshot in isolation |
| `recover-state` | Rebuild copy history and recover external restore reports into fresh local state |
| `retention --dry-run` | Preview candidates without deleting snapshots |
| `retention --apply --confirm REPOSITORY_ID` | Apply guarded retention to the confirmed repository |
| `ack-gap --base ID --slot UTC_ISO --reason 'Documented reason'` | Acknowledge an investigated, irrecoverable missed slot |

`restore` and `restore-check` always destroy their disposable database when
finished. They prove recoverability; putting an application back in service is
a separate, controlled recovery operation. Old gaps require explicit review;
acknowledgement never creates or validates a backup.

## Troubleshooting and limitations

- **Inventory/job mismatch:** inspect the expected database, server/port or
  SQLite path, schedule, `TZ`, job name and generator image ID. Do not print
  complete container environments or job YAML containing passwords.
- **No restore proof:** run the initial `restore-check`; a gzip checksum or a
  successful upload alone does not prove SQL recovery.
- **Large dump or timeout:** allow enough private state disk space and
  `restore_memory` for the decompressed database and indexes. The controller's
  own memory is configurable using `OFFSITE_MEMORY`.
- **Generator upgraded:** re-audit its atomic publication contract and update
  the recorded image ID. A stable file size is not proof of successful dumping.
- **Local filename changed in place:** the controller rejects mutation of a
  completed dump; retain the original externally verified copy for analysis.
- **Long interruption:** missed slots and outages beyond the detailed history
  window remain visible until investigated and explicitly acknowledged.
- **Notifications:** private Discord and Healthchecks URLs are required.
  Unsent notifications are retried and prevent a false successful heartbeat.
- **Formats:** gzip and GPG-encrypted gzip SQL dumps are supported. Age archives,
  raw SQLite files and arbitrary backup formats are not accepted.
- **SQLite:** it uses safe-mode SQL imports. Dumps requiring shell commands,
  extensions or external file access are rejected.
- **Scope:** Dropbox is the supported external backend. Encryption does not
  make a repository immutable against an account that can delete its objects.

## Development and validation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests/offsite -v
ruff check --isolated --select E4,E7,E9,F,I deploy/offsite tests/offsite
ruff format --isolated --check deploy/offsite tests/offsite
shellcheck deploy/offsite/run.sh unraid/user-scripts/*.sh unraid/profile.env.example scripts/*.sh
```

Real SQLite tests require `sqlite3` in `PATH` and `OFFSITE_REAL_SQLITE=1`.
Encrypted local roundtrips require Restic/rclone and `OFFSITE_REAL_TOOLS=1`.
Disposable local MariaDB tests require MariaDB server/install-db/client binaries
and `OFFSITE_REAL_MARIADB=1`. Docker tests use `OFFSITE_REAL_DOCKER=1` and, for
SQLite, an `OFFSITE_SQLITE_IMAGE` containing the built controller's local ID.
All test data is synthetic; tests do not use application database credentials.

GitHub Actions runs static checks, secret detection, a Docker build, encrypted
local roundtrips and isolated MariaDB/SQLite restore tests. Local tests do not
prove the health of a real Unraid/Dropbox installation. See
[security guidance](SECURITY.md) before reporting sensitive findings.
