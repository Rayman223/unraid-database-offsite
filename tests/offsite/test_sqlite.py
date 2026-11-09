"""SQLite : contrat générateur, archives compatibles et import réel sans source."""

import copy
import gzip
import importlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_offsite import AT, SQL, Fixture, core, main, services

sqlite_restore = importlib.import_module("sqlite_restore")

SQLITE_SQL = b"PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n" + SQL + b"COMMIT;\n"


class SQLiteFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.base.update(
            engine="sqlite",
            sqlite_path="/sqlite/bot/verify.sqlite3",
            folder="sqlite/job",
        )
        self.folder = self.source / self.base["folder"]
        self.folder.mkdir(parents=True)
        self.file = self.folder / self.file.name
        self.file.write_bytes(gzip.compress(SQLITE_SQL))
        self.config["sqlite_restore_image"] = "sha256:" + "b" * 64
        self.config["restore_timeout"] = 30


class SQLiteContractTests(SQLiteFixture):
    def test_sqlite_only_needs_no_mariadb_inventory_or_image(self):
        del self.config["inventory"], self.config["restore_image"]
        core.validate(self.config)
        with (
            patch.object(main, "discover") as discover,
            patch.object(main, "check_jobs", return_value={}),
        ):
            self.assertEqual(([], {}), self.operator.inventory())
        discover.assert_not_called()

    def test_invalid_engine_path_folder_and_mutable_image_are_rejected(self):
        for changes in (
            {"engine": "postgres"},
            {"sqlite_path": "relative.sqlite3"},
            {"sqlite_path": "/sqlite/../other.sqlite3"},
            {"folder": "mariadb/job"},
        ):
            config = copy.deepcopy(self.config)
            config["bases"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(core.Failure):
                core.validate(config)
        self.config["sqlite_restore_image"] = "bc-offsite:latest"
        with self.assertRaises(core.Failure):
            core.validate(self.config)

    def test_mixed_inventory_covers_mariadb_names_and_ignores_sqlite_label(self):
        maria = dict(
            self.base,
            engine="mariadb",
            id="maria",
            job="maria",
            folder="mariadb/maria",
            database="actual_db",
        )
        self.config["bases"].append(maria)
        core.validate(self.config)
        self.assertEqual(
            [], services.compare_inventory(self.config, {"mysql", "actual_db"})
        )
        self.assertEqual(
            ["Base non couverte : forgotten."],
            services.compare_inventory(
                self.config, {"mysql", "actual_db", "forgotten"}
            ),
        )
        with (
            patch.object(
                main, "discover", return_value={"mysql", "actual_db"}
            ) as discover,
            patch.object(main, "check_jobs", return_value={}),
        ):
            self.assertEqual(([], {}), self.operator.inventory())
        discover.assert_called_once()

    def test_sqlite_job_matches_path_type_schedule_and_timezone(self):
        job = {
            "name": "job",
            "type": "sqlite",
            "path": self.base["sqlite_path"],
            "schedule": self.base["schedule"],
        }
        inspected = [
            {
                "Image": self.base["completion"]["generator_image_id"],
                "Config": {"Env": ["TZ=UTC"]},
            }
        ]
        for changes in (
            {},
            {"path": "/sqlite/other.sqlite3"},
            {"type": "mariadb"},
            {"schedule": "0 4 * * *"},
        ):
            Path(self.config["jobs_file"]).write_text(
                json.dumps({"jobs": [dict(job, **changes)]})
            )
            with patch.object(
                services, "execute", return_value=json.dumps(inspected).encode()
            ):
                errors = services.check_jobs(self.config)
            self.assertEqual(bool(changes), bool(errors))
        inspected[0]["Config"]["Env"] = ["TZ=Europe/Brussels"]
        with patch.object(
            services, "execute", return_value=json.dumps(inspected).encode()
        ):
            self.assertIn("base", services.check_jobs(self.config))

    def test_truncated_or_rollback_sqlite_dump_is_never_uploaded(self):
        for sql in (SQL, SQLITE_SQL.replace(b"COMMIT;", b"ROLLBACK;")):
            self.file.write_bytes(gzip.compress(sql))
            self.assertTrue(self.collect())
        self.assertEqual(0, self.repo.created)

    def test_manifest_and_artifact_cannot_be_used_for_another_engine(self):
        self.assertEqual([], self.collect())
        snapshot = self.repo.snapshots(self.base)[0]
        maria = dict(self.base, engine="mariadb")
        self.assertNotEqual(
            core.artifact_id(self.base, self.file.name, "a"),
            core.artifact_id(maria, self.file.name, "a"),
        )
        with tempfile.TemporaryDirectory(dir=self.state) as temp:
            manifest = self.repo.verify(self.base, snapshot, temp)
            self.assertEqual("sqlite", manifest["engine"])
            with self.assertRaises(core.Failure):
                self.repo.verify(maria, snapshot, temp)

    def test_state_rejects_changed_engine_or_sqlite_source(self):
        self.collect()
        for changes in (
            {"engine": "mariadb"},
            {"sqlite_path": "/sqlite/other.sqlite3"},
        ):
            config = copy.deepcopy(self.config)
            config["bases"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(core.Failure):
                main.Operator(config, self.repo, self.sandbox)

    def test_legacy_mariadb_state_and_manifests_remain_readable(self):
        self.base.pop("engine")
        self.base["folder"] = "mariadb/job"
        self.assertEqual([], self.collect())
        state = core.load(self.operator.path)
        state.pop("sources")
        core.save(self.operator.path, state)
        restarted = main.Operator(self.config, self.repo, self.sandbox)
        restarted.base_state(self.base)
        snapshot = self.repo.snapshots(self.base)[0]
        with tempfile.TemporaryDirectory(dir=self.state) as temp:
            manifest = self.repo.verify(self.base, snapshot, temp)
            self.assertNotIn("engine", manifest)
        self.base["engine"] = "sqlite"
        with self.assertRaises(core.Failure):
            main.Operator(self.config, self.repo, self.sandbox)

    def test_historical_archive_retains_original_sqlite_path_without_trusting_state_reassignment(
        self,
    ):
        self.collect()
        snapshot = self.repo.snapshots(self.base)[0]
        self.base["sqlite_path"] = "/sqlite/moved/verify.sqlite3"
        with tempfile.TemporaryDirectory(dir=self.state) as temp:
            self.assertEqual(
                "/sqlite/bot/verify.sqlite3",
                self.repo.verify(self.base, snapshot, temp)["sqlite_path"],
            )
        with self.assertRaises(core.Failure):
            main.Operator(self.config, self.repo, self.sandbox)

    def test_restore_reports_bind_engine_and_recovery_rejects_mariadb_report(self):
        self.collect()
        with patch.object(main, "now", return_value=AT), patch.object(main, "ping"):
            self.assertEqual(0, self.operator.restore())
        report = self.operator.base_state(self.base)["last_restore_success"]
        self.assertEqual("sqlite", report["engine"])
        self.assertEqual(self.config["sqlite_restore_image"], report["image"])
        self.operator.path.unlink()
        recovered = main.Operator(self.config, self.repo, self.sandbox)
        self.assertEqual(0, recovered.recover())
        self.assertEqual(
            report, recovered.base_state(self.base)["last_restore_success"]
        )
        snapshot = self.repo.snapshots(self.base, "report")[0]
        wrong = dict(report, engine="mariadb")
        self.repo.payloads[snapshot["id"]]["restore-report.json"] = json.dumps(
            wrong
        ).encode()
        recovered.path.unlink()
        recovered = main.Operator(self.config, self.repo, self.sandbox)
        self.assertEqual(1, recovered.recover())
        self.assertNotIn("last_restore_success", recovered.base_state(self.base))

    def test_sqlite_child_has_no_network_mount_socket_or_capabilities_and_is_cleaned_on_error(
        self,
    ):
        sql = self.root / "test.sql"
        sql.write_bytes(SQLITE_SQL)

        def fail_import(args, **kwargs):
            if "/opt/offsite/sqlite_restore.py" in args:
                raise core.Failure("restauration", "import failed")
            return b""

        with patch.object(services, "execute", side_effect=fail_import) as execute:
            with self.assertRaises(core.Failure):
                services.RestoreSandbox(self.config).run(self.base, sql)
        run = execute.call_args_list[0].args[0]
        self.assertEqual("none", run[run.index("--network") + 1])
        self.assertEqual("65534:65534", run[run.index("--user") + 1])
        self.assertEqual("ALL", run[run.index("--cap-drop") + 1])
        self.assertIn("--read-only", run)
        self.assertIn("no-new-privileges:true", run)
        self.assertIn(self.config["sqlite_restore_image"], run)
        self.assertNotIn("--mount", run)
        self.assertNotIn("--volume", run)
        self.assertNotIn("--cap-add", run)
        self.assertNotIn(self.config["source_root"], " ".join(run))
        self.assertEqual(["docker", "rm", "--force"], execute.call_args.args[0][:3])


class LocalSQLite:
    def __init__(self, root, config):
        self.root = root
        self.c = config

    def run(self, base, sql):
        checks = {
            "required_tables": base["required_tables"],
            "assertions": base["assertions"],
            "timeout": self.c.get("restore_timeout", 30),
        }
        with (
            tempfile.TemporaryDirectory(dir=self.root) as temp,
            Path(sql).open("rb") as handle,
        ):
            sqlite_restore.restore(checks, temp, handle)


@unittest.skipUnless(
    os.environ.get("OFFSITE_REAL_SQLITE") == "1", "SQLite réel : OFFSITE_REAL_SQLITE=1"
)
class RealSQLiteTests(SQLiteFixture):
    def setUp(self):
        super().setUp()
        self.checks = {
            "required_tables": ["sentinel"],
            "assertions": self.base["assertions"],
            "timeout": 5,
        }
        self.directory = self.root / "restoration"
        self.directory.mkdir()

    def import_sql(self, sql):
        path = self.root / "input.sql"
        path.write_bytes(sql)
        with path.open("rb") as handle:
            sqlite_restore.restore(self.checks, self.directory, handle)

    def test_native_readonly_dump_imports_multiline_text_blobs_indexes_and_data(self):
        source = self.root / "source.sqlite3"
        with sqlite3.connect(source) as db:
            db.executescript(SQL.decode())
            db.execute("CREATE TABLE payload(value BLOB, text TEXT)")
            db.execute(
                "INSERT INTO payload VALUES (?, ?)",
                (b"\x00\xff", "line1\n.shell harmless text\n'quote"),
            )
            db.execute("CREATE INDEX by_text ON payload(text)")
        dump = services.execute(["sqlite3", "-readonly", str(source), ".dump"])
        self.assertTrue(dump.endswith(b"COMMIT;\n"))
        self.import_sql(dump)
        with sqlite3.connect(self.directory / "restored.sqlite3") as restored:
            self.assertEqual(
                (b"\x00\xff", "line1\n.shell harmless text\n'quote"),
                restored.execute("SELECT * FROM payload").fetchone(),
            )

    def test_invalid_sql_aborts_import(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.import_sql(SQLITE_SQL + b"PRIVATE_INVALID_SQL;\n")

    def test_missing_tables_and_wrong_scalar_fail_checks(self):
        self.import_sql(SQLITE_SQL)
        path = self.directory / "restored.sqlite3"
        for changes in (
            {"required_tables": ["missing"]},
            {"assertions": [{"sql": "SELECT COUNT(*) FROM sentinel", "expected": "2"}]},
            {"assertions": [{"sql": "SELECT 1, 2", "expected": "1"}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sqlite_restore.check_database(
                    path, dict(self.checks, **changes), time.monotonic() + 5
                )

    def test_foreign_key_violation_is_detected(self):
        sql = (
            SQLITE_SQL
            + b"CREATE TABLE child(parent INTEGER REFERENCES sentinel(id));\nINSERT INTO child VALUES(999);\n"
        )
        with self.assertRaises(ValueError):
            self.import_sql(sql)

    def test_corrupt_database_cannot_pass_integrity_check(self):
        path = self.directory / "restored.sqlite3"
        path.write_bytes(b"corrupt")
        with self.assertRaises(sqlite3.DatabaseError):
            sqlite_restore.check_database(path, self.checks, time.monotonic() + 5)

    def test_safe_cli_rejects_shell_attach_extension_and_external_file_io(self):
        target = self.root / "must-not-exist"
        for sql in (
            f".shell touch {target}\n",
            f"ATTACH '{target}' AS other;\n",
            f"SELECT writefile('{target}', 'forbidden');\n",
            "SELECT load_extension('forbidden');\n",
        ):
            with (
                self.subTest(sql=sql),
                tempfile.TemporaryDirectory(dir=self.root) as temp,
            ):
                self.directory = Path(temp)
                with self.assertRaises(subprocess.CalledProcessError):
                    self.import_sql(sql.encode())
                self.assertFalse(target.exists())

    def test_query_timeout_is_enforced_without_any_data_output(self):
        self.checks["timeout"] = 0.01
        with self.assertRaises(subprocess.TimeoutExpired):
            self.import_sql(
                b"WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT n FROM x;\n"
            )

    def test_external_copy_is_imported_after_local_dump_is_removed(self):
        self.operator.sandbox = LocalSQLite(self.root, self.config)
        self.assertEqual([], self.collect())
        self.file.unlink()
        with patch.object(main, "ping"), patch.object(main, "now", return_value=AT):
            self.assertEqual(0, self.operator.restore())


@unittest.skipUnless(
    os.environ.get("OFFSITE_REAL_SQLITE") == "1"
    and os.environ.get("OFFSITE_REAL_TOOLS") == "1",
    "SQLite, Restic et rclone réels requis",
)
class SQLiteRepositoryTests(SQLiteFixture):
    def test_encrypted_repository_readback_import_and_report_recovery(self):
        password = Path(self.config["password_file"])
        password.write_text("sqlite-test-only-password")
        password.chmod(0o600)
        rclone = Path(self.config["rclone_config"])
        rclone.write_text("[dropbox]\ntype=local\n")
        rclone.chmod(0o600)
        self.config["repository"] = "rclone:dropbox:" + str(
            self.root / "external/mariadb-offsite-v1"
        )
        with patch.object(services, "ensure_dropbox"):
            repo = services.Repository(self.config)
        repo.run("init")
        self.config["repository_id"] = repo.identity()
        self.operator = main.Operator(
            self.config, repo, LocalSQLite(self.root, self.config)
        )
        self.assertEqual([], self.collect())
        self.file.unlink()
        with patch.object(main, "ping"), patch.object(main, "now", return_value=AT):
            self.assertEqual(0, self.operator.restore())
        self.operator.path.unlink()
        recovered = main.Operator(self.config, repo, self.operator.sandbox)
        self.assertEqual(0, recovered.recover())
        self.assertEqual(
            "sqlite", recovered.base_state(self.base)["last_restore_success"]["engine"]
        )


@unittest.skipUnless(
    os.environ.get("OFFSITE_REAL_DOCKER") == "1",
    "Conteneur SQLite : OFFSITE_REAL_DOCKER=1 et OFFSITE_SQLITE_IMAGE requis",
)
class SQLiteDockerTests(SQLiteFixture):
    def setUp(self):
        super().setUp()
        self.config["sqlite_restore_image"] = os.environ["OFFSITE_SQLITE_IMAGE"]

    def test_actual_image_import_and_failure_cleanup(self):
        sandbox = services.RestoreSandbox(self.config)
        sql = self.root / "dump.sql"
        sql.write_bytes(SQLITE_SQL)
        sandbox.run(self.base, sql)
        self.base["assertions"][0]["expected"] = "2"
        with self.assertRaises(core.Failure):
            sandbox.run(self.base, sql)
        output = services.execute(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=bc.offsite=restore",
                "--filter",
                "label=bc.offsite.repository=" + self.config["repository_id"],
                "--format",
                "{{.Names}}",
            ]
        )
        self.assertEqual(b"", output)
