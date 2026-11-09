"""Sauvegarde externe des dumps database-backup, sans génération concurrente."""

import argparse
import contextlib
import copy
import datetime as dt
import gzip
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from core import (
    TAG,
    Failure,
    artifact_id,
    check_gzip,
    date,
    engine,
    filename_date,
    fingerprint,
    inside,
    iso,
    load,
    lock,
    lock_is_held,
    now,
    previous_slot,
    private_file,
    restoration_image,
    retention_candidates,
    save,
    secret,
    sha256,
    slots,
    source_identity,
    validate,
)
from services import (
    Repository,
    RestoreSandbox,
    check_jobs,
    compare_inventory,
    discover,
    execute,
    notification_url,
    notify,
    ping,
)


def problem(error):
    if isinstance(error, Failure):
        return f"{error.category} : {error}"
    # JSON, SQL, URL, noms de fichiers et exceptions tierces ne sont jamais repris.
    return (
        "configuration : données illisibles ou opération impossible (détail expurgé)."
    )


def sql_file(config, base, dump, directory, *, archive_format=None):
    """Déchiffre si nécessaire, valide CRC/EOF gzip, puis décompresse en privé."""
    compressed = Path(dump)
    encoding = (
        archive_format if archive_format is not None else base.get("format", "gzip")
    )
    if encoding not in ("gzip", "gpg-gzip"):
        raise Failure("configuration", "Encodage de l'archive non pris en charge.")
    if encoding == "gpg-gzip":
        if not base.get("gpg_passphrase_file"):
            raise Failure(
                "configuration", "Phrase GPG de cette archive historique requise."
            )
        password = private_file(base["gpg_passphrase_file"])
        secret(password)
        compressed = Path(directory) / "decrypted.sql.gz"
        (Path(directory) / "gnupg").mkdir(mode=0o700, exist_ok=True)
        with compressed.open("wb") as output:
            execute(
                [
                    "gpg",
                    "--no-options",
                    "--batch",
                    "--no-tty",
                    "--pinentry-mode",
                    "loopback",
                    "--no-symkey-cache",
                    "--no-autostart",
                    "--homedir",
                    str(Path(directory) / "gnupg"),
                    "--passphrase-file",
                    password,
                    "--output",
                    "-",
                    "--decrypt",
                    dump,
                ],
                stdout=output,
                timeout=config.get("command_timeout", 1800),
                category="corruption",
            )
    size = check_gzip(compressed, base["minimum_sql_bytes"])
    target = Path(directory) / "restore.sql"
    with gzip.open(compressed, "rb") as source, target.open("wb") as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    if engine(base) == "sqlite":
        # sqlite3 .dump peut terminer par ROLLBACK en cas de corruption.
        with target.open("rb") as stream:
            stream.seek(max(0, size - 256))
            if not stream.read().rstrip().endswith(b"COMMIT;"):
                raise Failure("corruption", "Dump SQLite sans fin COMMIT confirmée.")
    return target, size


def stage_dump(config, base, path, directory, at):
    generated = filename_date(base, path.name)
    if generated > at:
        raise Failure("generation", "Horodatage de dump dans le futur.")
    before = fingerprint(path)
    if before[2] == 0:
        raise Failure("corruption", "Dump vide.")
    completion = base["completion"]
    completed = generated
    marker = None
    if completion["mode"] == "success-marker":
        marker_path = path.with_name(path.name + ".complete.json")
        if not marker_path.is_file():
            return (
                None  # Le producteur travaille encore ; échéance surveillée séparément.
            )
        if marker_path.is_symlink():
            raise Failure("generation", "Marqueur symbolique refusé.")
        marker = load(marker_path)
        completed = date(marker["completed_at"])
        if marker["filename"] != path.name or completed > at or completed < generated:
            raise Failure("generation", "Marqueur de fin incohérent.")
    target = Path(directory) / "dump"
    shutil.copyfile(path, target)
    digest = sha256(target)
    if fingerprint(path) != before:
        raise Failure("generation", "Dump modifié pendant la collecte.")
    if marker is not None and (
        marker["sha256"] != digest or marker["bytes"] != before[2]
    ):
        raise Failure("corruption", "Dump différent du marqueur de fin.")
    _, sql_bytes = sql_file(config, base, target, directory)
    artifact = artifact_id(base, path.name, digest)
    manifest = {
        "version": 1,
        "base": base["id"],
        "database": base["database"],
        "job": base["job"],
        "filename": path.name,
        "artifact": artifact,
        "format": base.get("format", "gzip"),
        "generated_at": iso(generated),
        "completed_at": iso(completed),
        "collected_at": iso(at),
        "slot": iso(previous_slot(base, generated)),
        "bytes": before[2],
        "sql_bytes": sql_bytes,
        "sha256": digest,
    }
    if engine(base) == "sqlite":
        manifest.update(engine="sqlite", sqlite_path=base["sqlite_path"])
    save(Path(directory) / "manifest.json", manifest)
    # Seuls le dump d'origine et le manifeste chiffrés partent chez Restic.
    (Path(directory) / "restore.sql").unlink()
    (Path(directory) / "decrypted.sql.gz").unlink(missing_ok=True)
    shutil.rmtree(Path(directory) / "gnupg", ignore_errors=True)
    return manifest


