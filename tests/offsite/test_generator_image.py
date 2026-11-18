"""Image unique du générateur, compatibilité et maintien du blocage de collecte."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_offsite import AT, Fixture, core, main, services


class GeneratorImageTests(Fixture):
    def add_base(self, mode="atomic-rename", engine="mariadb"):
        base = copy.deepcopy(self.base)
        base.update(
            id="second",
            database="second",
            job="second",
            folder=engine + "/second",
            engine=engine,
        )
        base["completion"]["mode"] = mode
        if engine == "sqlite":
            base["sqlite_path"] = "/sqlite/second.sqlite3"
            self.config["sqlite_restore_image"] = "sha256:" + "c" * 64
        self.config["bases"].append(base)
        return base

    def inspect_jobs(self, image):
        jobs = []
        for base in self.config["bases"]:
            job = {
                "name": base["job"],
                "type": core.engine(base),
                "schedule": base["schedule"],
            }
            if core.engine(base) == "sqlite":
                job["path"] = base["sqlite_path"]
            else:
                job.update(database=base["database"], host="mariadb", port=3306)
            jobs.append(job)
        Path(self.config["jobs_file"]).write_text(json.dumps({"jobs": jobs}))
        inspection = [{"Image": image, "Config": {"Env": ["TZ=UTC"]}}]
        mocked = patch.object(
            services, "execute", return_value=json.dumps(inspection).encode()
        )
        execute = mocked.start()
        self.addCleanup(mocked.stop)
        return execute

    def test_one_profile_id_covers_mariadb_and_sqlite(self):
        self.add_base(engine="sqlite")
        original = copy.deepcopy(self.config)
        core.validate(self.config)
        execute = self.inspect_jobs(self.config["generator_image_id"])
        self.assertEqual({}, services.check_jobs(self.config))
        execute.assert_called_once()
        self.assertEqual(original, self.config)

    def test_changed_image_blocks_every_atomic_base_but_not_success_markers(self):
        self.add_base(engine="sqlite")
        marker = self.add_base(mode="success-marker")
        marker.update(
            id="marker", database="marker", job="marker", folder="mariadb/marker"
        )
        core.validate(self.config)
        self.inspect_jobs("sha256:" + "b" * 64)
        errors = services.check_jobs(self.config)
        self.assertEqual({"base", "second"}, set(errors))
        self.assertTrue(all("Image" in message for message in errors.values()))
        # Une seule modification du profil suffit à rétablir toutes les bases.
        self.config["generator_image_id"] = "sha256:" + "b" * 64
        self.assertEqual({}, services.check_jobs(self.config))

    def test_matching_legacy_configuration_still_works_without_mutation(self):
        self.add_base(engine="sqlite")
        expected = self.config.pop("generator_image_id")
        for base in self.config["bases"]:
            base["completion"]["generator_image_id"] = expected
        original = copy.deepcopy(self.config)
        core.validate(self.config)
        self.inspect_jobs(expected)
        self.assertEqual({}, services.check_jobs(self.config))
        self.assertEqual(original, self.config)

    def test_legacy_configuration_still_rejects_an_actual_image_change(self):
        self.base["completion"]["generator_image_id"] = self.config.pop(
            "generator_image_id"
        )
        self.inspect_jobs("sha256:" + "b" * 64)
        self.assertIn("Image", services.check_jobs(self.config)["base"])

    def test_divergent_legacy_ids_are_rejected(self):
        second = self.add_base()
        self.base["completion"]["generator_image_id"] = self.config.pop(
            "generator_image_id"
        )
        second["completion"]["generator_image_id"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(core.Failure, "divergents"):
            core.validate(self.config)

    def test_conflicting_global_and_legacy_ids_are_not_silently_overridden(self):
        self.base["completion"]["generator_image_id"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(core.Failure, "divergents"):
            core.validate(self.config)
        self.base["completion"]["generator_image_id"] = self.config[
            "generator_image_id"
        ]
        core.validate(self.config)

    def test_missing_legacy_id_on_one_atomic_base_is_rejected(self):
        self.add_base()
        self.base["completion"]["generator_image_id"] = self.config.pop(
            "generator_image_id"
        )
        with self.assertRaisesRegex(core.Failure, "divergents"):
            core.validate(self.config)

    def test_missing_or_invalid_profile_id_cannot_disable_the_image_guard(self):
        expected = self.config.pop("generator_image_id")
        with self.assertRaises(core.Failure):
            core.validate(self.config)
        self.base["completion"]["generator_image_id"] = expected
        for invalid in (None, "", "latest", "sha256:abc", 1, [], {}):
            with self.subTest(invalid=invalid):
                self.config["generator_image_id"] = invalid
                with self.assertRaises(core.Failure):
                    core.validate(self.config)

    def test_success_marker_only_profile_does_not_require_an_image_id(self):
        self.base["completion"]["mode"] = "success-marker"
        del self.config["generator_image_id"]
        core.validate(self.config)
        self.inspect_jobs("sha256:" + "b" * 64)
        self.assertEqual({}, services.check_jobs(self.config))

    def test_profile_id_does_not_replace_per_base_completion_evidence(self):
        self.base["completion"]["evidence"] = ""
        with self.assertRaisesRegex(core.Failure, "Preuve"):
            core.validate(self.config)

    def test_run_never_uploads_an_unapproved_image_and_resumes_after_profile_update(
        self,
    ):
        self.inspect_jobs("sha256:" + "b" * 64)
        with (
            patch.object(main, "discover", return_value={"mysql", "example"}),
            patch.object(main, "now", return_value=AT),
            patch.object(self.operator, "restore_issues", return_value=[]),
        ):
            self.assertEqual(1, self.operator.run())
            self.assertEqual(0, self.repo.created)
            self.config["generator_image_id"] = "sha256:" + "b" * 64
            self.assertEqual(0, self.operator.run())
            self.assertEqual(1, self.repo.created)
