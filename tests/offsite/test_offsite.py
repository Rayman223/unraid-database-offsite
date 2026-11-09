"""Recette déterministe : services simulés, aucun accès aux bases PRD/QUA."""

import copy
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/offsite"))
import core  # noqa: E402
import main  # noqa: E402
import mark_complete  # noqa: E402
import services  # noqa: E402

AT = core.date("2026-11-06T08:00:00+00:00")
SQL = b"CREATE TABLE sentinel (id INT PRIMARY KEY);\nINSERT INTO sentinel VALUES (1);\n"


class FakeRepository(services.Repository):
    def __init__(self):
        self.data = {}
        self.payloads = {}
        self.created = 0
        self.calls = []
        self.fail_transfer = False

    def check_identity(self):
        return None

    def snapshots(self, base=None, kind="dump"):
        return [
            copy.deepcopy(item)
            for item in self.data.values()
            if base is None
            or (f"base={base['id']}" in item["tags"] and f"kind={kind}" in item["tags"])
        ]

    def create(self, base, folder, generated, artifact, kind="dump"):
        if self.fail_transfer:
            raise core.Failure("transfert", "Destination inaccessible.")
        self.created += 1
        identity = hashlib.sha256(str(self.created).encode()).hexdigest()
        snapshot = {
            "id": identity,
            "time": core.iso(generated),
            "paths": [str(folder)],
            "tags": [
                core.TAG,
                f"base={base['id']}",
                f"kind={kind}",
                f"artifact={artifact}",
            ],
        }
        self.data[identity] = snapshot
        self.payloads[identity] = {
            path.name: path.read_bytes()
            for path in Path(folder).iterdir()
            if path.is_file()
        }
        return copy.deepcopy(snapshot)

    def fetch(self, snapshot, name, destination):
        Path(destination).write_bytes(self.payloads[snapshot["id"]][name])

    def run(self, *args):
        self.calls.append(args)
        if args[0] == "forget":
            del self.data[args[1]]
        return b""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.source = self.root / "source"
        self.folder = self.source / "mariadb/job"
        self.folder.mkdir(parents=True)
        self.config = {
            "state_dir": str(self.state),
            "source_root": str(self.source),
            "jobs_file": str(self.root / "jobs.yml"),
            "repository": "rclone:dropbox:MariaDB/mariadb-offsite-v1",
            "repository_id": "a" * 64,
            "password_file": str(self.root / "password"),
            "rclone_config": str(self.root / "rclone.conf"),
            "restore_image": "mariadb:11.8",
            "restore_memory": "1g",
            "command_timeout": 30,
            "inventory": {
                "host": "mariadb",
                "port": 3306,
                "user": "inventory",
                "password_file": str(self.root / "password"),
                "version_prefix": "11.8.",
                "exclusions": {"mysql": "Système"},
            },
            "bases": [
                {
                    "id": "base",
                    "database": "example",
                    "job": "job",
                    "folder": "mariadb/job",
                    "pattern": "*.sql.gz",
                    "schedule": "0 3 * * *",
                    "timezone": "UTC",
                    "enabled_since": "2026-11-06T03:00:00+00:00",
                    "timestamp_regex": r"daily_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{6})\.sql\.gz",
                    "timestamp_format": "%Y-%m-%d_%H%M%S",
                    "minimum_sql_bytes": 20,
                    "completion": {
                        "mode": "atomic-rename",
                        "evidence": "Contrat testé sur image de fixture immuable.",
                        "generator_image_id": "sha256:" + "a" * 64,
                    },
                    "required_tables": ["sentinel"],
                    "assertions": [
                        {"sql": "SELECT COUNT(*) FROM sentinel", "expected": "1"}
                    ],
                }
            ],
        }
        self.base = self.config["bases"][0]
        self.repo = FakeRepository()
        self.sandbox = unittest.mock.Mock()
        self.operator = main.Operator(self.config, self.repo, self.sandbox)
        quiet = patch.object(main.Operator, "print_status")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.file = self.folder / "daily_2026-11-06_030000.sql.gz"
        self.file.write_bytes(gzip.compress(SQL))

    def collect(self):
        return self.operator.collect_base(self.base, AT)


