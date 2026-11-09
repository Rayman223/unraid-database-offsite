"""Régressions de la revue #1068 : preuves, concurrence et garde de rétention."""

import copy
import datetime as dt
import gzip
import json
import tempfile
from unittest.mock import patch

from test_offsite import AT, SQL, FakeRepository, Fixture, core, main, services


class ReviewRegressions(Fixture):
    def healthy(self):
        self.assertEqual([], self.collect())
        with patch.object(main, "now", return_value=AT), patch.object(main, "ping"):
            self.assertEqual(0, self.operator.restore())
        self.operator.state.update(last_collection_run=core.iso(AT), issues=[])
        self.operator.persist()

    def test_state_cannot_reuse_proofs_for_a_different_repository(self):
        self.collect()
        config = copy.deepcopy(self.config)
        config["repository_id"] = "b" * 64
        with self.assertRaises(core.Failure):
            main.Operator(config, FakeRepository(), self.sandbox)

    def test_base_id_cannot_reassign_existing_proofs_to_another_database(self):
        self.collect()
        config = copy.deepcopy(self.config)
        config["bases"][0]["database"] = "new_qua"
        with self.assertRaises(core.Failure):
            main.Operator(config, FakeRepository(), self.sandbox)

    def test_future_activation_date_cannot_disable_freshness_checks(self):
        config = copy.deepcopy(self.config)
        config["bases"][0]["enabled_since"] = core.iso(AT + dt.timedelta(days=90))
        with patch.object(core, "now", return_value=AT):
            with self.assertRaises(core.Failure):
                core.validate(config)

    def test_finished_dump_mutation_does_not_create_another_valid_version(self):
        self.collect()
        self.file.write_bytes(gzip.compress(SQL * 2))
        self.assertTrue(self.collect())
        self.assertEqual(1, self.repo.created)

    def test_live_long_collection_does_not_look_like_a_stopped_collector(self):
        self.healthy()
        with patch.object(main, "now", return_value=AT), self.operator.operation("run"):
            with (
                patch.object(main, "now", return_value=AT + dt.timedelta(hours=2)),
                patch.object(self.operator, "inventory", return_value=([], {})),
                patch.object(main, "notify"),
                patch.object(main, "ping") as ping,
            ):
                self.assertEqual(0, self.operator.monitor())
        self.assertIs(True, ping.call_args.args[-1])

    def test_full_state_disk_cannot_emit_a_successful_heartbeat(self):
        self.healthy()
        with (
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(main, "notify") as notify,
            patch.object(main, "ping") as ping,
            patch.object(main, "save", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                self.operator.monitor()
        notify.assert_not_called()
        ping.assert_not_called()

    def test_failed_notification_cannot_emit_a_successful_heartbeat(self):
        self.healthy()
        with (
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(
                main, "notify", side_effect=core.Failure("notification", "échec")
            ),
            patch.object(main, "ping") as ping,
        ):
            self.assertEqual(1, self.operator.monitor())
        self.assertIs(False, ping.call_args.args[-1])

    def test_discord_keeps_the_last_base_visible_after_many_missed_slots(self):
        url = self.root / "discord"
        url.write_text("https://discord.com/api/webhooks/test/token")
        url.chmod(0o600)
        self.config["notifications"] = {"discord_url_file": str(url)}
        issues = [
            f"first — generation : dump absent/incomplet pour l'échéance {core.iso(AT + dt.timedelta(days=index))}."
            for index in range(50)
        ]
        issues.append("last — corruption : archive illisible.")
        with patch.object(services, "request") as request:
            services.notify(self.config, {}, issues)
        messages = [
            call.kwargs["content"]["content"] for call in request.call_args_list
        ]
        self.assertTrue(any("last — corruption" in message for message in messages))
        self.assertTrue(all(len(message) <= 2000 for message in messages))

    def test_discord_resumes_after_a_partial_send_without_starving_last_pages(self):
        url = self.root / "discord"
        url.write_text("https://discord.com/api/webhooks/test/token")
        url.chmod(0o600)
        self.config["notifications"] = {"discord_url_file": str(url)}
        issues = [
            f"base{index} — corruption : " + "detail " * 100 for index in range(12)
        ]
        state = {}
        with patch.object(
            services,
            "request",
            side_effect=[None, core.Failure("notification", "quota")],
        ) as request:
            with self.assertRaises(core.Failure):
                services.notify(self.config, state, issues)
        first = request.call_args_list[0].kwargs["content"]["content"]
        self.assertNotIn("notified", state)
        with patch.object(services, "request") as retry:
            services.notify(self.config, state, issues)
        self.assertTrue(retry.called)
        self.assertNotIn(
            first, [call.kwargs["content"]["content"] for call in retry.call_args_list]
        )
        self.assertEqual(json.dumps(issues, sort_keys=True), state["notified"])

    def test_discord_recovers_even_if_only_part_of_the_initial_alarm_was_sent(self):
        url = self.root / "discord"
        url.write_text("https://discord.com/api/webhooks/test/token")
        url.chmod(0o600)
        self.config["notifications"] = {"discord_url_file": str(url)}
        state = {"notification_progress": {"signature": "old", "next": 1}}
        with patch.object(services, "request") as request:
            services.notify(self.config, state, [])
        self.assertIn(
            "retour à la normale", request.call_args.kwargs["content"]["content"]
        )

    def test_discord_recovers_after_partial_alarm_from_a_previously_healthy_state(self):
        url = self.root / "discord"
        url.write_text("https://discord.com/api/webhooks/test/token")
        url.chmod(0o600)
        self.config["notifications"] = {"discord_url_file": str(url)}
        state = {
            "notified": "[]",
            "notification_progress": {"signature": "alarm", "next": 1},
        }
        with patch.object(services, "request") as request:
            services.notify(self.config, state, [])
        self.assertIn(
            "retour à la normale", request.call_args.kwargs["content"]["content"]
        )
        self.assertNotIn("notification_progress", state)

    def test_historical_snapshot_uses_its_original_job_and_encoding(self):
        self.collect()
        self.base.update(job="renamed", folder="mariadb/renamed", format="gpg-gzip")
        snapshot = self.repo.snapshots(self.base)[0]
        with tempfile.TemporaryDirectory(dir=self.state) as temp:
            manifest = self.repo.verify(self.base, snapshot, temp)
        self.assertEqual("job", manifest["job"])
        self.assertEqual("gzip", manifest["format"])

    def test_plain_archive_restores_after_the_source_switches_to_gpg(self):
        self.healthy()
        self.base["format"] = "gpg-gzip"
        with patch.object(main, "now", return_value=AT), patch.object(main, "ping"):
            self.assertEqual(0, self.operator.restore())

    def test_renaming_a_job_does_not_duplicate_its_identical_archives(self):
        self.collect()
        self.folder.rename(self.source / "mariadb/renamed")
        self.base.update(job="renamed", folder="mariadb/renamed")
        state = self.operator.base_state(self.base)
        state["files"], state["remote"] = {}, {}
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)

    def test_repeated_success_marker_keeps_the_original_verified_copy(self):
        self.base["completion"]["mode"] = "success-marker"
        marker = self.file.with_name(self.file.name + ".complete.json")
        data = {
            "filename": self.file.name,
            "bytes": self.file.stat().st_size,
            "sha256": core.sha256(self.file),
            "completed_at": "2026-11-06T03:02:00+00:00",
        }
        core.save(marker, data)
        self.collect()
        data["completed_at"] = "2026-11-06T03:04:00+00:00"
        core.save(marker, data)
        state = self.operator.base_state(self.base)
        state["files"], state["remote"] = {}, {}
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)

    def test_state_cannot_reuse_proofs_for_a_different_destination(self):
        self.collect()
        config = dict(self.config, repository="rclone:dropbox:Other/mariadb-offsite-v1")
        with self.assertRaises(core.Failure):
            main.Operator(config, FakeRepository(), self.sandbox)

    def test_unbound_legacy_state_is_not_silently_trusted(self):
        self.collect()
        state = core.load(self.operator.path)
        state.pop("repository", None)
        state.pop("repository_id", None)
        core.save(self.operator.path, state)
        with self.assertRaises(core.Failure):
            main.Operator(self.config, self.repo, self.sandbox)

    def test_operation_reloads_state_after_acquiring_lock(self):
        queued = main.Operator(self.config, self.repo, self.sandbox)
        self.collect()  # Un autre processus a enregistré sa copie entre-temps.
        with queued.operation("ack-gap"):
            queued.state["audit"] = "completed"
            queued.persist()
        state = core.load(queued.path)
        self.assertIn("last_external_verified", state["bases"]["base"])

    def test_historical_snapshot_remains_readable_after_schedule_change(self):
        self.collect()
        self.base["schedule"] = "0 4 * * *"
        snapshot = self.repo.snapshots(self.base)[0]
        with tempfile.TemporaryDirectory(dir=self.state) as temp:
            manifest = self.repo.verify(self.base, snapshot, temp)
        self.assertEqual("2026-11-06T03:00:00+00:00", manifest["slot"])

    def test_resume_old_snapshot_keeps_its_original_due_slot(self):
        self.collect()
        self.base["schedule"] = "0 4 * * *"
        state = self.operator.base_state(self.base)
        state["files"] = {}
        state["remote"] = {}
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)
        self.assertEqual(
            "2026-11-06T03:00:00+00:00", next(iter(state["local"].values()))["slot"]
        )

    def test_cached_copy_keeps_its_original_slot_after_permissions_change(self):
        self.collect()
        self.base["schedule"] = "0 4 * * *"
        self.file.touch()  # Empreinte des attributs différente, contenu inchangé.
        self.assertEqual([], self.collect())
        state = self.operator.base_state(self.base)
        self.assertEqual(
            "2026-11-06T03:00:00+00:00", next(iter(state["local"].values()))["slot"]
        )

    def test_recovery_continues_after_one_corrupt_base(self):
        self.collect()
        first = self.repo.snapshots(self.base)[0]
        self.repo.payloads[first["id"]]["dump"] = b"altered"
        other = copy.deepcopy(self.base)
        other.update(id="other", database="other", job="other", folder="mariadb/other")
        self.config["bases"].append(other)
        folder = self.source / other["folder"]
        folder.mkdir()
        (folder / self.file.name).write_bytes(self.file.read_bytes())
        self.assertEqual([], self.operator.collect_base(other, AT))
        self.operator.path.unlink()
        recovered = main.Operator(self.config, self.repo, self.sandbox)
        self.assertEqual(1, recovered.recover())
        self.assertIn("last_external_verified", recovered.base_state(other))
        self.assertTrue(recovered.state["recovery_issues"])

    def test_collect_cannot_clear_a_known_repository_integrity_failure(self):
        self.healthy()
        self.operator.state["repository_integrity_error"] = (
            "corruption : bloc ancien altéré."
        )
        with (
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "inventory", return_value=([], {})),
        ):
            self.assertEqual(1, self.operator.run())

    def test_corruption_alert_clears_only_after_a_successful_repository_check(self):
        self.healthy()
        self.operator.state["repository_integrity_error"] = (
            "corruption : bloc ancien altéré."
        )
        with (
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(main, "ping"),
        ):
            self.assertEqual(1, self.operator.run())
            self.assertEqual(0, self.operator.restore())

    def test_retention_refuses_known_repository_corruption(self):
        self.healthy()
        self.operator.state["repository_integrity_error"] = (
            "corruption : bloc ancien altéré."
        )
        with (
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "preflight"),
        ):
            with self.assertRaises(core.Failure):
                self.operator.retention(apply=True)
        self.assertFalse(
            any(args[0] in ("forget", "prune") for args in self.repo.calls)
        )

    def test_repository_check_failure_is_visible_before_an_interrupted_import(self):
        self.healthy()
        observed = []

        def interrupt_import(base, sql):
            watcher = main.Operator(self.config, self.repo, self.sandbox)
            with (
                patch.object(watcher, "inventory", return_value=([], {})),
                patch.object(main, "notify"),
                patch.object(main, "ping") as ping,
            ):
                observed.append((watcher.monitor(), ping.call_args.args[-1]))
            raise KeyboardInterrupt()

        with (
            patch.object(main, "now", return_value=AT),
            patch.object(
                self.repo,
                "run",
                side_effect=core.Failure("corruption", "archive ancienne altérée"),
            ),
            patch.object(self.sandbox, "run", side_effect=interrupt_import),
            self.operator.operation("restore-check"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.operator.restore()
        self.assertEqual([(1, False)], observed)
        restarted = main.Operator(self.config, self.repo, self.sandbox)
        self.assertTrue(restarted.state.get("repository_integrity_error"))

    def test_recovery_anomaly_stays_visible_until_comprehensive_recovery(self):
        self.healthy()
        self.operator.state["recovery_issues"] = [
            "base — corruption : archive ancienne illisible."
        ]
        with (
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(main, "ping") as ping,
        ):
            self.assertEqual(1, self.operator.run())
            self.assertEqual(1, self.operator.restore())
            self.assertIs(False, ping.call_args.args[-1])
            with patch.object(self.operator, "preflight"):
                with self.assertRaises(core.Failure):
                    self.operator.retention(apply=True)
            self.assertEqual(0, self.operator.recover())
            self.assertEqual([], self.operator.state["recovery_issues"])

    def test_recovery_fault_is_durable_if_the_next_read_is_interrupted(self):
        self.healthy()
        self.operator.path.unlink()
        recovered = main.Operator(self.config, self.repo, self.sandbox)
        dump = self.repo.snapshots(self.base)[0]
        with (
            patch.object(self.repo, "snapshots", side_effect=[[dump, dump]]),
            patch.object(
                self.repo,
                "verify",
                side_effect=[
                    core.Failure("corruption", "archive altérée"),
                    KeyboardInterrupt(),
                ],
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                recovered.recover()
        self.assertTrue(core.load(recovered.path)["recovery_issues"])

    def test_retention_rechecks_deadlines_instead_of_trusting_a_recent_bilan(self):
        self.healthy()
        self.operator.state["last_collection_run"] = "2026-11-07T04:59:00+00:00"
        with (
            patch.object(
                main, "now", return_value=core.date("2026-11-07T05:01:00+00:00")
            ),
            patch.object(self.operator, "preflight"),
        ):
            with self.assertRaises(core.Failure):
                self.operator.retention(apply=True)

    def test_orphaned_operation_metadata_does_not_hide_a_stopped_collector(self):
        self.healthy()
        core.save(
            self.state / "operation.json",
            {"kind": "restore-check", "started_at": core.iso(AT)},
        )
        with (
            patch.object(main, "now", return_value=AT + dt.timedelta(hours=2)),
            patch.object(self.operator, "inventory", return_value=([], {})),
            patch.object(main, "notify"),
            patch.object(main, "ping") as ping,
        ):
            self.assertEqual(1, self.operator.monitor())
        self.assertIs(False, ping.call_args.args[-1])

    def test_fractional_filename_timestamps_roundtrip_at_restic_precision(self):
        name = "daily_2026-11-06_030000.123456.sql.gz"
        self.file.rename(self.folder / name)
        self.base.update(
            timestamp_regex=r"daily_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{6}\.\d{6})\.sql\.gz",
            timestamp_format="%Y-%m-%d_%H%M%S.%f",
        )
        self.assertEqual([], self.collect())
        self.assertEqual(1, self.repo.created)
