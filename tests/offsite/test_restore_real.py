"""Imports réels sur datadir neuf ou Docker jetable, jamais sur DB_* / TEST_DB_*."""

import os
import pwd
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_offsite import AT, SQL, Fixture, core, main, services


class LocalMariaDB:
    def __init__(self, root, config):
        self.root = root
        self.c = config
        self.process = None

    def start(self):
        self.root.mkdir(mode=0o700)
        data = self.root / "data"
        data.mkdir(mode=0o700)
        restricted = self.root / "restricted"
        restricted.mkdir(mode=0o500)
        services.execute(
            [
                "mariadb-install-db",
                "--no-defaults",
                "--datadir=" + str(data),
                "--auth-root-authentication-method=normal",
                "--skip-test-db",
            ],
            category="test",
            timeout=90,
        )
        self.process = subprocess.Popen(
            [
                shutil.which("mariadbd"),
                "--no-defaults",
                "--datadir=" + str(data),
                "--user=" + pwd.getpwuid(os.geteuid()).pw_name,
                "--socket=" + str(self.root / "db.sock"),
                "--pid-file=" + str(self.root / "db.pid"),
                "--skip-networking",
                "--event-scheduler=DISABLED",
                "--local-infile=0",
                "--secure-file-priv=" + str(restricted),
                "--innodb-buffer-pool-size=32M",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.client = [
            "mariadb",
            "--no-defaults",
            "--socket=" + str(self.root / "db.sock"),
            "--protocol=socket",
            "-uroot",
            "--binary-mode",
            "--batch",
            "--skip-column-names",
        ]
        end = time.monotonic() + 30
        while True:
            try:
                services.execute(self.client + ["--execute", "SELECT 1"], timeout=2)
                break
            except core.Failure:
                if time.monotonic() > end or self.process.poll() is not None:
                    raise RuntimeError("MariaDB de test isolée non prête.") from None
                time.sleep(0.1)
        return self

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def run(self, base, sql):
        # Aucun nom de base n'est transmis au serveur du projet : ce client
        # utilise exclusivement le socket du datadir initialisé ci-dessus.
        services.execute(
            self.client + ["--execute", f"CREATE DATABASE `{base['database']}`"],
            timeout=10,
        )
        services.import_and_check(self.c, base, sql, self.client + [base["database"]])


@unittest.skipUnless(
    os.environ.get("OFFSITE_REAL_MARIADB") == "1",
    "MariaDB locale jetable : OFFSITE_REAL_MARIADB=1",
)
class RealMariaDBTests(Fixture):
    def setUp(self):
        super().setUp()
        self.db = LocalMariaDB(self.root / "isolated-mariadb", self.config)
        self.addCleanup(self.db.stop)
        self.db.start()
        self.operator.sandbox = self.db

    def restore(self):
        with patch.object(main, "ping"), patch.object(main, "now", return_value=AT):
            return self.operator.restore()

    def test_external_dump_is_actually_imported_and_data_is_checked(self):
        self.assertEqual([], self.collect())
        self.file.unlink()
        self.assertEqual(0, self.restore())
        rows = services.execute(
            self.db.client + ["example", "--execute", "SELECT COUNT(*) FROM sentinel"]
        )
        self.assertEqual(b"1\n", rows)

    @unittest.skipUnless(
        os.environ.get("OFFSITE_REAL_TOOLS") == "1", "Restic/rclone réels requis"
    )
    def test_full_encrypted_external_roundtrip_then_real_mariadb_import(self):
        password = Path(self.config["password_file"])
        password.write_text("test-only-restic-password")
        password.chmod(0o600)
        rclone = Path(self.config["rclone_config"])
        rclone.write_text("[dropbox]\ntype = local\n")
        rclone.chmod(0o600)
        self.config["repository"] = "rclone:dropbox:" + str(
            self.root / "external/MariaDB/mariadb-offsite-v1"
        )
        with patch.object(services, "ensure_dropbox"):
            repo = services.Repository(self.config)
        repo.run("init")
        self.config["repository_id"] = repo.identity()
        self.operator = main.Operator(self.config, repo, self.db)
        self.assertEqual([], self.collect())
        self.file.unlink()
        self.assertEqual(0, self.restore())
        self.assertEqual(
            b"1\n",
            services.execute(
                self.db.client
                + ["example", "--execute", "SELECT COUNT(*) FROM sentinel"]
            ),
        )
        self.assertEqual(1, len(repo.snapshots(self.base, "report")))

    @unittest.skipUnless(shutil.which("mariadb-dump"), "mariadb-dump requis")
    def test_standard_mariadb_dump_header_and_schema_roundtrip(self):
        import gzip

        services.execute(self.db.client + ["--execute", "CREATE DATABASE example"])
        (self.root / "seed.sql").write_bytes(SQL)
        with (self.root / "seed.sql").open("rb") as seed:
            services.execute(self.db.client + ["example"], stdin=seed)
        result = subprocess.run(
            [
                "mariadb-dump",
                "--no-defaults",
                "--socket=" + str(self.db.root / "db.sock"),
                "--protocol=socket",
                "-uroot",
                "--routines",
                "--triggers",
                "example",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            0, result.returncode, result.stderr.decode()
        )  # Données de fixture uniquement.
        dump = result.stdout
        services.execute(self.db.client + ["--execute", "DROP DATABASE example"])
        self.file.write_bytes(gzip.compress(dump))
        self.assertEqual([], self.collect())
        self.assertEqual(0, self.restore())

    def test_correct_hash_but_invalid_sql_fails_real_import(self):
        import gzip

        self.file.write_bytes(gzip.compress(SQL + b"THIS IS INVALID SQL;\n"))
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.restore())
        self.assertNotIn("last_restore_success", self.operator.base_state(self.base))

    def test_missing_data_is_detected_after_successful_import(self):
        self.collect()
        self.base["assertions"][0]["expected"] = "2"
        self.assertEqual(1, self.restore())

    def test_sql_events_cannot_be_enabled(self):
        import gzip

        self.file.write_bytes(gzip.compress(SQL + b"SET GLOBAL event_scheduler=ON;\n"))
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.restore())
        state = services.execute(
            self.db.client
            + ["--execute", "SELECT @@event_scheduler, @@skip_networking"]
        )
        self.assertEqual(b"DISABLED\t1\n", state)


@unittest.skipUnless(
    os.environ.get("OFFSITE_REAL_DOCKER") == "1",
    "Conteneur MariaDB jetable : OFFSITE_REAL_DOCKER=1",
)
class RealDockerTests(Fixture):
    def test_container_import_and_guards(self):
        sql = self.root / "dump.sql"
        sql.write_bytes(SQL)
        services.RestoreSandbox(self.config).run(self.base, sql)

    def test_container_sql_error_is_reported_and_container_is_removed(self):
        sql = self.root / "bad.sql"
        sql.write_bytes(SQL + b"INVALID SQL;\n")
        with self.assertRaises(core.Failure):
            services.RestoreSandbox(self.config).run(self.base, sql)