class ConfigAndTimeTests(Fixture):
    def test_initial_monitor_also_checks_the_latest_already_due_dump(self):
        base = dict(self.base, enabled_since="2026-11-06T16:00:00+00:00")
        state = {}
        errors = main.deadline_issues(
            self.config, base, state, core.date("2026-11-06T16:01:00+00:00")
        )
        self.assertTrue(any("2026-11-06T03:00:00" in item for item in errors))

    def test_remote_name_cannot_hide_a_local_destination(self):
        path = Path(self.config["rclone_config"])
        path.write_text("[dropbox]\ntype = local\n")
        path.chmod(0o600)
        with self.assertRaises(core.Failure):
            services.ensure_dropbox(self.config)
        path.write_text(
            "[dropbox]\ntype = alias\nremote = actual:folder\n[actual]\ntype = dropbox\ntoken = PRIVATE_TOKEN\n"
        )
        services.ensure_dropbox(self.config)

    def test_example_is_intentionally_not_ready(self):
        config = core.load(ROOT / "deploy/offsite/config.example.json")
        config["state_dir"] = str(self.state)
        with self.assertRaises(Exception):
            core.validate(config)

    def test_valid_and_dangerous_configs(self):
        core.validate(self.config)
        for mutate in (
            lambda c: c.update(repository="rclone:dropbox:existing-personal-folder"),
            lambda c: c.update(restore_image="mariadb:latest"),
            lambda c: c["bases"][0].update(assertions=[]),
            lambda c: c["bases"][0].update(folder="mariadb/other-project"),
            lambda c: c["bases"][0]["completion"].update(mode="stable-size"),
            lambda c: c["bases"].append(copy.deepcopy(c["bases"][0])),
            lambda c: c["inventory"]["exclusions"].update(example="exclue"),
        ):
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.assertRaises(core.Failure):
                core.validate(config)

    def test_every_slot_not_only_latest(self):
        state = {
            "local": {"a": {"slot": "2026-11-07T03:00:00+00:00"}},
            "remote": {"a": {"slot": "2026-11-07T03:00:00+00:00"}},
        }
        issues = main.deadline_issues(
            self.config, self.base, state, AT + dt.timedelta(days=1)
        )
        self.assertTrue(any("2026-11-06T03:00" in item for item in issues))
        self.assertFalse(any("2026-11-07T03:00" in item for item in issues))

    def test_deadlines_separate_generation_and_transfer(self):
        state = {}
        at = core.date("2026-11-06T04:59:00+00:00")
        self.assertEqual([], main.deadline_issues(self.config, self.base, state, at))
        self.assertIn(
            "generation", main.deadline_issues(self.config, self.base, state, AT)[0]
        )
        state["local"] = {"a": {"slot": "2026-11-06T03:00:00+00:00"}}
        self.assertIn(
            "transfert", main.deadline_issues(self.config, self.base, state, AT)[0]
        )
        state["remote"] = state["local"]
        self.assertEqual([], main.deadline_issues(self.config, self.base, state, AT))
        self.assertTrue(
            state["expected"]["2026-11-06T03:00:00+00:00"]["generation_missed"]
        )

    def test_dst_repeated_hour_is_two_slots_and_gap_is_not_invented(self):
        base = dict(self.base, timezone="Europe/Brussels", schedule="30 2 * * *")
        repeated = list(
            core.slots(
                base,
                core.date("2026-10-25T00:00:00+00:00"),
                core.date("2026-10-25T03:00:00+00:00"),
            )
        )
        self.assertEqual(
            ["2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00"],
            list(map(core.iso, repeated)),
        )
        missing = list(
            core.slots(
                base,
                core.date("2026-03-29T00:00:00+00:00"),
                core.date("2026-03-29T03:00:00+00:00"),
            )
        )
        self.assertEqual([], missing)
        with self.assertRaises(core.Failure):
            core.filename_date(base, "daily_2026-10-25_023000.sql.gz")

    def test_new_unknown_qua_is_detected_independent_of_files_and_jobs(self):
        issues = services.compare_inventory(
            self.config, {"mysql", "example", "new_qua"}
        )
        self.assertEqual(["Base non couverte : new_qua."], issues)

    def test_missing_job_or_generator_upgrade(self):
        Path(self.config["jobs_file"]).write_text("jobs: []")
        inspection = [
            {
                "Image": "sha256:" + "a" * 64,
                "Config": {"Env": ["TZ=UTC", "SECRET=do-not-print"]},
            }
        ]
        with patch.object(
            services, "execute", return_value=json.dumps(inspection).encode()
        ):
            self.assertIn("absent", services.check_jobs(self.config)["base"])
        inspection[0]["Image"] = "sha256:" + "b" * 64
        with patch.object(
            services, "execute", return_value=json.dumps(inspection).encode()
        ):
            self.assertIn("Image", services.check_jobs(self.config)["base"])

    def test_no_symlink_or_parent_escape(self):
        (self.source / "link").symlink_to(self.folder, target_is_directory=True)
        for name in ("../outside", "/absolute", "link/file.sql.gz"):
            with self.assertRaises(core.Failure):
                core.inside(self.source, name)

    def test_lock_is_exclusive_and_released(self):
        path = self.state / "lock"
        with core.lock(path):
            with self.assertRaises(core.Failure):
                with core.lock(path):
                    pass
        with core.lock(path):
            pass


