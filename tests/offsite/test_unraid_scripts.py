"""Exercise host launchers and scheduling locks without Docker or live data."""

import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class UnraidScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile_root = self.root / "profile"
        (self.profile_root / "config").mkdir(parents=True, mode=0o700)
        (self.profile_root / "state").mkdir(mode=0o700)
        (self.profile_root / "config/config.json").write_text("{}")
        self.source = self.root / "dumps"
        self.source.mkdir()
        self.jobs = self.root / "jobs.yml"
        self.jobs.write_text("jobs: []")
        self.capture = self.root / "capture.json"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['OFFSITE_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        )
        docker.chmod(0o700)
        self.env = dict(os.environ)
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            OFFSITE_CAPTURE=str(self.capture),
            OFFSITE_ROOT=str(self.profile_root),
            OFFSITE_SOURCE=str(self.source),
            OFFSITE_JOBS=str(self.jobs),
            OFFSITE_NETWORK="test-network",
            OFFSITE_IMAGE="test-controller:local",
        )
        self.profile = self.profile_root / "config/profile.env"

    def run_launcher(self, command):
        return subprocess.run(
            ["bash", str(ROOT / "deploy/offsite/run.sh"), command],
            env=self.env,
            capture_output=True,
            timeout=10,
        )

    def run_template(self, name, repository_id="a" * 64):
        profile = {
            key: value for key, value in self.env.items() if key.startswith("OFFSITE_")
        }
        profile["OFFSITE_REPOSITORY_ID"] = repository_id
        self.profile.write_text(
            "\n".join(
                f"export {key}={shlex.quote(value)}" for key, value in profile.items()
            )
            + "\n"
        )
        self.profile.chmod(0o600)
        launcher = self.profile_root / "run.sh"
        # User Scripts explicitly invokes bash, so use a Bash recording launcher.
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            "exec python3 -c 'import json,os,sys; from pathlib import Path; "
            'Path(os.environ["OFFSITE_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\' "$@"\n'
        )
        source = (ROOT / "unraid/user-scripts" / name).read_text()
        source = source.replace(
            "profile_file='/mnt/user/REPLACE_PRIVATE_SHARE/offsite/production/config/profile.env'",
            "profile_file=" + shlex.quote(str(self.profile)),
        )
        script = self.root / name
        script.write_text(source)
        return subprocess.run(
            ["bash", str(script)], env=self.env, capture_output=True, timeout=10
        )

    def test_missing_source_or_explicit_environment_cannot_start_docker(self):
        self.source.rmdir()
        self.assertNotEqual(0, self.run_launcher("run").returncode)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.capture.exists())
        del self.env["OFFSITE_IMAGE"]
        self.assertNotEqual(0, self.run_launcher("restore-check").returncode)
        self.assertFalse(self.capture.exists())

    def test_recovery_launches_without_live_source_or_jobs_but_collection_mounts_them_readonly(
        self,
    ):
        self.assertEqual(0, self.run_launcher("run").returncode)
        args = json.loads(self.capture.read_text())
        self.assertIn(
            f"type=bind,source={self.source},target={self.source},readonly", args
        )
        self.assertIn(f"type=bind,source={self.jobs},target={self.jobs},readonly", args)
        self.source.rmdir()
        self.jobs.unlink()
        self.assertEqual(0, self.run_launcher("restore-check").returncode)
        args = json.loads(self.capture.read_text())
        self.assertFalse(
            any(arg.startswith("type=bind,source=" + str(self.source)) for arg in args)
        )
        self.assertFalse(
            any(arg.startswith("type=bind,source=" + str(self.jobs)) for arg in args)
        )

    def test_busy_copy_skips_while_monitor_stays_independent(self):
        with (self.profile_root / "state/schedule.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(0, self.run_template("copy.sh").returncode)
            self.assertFalse(self.capture.exists())
            self.assertEqual(0, self.run_template("monitor.sh").returncode)
            self.assertEqual(["monitor"], json.loads(self.capture.read_text()))

    def test_retention_requires_exact_repository_confirmation(self):
        self.assertNotEqual(
            0, self.run_template("retention.sh", "REPLACE_REPOSITORY_ID").returncode
        )
        self.assertFalse(self.capture.exists())
        self.assertEqual(0, self.run_template("retention.sh").returncode)
        self.assertEqual(
            ["retention", "--apply", "--confirm", "a" * 64],
            json.loads(self.capture.read_text()),
        )