def register_local(state, manifest):
    existing = state.setdefault("local", {})
    existing[manifest["artifact"]] = {
        key: manifest[key]
        for key in (
            "slot",
            "generated_at",
            "completed_at",
            "sha256",
            "filename",
            "sql_bytes",
        )
    }
    latest = max(existing.values(), key=lambda item: date(item["generated_at"]))
    state["last_local_completed"] = latest["completed_at"]


def register_remote(state, manifest, snapshot, checked):
    state.setdefault("remote", {})[manifest["artifact"]] = {
        "snapshot": snapshot["id"],
        "generated_at": manifest["generated_at"],
        "slot": manifest["slot"],
        "verified_at": iso(checked),
        "sha256": manifest["sha256"],
    }
    latest = max(state["remote"].values(), key=lambda item: date(item["generated_at"]))
    state["last_external_verified"] = latest
    state["last_integrity_success"] = {
        "checked_at": iso(checked),
        "snapshot": snapshot["id"],
        "sha256": manifest["sha256"],
    }


def deadline_issues(config, base, state, at):
    """Conserver chaque échéance manquée, même si les suivantes réussissent."""
    enabled = date(base["enabled_since"])
    # Dès l'installation, contrôler la dernière échéance déjà due : activer à
    # 16 h ne rend pas acceptable un dump de 3 h manquant ou datant d'hier.
    first_due = previous_slot(base, enabled)
    start = date(state["audited_until"]) if state.get("audited_until") else first_due
    # Une interruption très longue reste explicitement visible sans boucle infinie.
    if at - start > dt.timedelta(days=32):
        state["unobserved_since"] = iso(start)
        start = at - dt.timedelta(days=32)
    expected = state.setdefault("expected", {})
    for slot in slots(base, max(start, first_due), at):
        expected.setdefault(iso(slot), {})
    state["audited_until"] = iso(at)
    local_slots = {item["slot"] for item in state.get("local", {}).values()}
    remote_slots = {item["slot"] for item in state.get("remote", {}).values()}
    acknowledged = state.get("acknowledged_gaps", {})
    issues = []
    if state.get("unobserved_since") and "unobserved" not in acknowledged:
        issues.append(
            "generation : interruption de contrôle supérieure à 32 jours, période à examiner."
        )
    for slot, details in expected.items():
        if date(slot) < first_due or slot in acknowledged:
            continue
        local_limit = date(slot) + dt.timedelta(
            seconds=config.get("generation_grace_seconds", 7200)
        )
        external_limit = local_limit + dt.timedelta(
            seconds=config.get("transfer_grace_seconds", 3600)
        )
        if slot not in local_slots and at > local_limit:
            details["generation_missed"] = True
            issues.append(f"generation : dump absent/incomplet pour l'échéance {slot}.")
        elif slot not in remote_slots and at > external_limit:
            details["transfer_missed"] = True
            issues.append(f"transfert : copie vérifiée absente pour l'échéance {slot}.")
        elif slot in remote_slots:
            details["recovered"] = True
    # Garder les écarts historiques ; borner seulement les succès anciens.
    cutoff = at - dt.timedelta(days=32)
    for slot in list(expected):
        details = expected[slot]
        if (
            date(slot) < cutoff
            and details.get("recovered")
            and not details.get("generation_missed")
            and not details.get("transfer_missed")
        ):
            del expected[slot]
    return issues