class CollectionTests(Fixture):
    def test_roundtrip_and_idempotence(self):
        self.assertEqual([], self.collect())
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)
        state = self.operator.base_state(self.base)
        self.assertEqual(
            "2026-11-06T03:00:00+00:00", state["last_external_verified"]["generated_at"]
        )
        payload = next(iter(self.repo.payloads.values()))
        self.assertEqual({"dump", "manifest.json"}, set(payload))

    @unittest.skipUnless(shutil.which("gpg"), "GPG indisponible")
    def test_gpg_gzip_is_verified_before_upload(self):
        password = self.root / "gpg-password"
        password.write_text("test-only-gpg-password")
        password.chmod(0o600)
        home = self.root / "gnupg"
        home.mkdir(mode=0o700)
        output = self.folder / (self.file.name + ".gpg")
        self.addCleanup(
            lambda: services.execute(
                ["gpgconf", "--homedir", home, "--kill", "gpg-agent"], timeout=10
            )
        )
        services.execute(
            [
                "gpg",
                "--no-options",
                "--homedir",
                home,
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase-file",
                password,
                "--output",
                output,
                "--symmetric",
                self.file,
            ],
            timeout=30,
        )
        self.base.update(
            format="gpg-gzip",
            gpg_passphrase_file=str(password),
            pattern="*.sql.gz.gpg",
            timestamp_regex=r"daily_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{6})\.sql\.gz\.gpg",
        )
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)
        self.assertIn("last_external_verified", self.operator.base_state(self.base))

    def test_unpublished_and_part_are_not_transferred(self):
        self.base["completion"]["mode"] = "success-marker"
        (self.folder / (self.file.name + ".part")).write_bytes(b"in progress")
        self.assertEqual([], self.collect())
        self.assertEqual(0, self.repo.created)
        mark_complete.publish(self.file)
        with patch.object(main, "now", return_value=AT):
            marker = self.file.with_name(self.file.name + ".complete.json")
            data = core.load(marker)
            data["completed_at"] = "2026-11-06T03:02:00+00:00"
            core.save(marker, data)
            self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)

    def test_corruption_empty_truncated_and_valid_gzip_with_empty_sql(self):
        for raw in (b"", gzip.compress(b""), gzip.compress(SQL)[:-8], b"not-gzip"):
            self.file.write_bytes(raw)
            self.assertTrue(self.collect())
        self.assertEqual(0, self.repo.created)

    def test_source_changes_during_copy(self):
        original = shutil.copyfile

        def changed(source, destination, **kwargs):
            result = original(source, destination, **kwargs)
            Path(source).write_bytes(gzip.compress(SQL + b"-- changed\n"))
            return result

        with patch.object(main.shutil, "copyfile", side_effect=changed):
            self.assertTrue(any("modifié" in error for error in self.collect()))
        self.assertEqual(0, self.repo.created)

    def test_size_drop_is_not_silently_accepted(self):
        self.file.write_bytes(gzip.compress(SQL * 50))
        self.assertEqual([], self.collect())
        next_file = self.folder / "daily_2026-11-07_030000.sql.gz"
        next_file.write_bytes(gzip.compress(SQL))
        errors = self.operator.collect_base(self.base, AT + dt.timedelta(days=1))
        self.assertTrue(any("Chute" in item for item in errors))
        self.assertEqual(1, self.repo.created)

    def test_transfer_failure_keeps_local_and_can_resume(self):
        self.repo.fail_transfer = True
        self.assertTrue(self.collect())
        self.assertIn("last_local_completed", self.operator.base_state(self.base))
        self.assertNotIn("last_external_verified", self.operator.base_state(self.base))
        self.repo.fail_transfer = False
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)

    def test_interrupted_after_snapshot_before_verification(self):
        with patch.object(
            self.repo,
            "verify",
            side_effect=core.Failure("transfert", "Coupure de relecture."),
        ):
            self.assertTrue(self.collect())
        self.assertEqual(1, self.repo.created)
        # collected_at change entre deux exécutions n'invalide pas un dump identique.
        with patch.object(main, "now", return_value=AT + dt.timedelta(minutes=15)):
            self.assertEqual(
                [], self.operator.collect_base(self.base, AT + dt.timedelta(minutes=15))
            )
        self.assertEqual(1, self.repo.created)

    def test_external_payload_altered(self):
        original = self.repo.fetch

        def altered(snapshot, name, destination):
            original(snapshot, name, destination)
            if name == "dump":
                Path(destination).write_bytes(b"altered")

        with patch.object(self.repo, "fetch", side_effect=altered):
            self.assertTrue(any("Empreinte" in error for error in self.collect()))
        self.assertNotIn("last_external_verified", self.operator.base_state(self.base))

    def test_one_broken_base_does_not_prevent_other_copy(self):
        other = copy.deepcopy(self.base)
        other.update(id="other", database="other", job="other", folder="mariadb/other")
        self.config["bases"].append(other)
        folder = self.source / other["folder"]
        folder.mkdir()
        (folder / self.file.name).write_bytes(self.file.read_bytes())
        self.file.write_bytes(b"broken")
        with (
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(main, "now", return_value=AT),
            patch.object(main, "notify"),
            patch.object(main, "ping"),
        ):
            self.assertEqual(1, self.operator.run())
        self.assertIn("last_external_verified", self.operator.base_state(other))
        self.assertNotIn("last_external_verified", self.operator.base_state(self.base))


