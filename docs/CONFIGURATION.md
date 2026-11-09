# Configuration reference

Start from `deploy/offsite/config.example.json`. Keep the live file outside the
source checkout as `PROFILE/config/config.json`, mode `600`. Replace every
`REPLACE_*` value. Supply an actual current/past UTC activation date and a
verified completion contract before expecting validation to succeed.

## Profile fields

| Field | Meaning |
| --- | --- |
| `source_root` | Host directory holding database-backup's `mariadb/` and `sqlite/` output folders |
| `jobs_file` | Host path to the actual YAML mounted into the running generator |
| `state_dir` | Existing private directory, mode `700`, for state and temporary files |
| `repository` | New dedicated `rclone:REMOTE:FOLDER/mariadb-offsite-v1` URI |
| `repository_id` | `SET_AFTER_INIT` only during initialization; exact returned ID afterwards |
| `rclone_config` | Private writable remote configuration file |
| `password_file` | Private Restic password file |
| `backup_container` | Generator container name; omitted means `database-backup` |
| `command_timeout` | External command timeout in seconds; default 1800 |
| `generation_grace_seconds` | Grace from a scheduled dump slot; default 7200 |
| `transfer_grace_seconds` | Transfer grace after the generation deadline; default 3600 |
| `restore_timeout` | Restore import/check timeout in seconds; default 7200 |
| `restore_memory` | Disposable database memory/tmpfs limit, such as `2g` |
| `restore_image` | Versioned MariaDB image or repository digest; required with MariaDB |
| `sqlite_restore_image` | Exact local controller image ID, `sha256:` plus 64 hex digits; required with SQLite |

The launcher mounts absolute source/job paths read-only at the same paths
inside the controller. It mounts the profile's `config/` and `state/` writable
and shares the host Docker socket only with the controller. Restore children
receive none of those mounts or credentials. The restore/restore-check/status/
recover-state/init/ack-gap commands do not require local dumps or the job YAML.

The legacy repository suffix is enforced. Do not point the controller at an
existing unrelated repository, a personal folder with other data, or a folder
managed by another synchronization/deletion tool. Each profile's state is bound
to both the URI and repository ID.

## MariaDB discovery

`inventory` contains `host`, `port`, `user`, `password_file`, `version_prefix`
and an `exclusions` object mapping exact database names to reasons.

Create a dedicated account using an administrative SQL session with client
history disabled. Replace both placeholders before running this example;
type the password in that private session, not in a shell command:

```sql
CREATE USER 'offsite_inventory'@'REPLACE_ALLOWED_DOCKER_HOST_PATTERN'
  IDENTIFIED BY 'REPLACE_WITH_GENERATED_PASSWORD';
GRANT SHOW DATABASES ON *.*
  TO 'offsite_inventory'@'REPLACE_ALLOWED_DOCKER_HOST_PATTERN';
```

This account needs discovery visibility, not table data access. The controller
checks the global `SHOW DATABASES` privilege so discovery cannot silently hide
uncovered databases. Keep system databases and deliberately excluded databases
in `exclusions` with a reason. Database names are case-sensitive configuration
identities. An unexpected new database or missing expected one raises an alert.

The MariaDB job host, port and database must match this inventory endpoint.
Use separate profiles if generator jobs intentionally use different MariaDB
endpoints. SQLite logical names are ignored by MariaDB discovery. A SQLite-only
profile may omit the entire `inventory` section.

## Per-base fields

Each item in `bases` describes exactly one expected generator job.

| Field | Meaning |
| --- | --- |
| `id` | Stable unique protection identity |
| `engine` | `mariadb` (default) or `sqlite` |
| `database` | Exact MariaDB database name, or stable logical SQLite name |
| `sqlite_path` | SQLite-only path inside the generator container |
| `job` | Exact job `name` from the generator YAML |
| `folder` | Exactly `mariadb/JOB` or `sqlite/JOB`, under `source_root` |
| `pattern` | Simple filename glob, e.g. `*.sql.gz` |
| `schedule` | The job's five-field cron expression |
| `timezone` | Exact explicit IANA `TZ` used by the generator |
| `timestamp_timezone` | Filename timezone; defaults to `timezone` |
| `enabled_since` | Actual UTC ISO activation date, never in the future |
| `timestamp_regex` | Full filename regex containing a named `stamp` capture |
| `timestamp_format` | Python strptime format matching `stamp` |
| `completion` | One of the verified publication contracts below |
| `format` | `gzip` (default) or `gpg-gzip` |
| `gpg_passphrase_file` | Private passphrase for GPG archives, including historical ones |
| `minimum_sql_bytes` | Positive realistic minimum decompressed SQL size |
| `minimum_size_ratio` | Minimum fraction of the previous accepted SQL size, default 0.5 |
| `required_tables` | Nonempty list of essential table names |
| `assertions` | Nonempty list of scalar SELECT checks with expected results |