class Operator:
    def __init__(self, config, repository=None, sandbox=None):
        self.c = config
        self.root = Path(config["state_dir"])
        self.path = self.root / "status.json"
        self.state = self.read_state()
        self.repo = repository
        self.sandbox = sandbox or RestoreSandbox(config)

    def read_state(self):
        identity = {
            "repository": self.c["repository"],
            "repository_id": self.c["repository_id"],
        }
        if not self.path.exists():
            return {"version": 1, **identity, "databases": {}, "bases": {}}
        state = load(self.path)
        if state.get("version") != 1:
            raise Failure("configuration", "Version d'état inconnue.")
        if any(state.get(key) != value for key, value in identity.items()):
            raise Failure(
                "configuration",
                "État non lié à ce dépôt : conserver l'ancien état et utiliser recover-state sur un état neuf.",
            )
        if not isinstance(state.get("databases"), dict) or any(
            key not in state["databases"] for key in state["bases"]
        ):
            raise Failure(
                "configuration",
                "État sans identité des bases : reconstruire les preuves avec recover-state sur un état neuf.",
            )
        for base in self.c["bases"]:
            if (
                base["id"] in state["databases"]
                and state["databases"][base["id"]] != base["database"]
            ):
                raise Failure(
                    "configuration",
                    "Identifiant réaffecté à une autre base : choisir un nouvel identifiant.",
                )
            if base["id"] in state["databases"]:
                previous = state.get("sources", {}).get(
                    base["id"],
                    {"engine": "mariadb", "database": state["databases"][base["id"]]},
                )
                if previous != source_identity(base):
                    raise Failure(
                        "configuration",
                        "Source réaffectée : choisir un nouvel identifiant.",
                    )
        return state

    def persist(self):
        save(self.path, self.state)

    @contextlib.contextmanager
    def operation(self, kind):
        with lock(self.root / "operation.lock"):
            # L'instance peut avoir été construite pendant une autre opération.
            self.state = self.read_state()
            path = self.root / "operation.json"
            save(path, {"kind": kind, "started_at": iso(now())})
            try:
                yield
            finally:
                path.unlink(missing_ok=True)

    def base_state(self, base):
        identity = self.state["databases"].setdefault(base["id"], base["database"])
        if identity != base["database"]:
            raise Failure("configuration", "Identifiant réaffecté à une autre base.")
        sources = self.state.setdefault("sources", {})
        if sources.setdefault(base["id"], source_identity(base)) != source_identity(
            base
        ):
            raise Failure(
                "configuration", "Source réaffectée : choisir un nouvel identifiant."
            )
        return self.state["bases"].setdefault(base["id"], {})

    def repository(self):
        if self.repo is None:
            self.repo = Repository(self.c)
        self.repo.check_identity()
        return self.repo

    def inventory(self):
        errors = []
        try:
            if any(engine(base) == "mariadb" for base in self.c["bases"]):
                errors.extend(compare_inventory(self.c, discover(self.c)))
        except Exception as error:
            errors.append(problem(error))
        try:
            jobs = check_jobs(self.c)
        except Exception as error:
            jobs = {base["id"]: problem(error) for base in self.c["bases"]}
        return errors, jobs

    def preflight(self):
        errors, jobs = self.inventory()
        for key in (
            "discord_url_file",
            "healthcheck_url_file",
            "restore_healthcheck_url_file",
        ):
            notification_url(self.c, key)
        for base in self.c["bases"]:
            inside(self.c["source_root"], base["folder"]).stat()
            if base.get("format", "gzip") == "gpg-gzip":
                secret(base["gpg_passphrase_file"])
        self.repository()
        # Refuser un dépôt partagé, même si les filtres protégeraient ses snapshots.
        if any(TAG not in item.get("tags", []) for item in self.repo.snapshots()):
            errors.append("Dépôt contenant des sauvegardes étrangères.")
        errors.extend(f"{key} : {value}" for key, value in jobs.items())
        if errors:
            raise Failure("configuration", " ".join(errors))

    def collect_base(self, base, at):
        state = self.base_state(base)
        errors = []
        folder = inside(self.c["source_root"], base["folder"])
        if not folder.is_dir():
            raise Failure("generation", "Dossier de dumps absent.")
        snapshots = None
        for path in sorted(folder.glob(base["pattern"])):
            if any(piece in path.name for piece in (".part", ".tmp", ".complete.json")):
                continue
            try:
                inside(
                    self.c["source_root"], str(path.relative_to(self.c["source_root"]))
                )
                source_fingerprint = list(fingerprint(path))
                cached = state.get("files", {}).get(path.name)
                if cached and cached["fingerprint"] == source_fingerprint:
                    if cached["artifact"] in state.get("remote", {}):
                        continue
                with tempfile.TemporaryDirectory(
                    prefix="collect-", dir=self.root
                ) as temp:
                    manifest = stage_dump(self.c, base, path, temp, max(at, now()))
                    if manifest is None:
                        continue
                    prior = list(state.get("local", {}).values())
                    if any(
                        item["filename"] == manifest["filename"]
                        and item["sha256"] != manifest["sha256"]
                        for item in prior
                    ):
                        raise Failure(
                            "corruption",
                            "Dump terminé modifié après validation : conserver la copie originale et vérifier le producteur.",
                        )
                    if prior:
                        latest = max(prior, key=lambda item: date(item["generated_at"]))
                        if date(manifest["generated_at"]) > date(
                            latest["generated_at"]
                        ) and manifest["sql_bytes"] < latest["sql_bytes"] * base.get(
                            "minimum_size_ratio", 0.5
                        ):
                            raise Failure(
                                "corruption",
                                "Chute inhabituelle du volume SQL ; vérifier la base et le seuil.",
                            )
                    original = state.get("local", {}).get(manifest["artifact"])
                    if original:
                        manifest["slot"] = original["slot"]
                        manifest["completed_at"] = original["completed_at"]
                        save(Path(temp) / "manifest.json", manifest)
                    register_local(state, manifest)
                    self.persist()  # L'échec du transfert ne masque jamais la génération.
                    if manifest["artifact"] in state.get("remote", {}):
                        state.setdefault("files", {})[path.name] = {
                            "fingerprint": source_fingerprint,
                            "artifact": manifest["artifact"],
                        }
                        self.persist()
                        continue
                    repo = self.repository()
                    if snapshots is None:
                        snapshots = repo.snapshots(base)
                    matching = [
                        item
                        for item in snapshots
                        if "artifact=" + manifest["artifact"] in item.get("tags", [])
                    ]
                    if len(matching) > 1:
                        raise Failure("transfert", "Doublons externes à examiner.")
                    snapshot = (
                        matching[0]
                        if matching
                        else repo.create(
                            base,
                            temp,
                            date(manifest["generated_at"]),
                            manifest["artifact"],
                        )
                    )
                    with tempfile.TemporaryDirectory(
                        prefix="readback-", dir=self.root
                    ) as readback:
                        external = repo.verify(base, snapshot, readback)
                        if {
                            key: value
                            for key, value in external.items()
                            if key
                            not in (
                                "collected_at",
                                "completed_at",
                                "slot",
                                "job",
                                "sqlite_path",
                            )
                        } != {
                            key: value
                            for key, value in manifest.items()
                            if key
                            not in (
                                "collected_at",
                                "completed_at",
                                "slot",
                                "job",
                                "sqlite_path",
                            )
                        }:
                            raise Failure(
                                "corruption",
                                "Manifeste relu différent du manifeste local.",
                            )
                        sql_file(
                            self.c,
                            base,
                            Path(readback) / "dump",
                            readback,
                            archive_format=external["format"],
                        )
                    # Le manifeste authentifié conserve l'échéance d'origine,
                    # même après changement d'horaire du générateur.
                    register_local(state, external)
                    register_remote(state, external, snapshot, now())
                    state.setdefault("files", {})[path.name] = {
                        "fingerprint": source_fingerprint,
                        "artifact": manifest["artifact"],
                    }
                    self.persist()
                    if not matching:
                        snapshots.append(snapshot)
            except Exception as error:
                errors.append(problem(error))
        # Dédupliquer pour ne pas révéler de noms de fichiers ni produire de spam.
        return sorted(set(errors))

    def repository_issues(self):
        integrity = (
            ["dépôt — " + self.state["repository_integrity_error"]]
            if self.state.get("repository_integrity_error")
            else []
        )
        return integrity + self.state.get("recovery_issues", [])

    def report(self, issues, health_key="healthcheck_url_file", health_success=None):
        self.state["issues"] = sorted(set(issues + self.repository_issues()))
        self.state["checked_at"] = iso(now())
        self.persist()
        delivery_errors = []
        # Le watcher indépendant est le seul émetteur Discord/heartbeat régulier.
        # Les imports longs ne peuvent donc pas le priver de son signal.
        if health_key == "restore_healthcheck_url_file":
            try:
                ping(
                    self.c,
                    health_key,
                    not issues if health_success is None else health_success,
                )
            except Exception as error:
                delivery_errors.append(problem(error))
        self.state["notification_issues"] = delivery_errors
        self.persist()
        self.print_status()
        return 1 if self.state["issues"] or delivery_errors else 0

    def run(self):
        at = now()
        issues, jobs = [], {}
        try:
            inventory_errors, jobs = self.inventory()
            issues.extend("configuration : " + item for item in inventory_errors)
        except Exception as error:
            issues.append(problem(error))
        # Une erreur d'inventaire n'empêche pas de protéger les dumps connus.
        for base in self.c["bases"]:
            state = self.base_state(base)
            errors = []
            if base["id"] in jobs:
                errors.append("configuration : " + jobs[base["id"]])
            if base["id"] not in jobs:
                try:
                    errors.extend(self.collect_base(base, at))
                except Exception as error:
                    errors.append(problem(error))
            errors.extend(deadline_issues(self.c, base, state, now()))
            state["issues"] = sorted(set(errors))
            issues.extend(base["id"] + " — " + error for error in state["issues"])
            self.persist()
        self.state["pipeline_issues"] = issues
        self.state["last_collection_run"] = iso(now())
        integrity = (
            ["dépôt — " + self.state["repository_integrity_error"]]
            if self.state.get("repository_integrity_error")
            else []
        )
        return self.report(issues + self.restore_issues() + integrity)

    def restore_issues(self):
        issues = []
        for base in self.c["bases"]:
            state = self.base_state(base)
            if state.get("restore_error"):
                issues.append(
                    base["id"] + " — restauration : " + state["restore_error"]
                )
            restored = state.get("last_restore_success")
            if not restored or now() - date(restored["checked_at"]) > dt.timedelta(
                days=8
            ):
                issues.append(
                    base["id"]
                    + " — restauration : contrôle initial absent ou dernière réussite trop ancienne."
                )
        return issues

    def restore(self, selected=None, snapshot_id=None):
        repo = self.repository()
        errors = []
        try:
            repo.run("check", "--read-data")
            self.state.pop("repository_integrity_error", None)
        except Exception as error:
            self.state["repository_integrity_error"] = problem(error)
            errors.append("dépôt — " + self.state["repository_integrity_error"])
        # Publier le résultat avant les imports longs : le watcher doit voir
        # une corruption même si le premier import est ensuite interrompu.
        self.persist()
        for base in self.c["bases"]:
            if selected and selected != base["id"]:
                continue
            state = self.base_state(base)
            try:
                snapshots = sorted(
                    repo.snapshots(base),
                    key=lambda item: date(item["time"]),
                    reverse=True,
                )
                if snapshot_id:
                    snapshots = [
                        item for item in snapshots if item["id"] == snapshot_id
                    ]
                if not snapshots:
                    raise Failure(
                        "restauration",
                        "Aucune copie externe disponible pour cette base.",
                    )
                snapshot = snapshots[0]
                with tempfile.TemporaryDirectory(
                    prefix="restore-", dir=self.root
                ) as temp:
                    manifest = repo.verify(base, snapshot, temp)
                    sql, sql_bytes = sql_file(
                        self.c,
                        base,
                        Path(temp) / "dump",
                        temp,
                        archive_format=manifest["format"],
                    )
                    if sql_bytes != manifest["sql_bytes"]:
                        raise Failure(
                            "corruption", "Volume SQL relu différent du manifeste."
                        )
                    self.sandbox.run(base, sql)
                report = {
                    "version": 1,
                    "base": base["id"],
                    "snapshot": snapshot["id"],
                    "generated_at": manifest["generated_at"],
                    "checked_at": iso(now()),
                    "sha256": manifest["sha256"],
                    "image": restoration_image(self.c, base),
                    "success": True,
                    "scheduled_check": snapshot_id is None,
                }
                if engine(base) == "sqlite":
                    report["engine"] = "sqlite"
                # La preuve de restauration est aussi récupérable sans Unraid.
                with tempfile.TemporaryDirectory(
                    prefix="report-", dir=self.root
                ) as temp:
                    save(Path(temp) / "restore-report.json", report)
                    report_snapshot = repo.create(
                        base, temp, now(), report["snapshot"], kind="report"
                    )
                    fetched = Path(temp) / "readback.json"
                    repo.fetch(report_snapshot, "restore-report.json", fetched)
                    if load(fetched) != report:
                        raise Failure(
                            "corruption", "Rapport de restauration externe incohérent."
                        )
                register_remote(state, manifest, snapshot, now())
                if snapshot_id is None:
                    state["last_restore_success"] = report
                    state.pop("restore_error", None)
                else:
                    state["last_manual_restore_success"] = report
            except Exception as error:
                failure = problem(error)
                if snapshot_id is None:
                    state["restore_error"] = failure
                errors.append(base["id"] + " — " + failure)
            self.persist()
        # Pas de faux retour à la normale pour une panne de transfert existante.
        integrity_issues = (
            ["dépôt — " + self.state["repository_integrity_error"]]
            if self.state.get("repository_integrity_error")
            else []
        )
        issues = (
            self.state.get("pipeline_issues", [])
            + integrity_issues
            + self.restore_issues()
        )
        if snapshot_id:
            issues += errors
        return self.report(
            issues,
            "restore_healthcheck_url_file"
            if snapshot_id is None
            else "healthcheck_url_file",
            health_success=not errors
            and not self.restore_issues()
            and not self.repository_issues(),
        )

    def monitor(self):
        """Lecture d'état indépendante du verrou de transfert/restauration."""
        self.state = self.read_state()
        at = now()
        issues, jobs = self.inventory()
        issues = ["configuration : " + item for item in issues]
        operation_path = self.root / "operation.json"
        try:
            operation = load(operation_path)
        except FileNotFoundError:
            operation = {}
        active_operation = (
            lock_is_held(self.root / "operation.lock")
            and operation.get("kind")
            in (
                "run",
                "restore",
                "restore-check",
                "retention",
            )
            and dt.timedelta(0)
            <= at - date(operation["started_at"])
            < dt.timedelta(hours=24)
        )
        if (
            not self.state.get("last_collection_run")
            or at - date(self.state["last_collection_run"]) > dt.timedelta(minutes=45)
        ) and not active_operation:
            issues.append(
                "configuration : collecteur sans bilan récent (plus de 45 minutes)."
            )
        for base in self.c["bases"]:
            state = self.base_state(base)
            base_issues = list(state.get("issues", []))
            if not inside(self.c["source_root"], base["folder"]).is_dir():
                base_issues.append("generation : dossier source absent.")
            base_issues += deadline_issues(self.c, base, copy.deepcopy(state), at)
            if base["id"] in jobs:
                base_issues.append("configuration : " + jobs[base["id"]])
            issues.extend(base["id"] + " — " + item for item in base_issues)
        issues += self.restore_issues()
        issues += self.repository_issues()
        watcher_path = self.root / "monitor.json"
        watcher = load(watcher_path) if watcher_path.exists() else {}
        watcher.update(checked_at=iso(at), issues=sorted(set(issues)))
        # Une panne d'écriture doit être connue avant toute annonce de succès.
        # Le statut « notified » reste celui du dernier envoi confirmé.
        save(watcher_path, watcher)
        delivery_errors = []
        try:
            notify(self.c, watcher, watcher["issues"])
        except Exception as error:
            delivery_errors.append(problem(error))
        try:
            ping(self.c, "healthcheck_url_file", not issues and not delivery_errors)
        except Exception as error:
            delivery_errors.append(problem(error))
        watcher["notification_issues"] = delivery_errors
        save(watcher_path, watcher)
        for issue in watcher["issues"] + delivery_errors:
            print(issue)
        if not issues and not delivery_errors:
            print("Surveillance : toutes les bases sont protégées et contrôlées.")
        return 1 if issues or delivery_errors else 0

    def recover(self):
        """Reconstruire les copies lisibles sans bloquer les autres bases."""
        repo = self.repository()
        errors = []

        def record_error(base, error):
            message = base["id"] + " — " + problem(error)
            errors.append(message)
            self.state["recovery_issues"] = sorted(
                set(self.state.get("recovery_issues", []) + [message])
            )
            self.persist()

        for base in self.c["bases"]:
            state = self.base_state(base)
            try:
                snapshots = repo.snapshots(base)
            except Exception as error:
                record_error(base, error)
                continue
            if not snapshots:
                record_error(
                    base, Failure("transfert", "Aucune copie externe disponible.")
                )
            for snapshot in snapshots:
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="recover-", dir=self.root
                    ) as temp:
                        manifest = repo.verify(base, snapshot, temp)
                        _, size = sql_file(
                            self.c,
                            base,
                            Path(temp) / "dump",
                            temp,
                            archive_format=manifest["format"],
                        )
                        if size != manifest["sql_bytes"]:
                            raise Failure(
                                "corruption", "Volume SQL relu différent du manifeste."
                            )
                        register_local(state, manifest)
                        register_remote(state, manifest, snapshot, now())
                except Exception as error:
                    record_error(base, error)
                self.persist()
            try:
                reports = sorted(
                    repo.snapshots(base, "report"),
                    key=lambda item: date(item["time"]),
                    reverse=True,
                )
            except Exception as error:
                record_error(base, error)
                continue
            for snapshot in reports:
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="recover-report-", dir=self.root
                    ) as temp:
                        path = Path(temp) / "restore-report.json"
                        repo.fetch(snapshot, "restore-report.json", path)
                        report = load(path)
                        if (
                            report.get("version") != 1
                            or report.get("base") != base["id"]
                            or report.get("engine", "mariadb") != engine(base)
                            or report.get("success") is not True
                        ):
                            raise Failure(
                                "corruption", "Rapport de restauration invalide."
                            )
                        date(report["checked_at"])
                        date(report["generated_at"])
                        if report.get("scheduled_check") is True:
                            state["last_restore_success"] = report
                            break
                except Exception as error:
                    record_error(base, error)
            self.persist()
        self.state["recovery_issues"] = sorted(set(errors))
        self.state["issues"] = sorted(
            set(
                self.state.get("pipeline_issues", [])
                + self.restore_issues()
                + self.repository_issues()
            )
        )
        self.persist()
        self.print_status()
        return 1 if errors else 0

    def retention(self, apply=False):
        # La source et toutes les bases doivent être saines pour autoriser la purge.
        self.preflight()
        if (
            not self.state.get("last_collection_run")
            or now() - date(self.state["last_collection_run"]) > dt.timedelta(hours=1)
            or self.state.get("issues")
            or self.state.get("notification_issues")
            or self.repository_issues()
            or self.restore_issues()
        ):
            raise Failure(
                "configuration",
                "Purge refusée : dernier contrôle absent, ancien ou en échec.",
            )
        repo = self.repository()
        all_snapshots = repo.snapshots()
        known_bases = {"base=" + base["id"] for base in self.c["bases"]}
        if any(
            TAG not in item.get("tags", [])
            or not known_bases.intersection(item.get("tags", []))
            for item in all_snapshots
        ):
            raise Failure(
                "configuration",
                "Purge refusée : dépôt partagé ou base retirée de l'inventaire.",
            )
        candidates = []
        for base in self.c["bases"]:
            state = self.base_state(base)
            if deadline_issues(self.c, base, copy.deepcopy(state), now()):
                raise Failure(
                    "configuration",
                    "Purge refusée : échéance de génération ou de transfert en défaut.",
                )
            folder = inside(self.c["source_root"], base["folder"])
            if not any(path.is_file() for path in folder.glob(base["pattern"])):
                raise Failure("generation", "Purge refusée : dossier source vide.")
            verified = state.get("last_external_verified", {}).get("snapshot")
            restored = state.get("last_restore_success", {}).get("snapshot")
            if not verified or not restored:
                raise Failure(
                    "configuration", "Purge refusée : copie/restauration non prouvée."
                )
            snapshots = repo.snapshots(base)
            proven = {item["snapshot"] for item in state.get("remote", {}).values()}
            if any(item["id"] not in proven for item in snapshots):
                raise Failure(
                    "corruption",
                    "Purge refusée : instantané sans preuve de relecture (recover-state requis).",
                )
            if verified not in {item["id"] for item in snapshots} or restored not in {
                item["id"] for item in snapshots
            }:
                raise Failure(
                    "corruption", "Purge refusée : instantané protégé absent."
                )
            # Revalider la copie protégée avant toute suppression.
            with tempfile.TemporaryDirectory(
                prefix="prune-check-", dir=self.root
            ) as temp:
                protected_snapshot = next(
                    item for item in snapshots if item["id"] == verified
                )
                manifest = repo.verify(base, protected_snapshot, temp)
                sql_file(
                    self.c,
                    base,
                    Path(temp) / "dump",
                    temp,
                    archive_format=manifest["format"],
                )
            candidates += retention_candidates(snapshots, {verified, restored}, now())
            reports = repo.snapshots(base, "report")
            candidates += retention_candidates(reports, set(), now())
        print(json.dumps({"apply": apply, "snapshots_to_remove": candidates}, indent=2))
        if apply:
            for snapshot_id in candidates:
                repo.run("forget", snapshot_id)
            repo.run("prune", "--max-repack-size", "256M")
        return 0

    def print_status(self):
        # Le JSON complet contient des chemins privés ; seul le bilan est affiché.
        print("Contrôle : " + self.state.get("checked_at", "non exécuté"))
        for base in self.c["bases"]:
            state = self.base_state(base)
            remote = state.get("last_external_verified", {})
            restore = state.get("last_restore_success", {})
            print(
                f"{base['id']} : local={state.get('last_local_completed', 'absent')} "
                f"externe={remote.get('generated_at', 'absent')} "
                f"restauration={restore.get('checked_at', 'absente')}"
            )
        for issue in self.state.get("issues", []) + self.state.get(
            "notification_issues", []
        ):
            print(issue)

    def status(self):
        self.print_status()
        path = self.root / "monitor.json"
        watcher = load(path) if path.exists() else {}
        issues = watcher.get("issues", []) + watcher.get("notification_issues", [])
        if not watcher.get("checked_at") or now() - date(
            watcher["checked_at"]
        ) > dt.timedelta(minutes=45):
            issues.append("configuration : surveillance absente ou trop ancienne.")
        issues += self.repository_issues()
        for issue in sorted(set(issues)):
            print(issue)
        return (
            1
            if (
                issues
                or not self.state.get("checked_at")
                or self.state.get("issues")
                or self.state.get("notification_issues")
                or self.restore_issues()
            )
            else 0
        )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "command",
        choices=(
            "init",
            "preflight",
            "run",
            "monitor",
            "status",
            "restore-check",
            "restore",
            "recover-state",
            "retention",
            "ack-gap",
        ),
    )
    parser.add_argument("--base")
    parser.add_argument("--snapshot")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--slot")
    parser.add_argument("--reason")
    args = parser.parse_args()
    config = None
    try:
        config = load(private_file(args.config))
        validate(config)
        operator = Operator(config)
        if args.base and args.base not in {base["id"] for base in config["bases"]}:
            raise Failure("configuration", "Base inconnue.")
        if args.command == "status":
            return operator.status()
        if args.command == "monitor":
            with lock(operator.root / "monitor.lock"):
                return operator.monitor()
        with operator.operation(args.command):
            if args.command == "init":
                if (
                    args.confirm != config["repository"]
                    or config["repository_id"] != "SET_AFTER_INIT"
                ):
                    raise Failure(
                        "configuration", "Confirmer le dépôt dédié non initialisé."
                    )
                repo = Repository(config)
                repo.run("init")
                print("repository_id=" + repo.identity())
                return 0
            if args.command == "preflight":
                operator.preflight()
                print("Précontrôle réussi.")
                return 0
            if args.command == "run":
                operator.sandbox.cleanup_stale()
                return operator.run()
            if args.command in ("restore-check", "restore"):
                if args.command == "restore" and (
                    not args.base or not args.snapshot or args.confirm != "isolated"
                ):
                    raise Failure(
                        "configuration",
                        "Restauration isolée : --base, --snapshot et --confirm isolated requis.",
                    )
                operator.sandbox.cleanup_stale()
                return operator.restore(args.base, args.snapshot)
            if args.command == "recover-state":
                return operator.recover()
            if args.command == "retention":
                if args.apply and args.confirm != config["repository_id"]:
                    raise Failure(
                        "configuration",
                        "Confirmer l'identité du dépôt pour appliquer la rétention.",
                    )
                if args.apply and args.dry_run:
                    raise Failure("configuration", "Choisir simulation ou application.")
                return operator.retention(args.apply)
            if args.command == "ack-gap":
                if (
                    not args.base
                    or not args.slot
                    or not args.reason
                    or len(args.reason) < 10
                ):
                    raise Failure(
                        "configuration",
                        "Acquittement explicite : base, échéance et motif requis.",
                    )
                base = next(base for base in config["bases"] if base["id"] == args.base)
                state = operator.base_state(base)
                if args.slot not in state.get("expected", {}) and not (
                    args.slot == "unobserved" and state.get("unobserved_since")
                ):
                    raise Failure("configuration", "Écart inconnu.")
                state.setdefault("acknowledged_gaps", {})[args.slot] = {
                    "reason": args.reason,
                    "at": iso(now()),
                }
                operator.persist()
                print("Écart acquitté ; aucune sauvegarde créée ou déclarée valide.")
                return 0
    except Exception as error:
        print(problem(error), file=sys.stderr)
        # Une configuration invalide ou un crash ne doit jamais envoyer un succès.
        if (
            config
            and args.command not in ("status", "init", "ack-gap", "run")
            and not (isinstance(error, Failure) and error.category == "concurrence")
        ):
            try:
                ping(
                    config,
                    "restore_healthcheck_url_file"
                    if args.command in ("restore", "restore-check")
                    else "healthcheck_url_file",
                    False,
                )
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