class RestoreAndRetentionTests(Fixture):
    def report_patches(self):
        self.addCleanup(patch.stopall)
        patch.object(main, "notify").start()
        patch.object(main, "ping").start()
        patch.object(main, "now", return_value=AT).start()

    def test_restore_uses_only_external_copy_and_saves_proof(self):
        self.collect()
        self.file.unlink()
        self.report_patches()
        self.assertEqual(0, self.operator.restore())
        self.sandbox.run.assert_called_once()
        self.assertIn("last_restore_success", self.operator.base_state(self.base))
        self.assertEqual(1, len(self.repo.snapshots(self.base, "report")))

    def test_matching_hash_does_not_make_sql_restorable(self):
        self.collect()
        self.sandbox.run.side_effect = core.Failure(
            "restauration", "Import SQL refusé."
        )
        self.report_patches()
        self.assertEqual(1, self.operator.restore())
        self.assertNotIn("last_restore_success", self.operator.base_state(self.base))
        self.assertIn("restore_error", self.operator.base_state(self.base))

    def test_restore_failure_recovers_without_masking_pipeline(self):
        self.collect()
        self.report_patches()
        self.sandbox.run.side_effect = core.Failure("restauration", "Import refusé.")
        self.operator.restore()
        self.sandbox.run.side_effect = None
        self.operator.state["pipeline_issues"] = ["other — transfert : absent"]
        self.assertEqual(1, self.operator.restore())
        self.assertNotIn("restore_error", self.operator.base_state(self.base))
        self.assertEqual(["other — transfert : absent"], self.operator.state["issues"])

    def test_recover_status_without_local_files_or_state(self):
        self.collect()
        self.report_patches()
        self.operator.restore()
        shutil.rmtree(self.source)
        self.operator.path.unlink()
        recovered = main.Operator(self.config, self.repo, self.sandbox)
        self.assertEqual(0, recovered.recover())
        state = recovered.base_state(self.base)
        self.assertIn("last_external_verified", state)
        self.assertIn("last_restore_success", state)

    def test_retention_preserves_each_day_month_and_protected_restore(self):
        snapshots = [
            {"id": str(index), "time": core.iso(AT - dt.timedelta(days=index))}
            for index in range(500)
        ]
        removed = set(core.retention_candidates(snapshots, {"400"}, AT))
        for index in range(31):
            self.assertNotIn(str(index), removed)
        self.assertNotIn("400", removed)
        self.assertIn("499", removed)
        for month in range(1, 10):
            same_month = [
                item
                for item in snapshots
                if core.date(item["time"]).strftime("%Y-%m") == f"2026-{month:02}"
            ]
            self.assertTrue(any(item["id"] not in removed for item in same_month))

    def test_retention_deletes_only_expired_proven_snapshots_and_supports_dry_run(self):
        for day in ("2024-01-01", "2024-02-01"):
            (self.folder / f"daily_{day}_030000.sql.gz").write_bytes(gzip.compress(SQL))
        self.collect()
        self.report_patches()
        self.operator.restore()
        self.operator.state.update(
            checked_at=core.iso(AT),
            last_collection_run=core.iso(AT),
            issues=[],
            notification_issues=[],
        )
        before = set(self.repo.data)
        with patch.object(self.operator, "preflight"), patch("builtins.print"):
            self.assertEqual(0, self.operator.retention())
            self.assertEqual(before, set(self.repo.data))
            self.assertEqual(0, self.operator.retention(apply=True))
        self.assertEqual(len(before) - 2, len(self.repo.data))
        protected = self.operator.base_state(self.base)["last_restore_success"][
            "snapshot"
        ]
        self.assertIn(protected, self.repo.data)

    def test_manual_old_restore_cannot_clear_current_restore_failure(self):
        self.collect()
        self.report_patches()
        self.operator.base_state(self.base)["restore_error"] = (
            "restauration : copie actuelle invalide."
        )
        identity = self.repo.snapshots(self.base)[0]["id"]
        self.operator.restore(self.base["id"], identity)
        self.assertIn("restore_error", self.operator.base_state(self.base))
        self.assertNotIn("last_restore_success", self.operator.base_state(self.base))
        self.assertIn(
            "last_manual_restore_success", self.operator.base_state(self.base)
        )

    def test_empty_source_or_failed_pipeline_cannot_delete_external_snapshots(self):
        self.collect()
        self.report_patches()
        self.operator.restore()
        self.operator.state.update(
            checked_at=core.iso(AT),
            last_collection_run=core.iso(AT),
            issues=[],
            notification_issues=[],
        )
        self.file.unlink()
        with patch.object(self.operator, "preflight"):
            with self.assertRaises(core.Failure):
                self.operator.retention(apply=True)
        self.assertFalse(
            any(call[0] in ("forget", "prune") for call in self.repo.calls)
        )
        self.assertTrue(self.repo.data)

    def test_sandbox_has_no_source_network_mount_ports_or_socket(self):
        def fake(args, **kwargs):
            if args[:3] == ["docker", "exec", "-i"] and "--execute" in args:
                if args[-1] == "SELECT VERSION()":
                    return b"11.8.5-MariaDB\n"
                if args[-1] == "SELECT @@event_scheduler, @@skip_networking":
                    return b"DISABLED\t1\n"
                return b"1\n"
            if "cat /proc/1/comm" in args:
                return b"mariadbd\n"
            return b""

        sql = self.root / "restore.sql"
        sql.write_bytes(SQL)
        with patch.object(services, "execute", side_effect=fake) as execute:
            services.RestoreSandbox(self.config).run(self.base, sql)
        run = execute.call_args_list[0].args[0]
        self.assertIn("none", run)
        self.assertIn("--event-scheduler=DISABLED", run)
        self.assertIn("--secure-file-priv=/docker-entrypoint-initdb.d", run)
        self.assertNotIn("--mount", run)
        self.assertNotIn("--publish", run)
        self.assertNotIn("--volume", run)
        self.assertNotIn("docker.sock", " ".join(run))
        self.assertNotIn(str(self.source), " ".join(run))
        self.assertEqual(
            ["docker", "rm", "--force"], execute.call_args_list[-1].args[0][:3]
        )

    def test_stale_cleanup_is_restricted_to_own_repository_and_expired_instances(self):
        name = "bc-offsite-restore-fixture"
        labels = {
            "bc.offsite": "restore",
            "bc.offsite.repository": self.config["repository_id"],
            "bc.offsite.deadline": "0",
        }
        outputs = [
            name.encode(),
            json.dumps([{"Config": {"Labels": labels}}]).encode(),
            b"",
        ]
        with patch.object(services, "execute", side_effect=outputs) as execute:
            services.RestoreSandbox(self.config).cleanup_stale()
        self.assertEqual(["docker", "rm", "--force", name], execute.call_args.args[0])
        labels["bc.offsite.repository"] = "other"
        with patch.object(
            services,
            "execute",
            side_effect=[
                name.encode(),
                json.dumps([{"Config": {"Labels": labels}}]).encode(),
            ],
        ) as execute:
            with self.assertRaises(core.Failure):
                services.RestoreSandbox(self.config).cleanup_stale()
        self.assertFalse(
            any(call.args[0][:2] == ["docker", "rm"] for call in execute.call_args_list)
        )


class MonitorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.collect()
        self.operator.state["last_collection_run"] = core.iso(AT)
        self.operator.base_state(self.base)["last_restore_success"] = {
            "checked_at": core.iso(AT)
        }
        self.operator.persist()

    def monitor(self, at=AT):
        with (
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(main, "notify"),
            patch.object(main, "ping") as ping,
            patch.object(main, "now", return_value=at),
            patch("builtins.print"),
        ):
            code = self.operator.monitor()
        return code, ping.call_args.args[-1]

    def test_watcher_can_run_while_restore_holds_operation_lock(self):
        with self.operator.operation("restore-check"):
            self.assertEqual((0, True), self.monitor())
        self.assertFalse((self.state / "operation.json").exists())

    def test_stopped_collector_is_detected_even_with_running_watcher(self):
        self.assertEqual((1, False), self.monitor(AT + dt.timedelta(minutes=46)))
        state = core.load(self.state / "monitor.json")
        self.assertTrue(any("collecteur" in item for item in state["issues"]))

    def test_status_cannot_call_stale_or_failed_monitor_healthy(self):
        self.operator.state["checked_at"] = core.iso(AT)
        with patch.object(main, "now", return_value=AT), patch("builtins.print"):
            self.assertEqual(1, self.operator.status())
            core.save(
                self.state / "monitor.json", {"checked_at": core.iso(AT), "issues": []}
            )
            self.assertEqual(0, self.operator.status())
            core.save(
                self.state / "monitor.json",
                {
                    "checked_at": core.iso(AT),
                    "issues": ["configuration : base oubliée"],
                },
            )
            self.assertEqual(1, self.operator.status())

    def test_long_restore_does_not_mask_missing_dump_or_collector_forever(self):
        with (
            patch.object(main, "now", return_value=AT),
            self.operator.operation("restore-check"),
        ):
            self.assertEqual((0, True), self.monitor(AT + dt.timedelta(hours=2)))
            code, healthy = self.monitor(AT + dt.timedelta(days=1))
        self.assertEqual((1, False), (code, healthy))
        state = core.load(self.state / "monitor.json")
        self.assertTrue(any("échéance" in item for item in state["issues"]))