IDs and job names must be unique. A previously recorded ID cannot be reassigned
to a different database, engine or SQLite path. Use a new ID for a new source;
do not reuse old proofs. Historical archives keep their original schedule,
encoding and source metadata, even after current jobs change.

Example timestamp settings **only if the actual generator uses this filename**:

```json
{
  "pattern": "*.sql.gz",
  "timestamp_regex": "example-app_(?P<stamp>[0-9]{8}_[0-9]{6})[.]sql[.]gz",
  "timestamp_format": "%Y%m%d_%H%M%S"
}
```

The generator may use local time even when the host uses UTC. Daylight-saving
ambiguities without an offset are rejected; use UTC or offset-bearing names
when the generator supports them. Supported job frequencies are daily or
weekly. The monitor tracks every due slot, including the last already-due slot
at installation; setting a future activation date cannot bypass freshness.

## Completion contract

A readable gzip or unchanged file size does not prove successful generation.
Choose one contract based on the exact generator image/code actually running:

- `mode: "atomic-rename"`: final filenames are published only after successful
  dump, compression/encryption, flush and close. Set `generator_image_id` to the
  generator's actual local `sha256:...` ID, and `evidence` to an audited source
  URL or meaningful verified contract reference. Any image change blocks
  collection until the contract is verified again.
- `mode: "success-marker"`: run `deploy/offsite/mark_complete.py DUMP_PATH`
  only from the generator's **successful** post-close hook. It writes an
  atomic `.complete.json` with size/hash/completion time. Do not run it from a
  periodic file sweep or failure hook; it does not independently prove dumping
  succeeded. The helper is standalone and needs only Python's standard library.

Inspect only necessary metadata, never a full environment containing secrets:

```bash
docker inspect database-backup --format '{{.Image}}'
docker inspect database-backup --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
docker exec database-backup printenv TZ
```

Pin or record the generator version and review its publication behavior. The
controller ignores temporary filenames and rejects changes to completed dumps.

## SQLite source access

Mount the application's **whole data directory** read-only into database-backup
so the SQLite database and any WAL/SHM files are visible together. The job's
absolute `path` and the base's `sqlite_path` must be the same container path:

```yaml
jobs:
  - name: example-sqlite
    type: sqlite
    path: /sqlite/example-sqlite/app.sqlite3
    schedule: "0 4 * * *"
    retention_days: 30
```

Preserve existing jobs when editing the YAML. Validate and reload the generator
using its documented procedure, then create a first dump and verify that it
appears under `sqlite/example-sqlite/`. Do not copy only a live `.sqlite3` file
or run another recurring dump task in the offsite controller.

SQLite SQL dumps must finish with `COMMIT;`; `ROLLBACK;` or a missing end marker
is rejected. Restore uses the CLI's safe mode, `integrity_check`,
`foreign_key_check`, essential tables and scalar assertions. Dumps requiring
external extensions, file access or shell commands intentionally fail.

## Data assertions

Every assertion must be a single SELECT without a semicolon. Its single scalar
result must equal `expected`, compared as text. Examples for a synthetic app:

```json
{
  "required_tables": ["users", "schema_versions"],
  "assertions": [
    {"sql": "SELECT COUNT(*) > 0 FROM schema_versions", "expected": "1"}
  ]
}
```

Choose invariants meaningful for your application: required migration history,
expected reference rows or absence of orphaned records. If a table can
legitimately be empty, do not require a positive row count for it. Inspect a
legitimate large purge before adjusting size thresholds; never lower them
automatically to silence an unexplained drop.

## Notifications and runtime state

`notifications` holds paths for `discord_url_file`, `healthcheck_url_file` and
`restore_healthcheck_url_file`. Each is a private regular file. The Discord URL
must use the official `discord.com/api/webhooks/` endpoint; Healthchecks ping
URLs must use `hc-ping.com` without query/fragment. Requests do not follow
redirects. Notification errors prevent successful heartbeats and are retried.

The private `status.json` records generated/copied/restored state and missed
slots. `monitor.json` records independent observations and notification progress.
Successful restore reports are also encrypted into the external repository.
Keep these files private: they contain installation metadata even though they
are not replacements for credentials or the Restic password.

The monitor detects a collector without a recent run after 45 minutes. A
genuinely held operation lock allows up to 24 hours for long operations;
orphaned JSON metadata cannot fake an active process. Backup deadlines still
remain monitored during those operations.