class AlertTests(Fixture):
    def test_discord_deduplicates_and_announces_recovery(self):
        url = self.root / "discord"
        url.write_text("https://discord.com/api/webhooks/test/token")
        url.chmod(0o600)
        self.config["notifications"] = {"discord_url_file": str(url)}
        state = {}
        with patch.object(services, "request") as send:
            services.notify(self.config, state, ["base — corruption"])
            services.notify(self.config, state, ["base — corruption"])
            services.notify(self.config, state, [])
        self.assertEqual(2, send.call_count)
        self.assertIn("retour", send.call_args.kwargs["content"]["content"])

    def test_notification_failure_is_retried(self):
        url = self.root / "discord"
        url.write_text("https://discord.com/api/webhooks/test/token")
        url.chmod(0o600)
        self.config["notifications"] = {"discord_url_file": str(url)}
        state = {}
        with patch.object(
            services, "request", side_effect=core.Failure("notification", "échec")
        ):
            with self.assertRaises(core.Failure):
                services.notify(self.config, state, ["base — corruption"])
        self.assertNotIn("notified", state)

    def test_command_errors_do_not_disclose_stderr(self):
        with self.assertRaises(core.Failure) as raised:
            services.execute(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('TOP_SECRET'); sys.exit(9)",
                ]
            )
        self.assertNotIn("TOP_SECRET", str(raised.exception))


@unittest.skipUnless(
    os.environ.get("OFFSITE_REAL_TOOLS") == "1",
    "Restic/rclone réels : OFFSITE_REAL_TOOLS=1",
)
class RealRepositoryTests(Fixture):
    def test_real_encrypted_repository_over_rclone_roundtrip_and_restore_proof(self):
        password = Path(self.config["password_file"])
        password.write_text("test-only-restic-password")
        password.chmod(0o600)
        rclone = Path(self.config["rclone_config"])
        rclone.write_text("[dropbox]\ntype = local\n")
        rclone.chmod(0o600)
        destination = self.root / "external/MariaDB/mariadb-offsite-v1"
        self.config["repository"] = "rclone:dropbox:" + str(destination)
        with patch.object(services, "ensure_dropbox"):
            repo = services.Repository(self.config)
        repo.run("init")
        self.config["repository_id"] = repo.identity()
        self.operator = main.Operator(self.config, repo, self.sandbox)
        self.assertEqual([], self.collect())
        self.assertEqual([], self.collect())
        self.assertEqual(1, len(repo.snapshots(self.base)))
        for path in destination.rglob("*"):
            if path.is_file():
                self.assertNotIn(SQL, path.read_bytes())
        self.file.unlink()
        with patch.object(main, "notify"), patch.object(main, "ping"):
            self.assertEqual(0, self.operator.restore())
        self.assertEqual(1, len(repo.snapshots(self.base, "report")))
        repo.run("check", "--read-data")


if __name__ == "__main__":
    unittest.main()
