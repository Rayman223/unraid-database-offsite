"""Contrats de configuration, échéances et intégrité, sans accès aux services."""

import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import re
import stat
import tempfile
import zlib
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

UTC = dt.timezone.utc
TAG = "bc-mariadb-offsite-v1"


class Failure(Exception):
    """Message contrôlé, sans sortie de sous-processus ni donnée SQL."""

    def __init__(self, category, message):
        self.category = category
        super().__init__(message)


def now():
    return dt.datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec="seconds")


def date(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise Failure("configuration", "Date avec fuseau obligatoire.")
    return parsed.astimezone(UTC)


def load(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def private_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise Failure("configuration", "Fichier privé régulier en mode 600 requis.")
    return path


def secret(path):
    value = private_file(path).read_text(encoding="utf-8").strip()
    if not value:
        raise Failure("configuration", "Secret vide.")
    return value


def engine(base):
    # Les configurations et archives antérieures désignent toujours MariaDB.
    return base.get("engine", "mariadb")


def source_identity(base):
    identity = {"engine": engine(base), "database": base["database"]}
    if engine(base) == "sqlite":
        identity["sqlite_path"] = base["sqlite_path"]
    return identity


def artifact_id(base, filename, digest):
    prefix = "sqlite\n" if engine(base) == "sqlite" else ""
    return hashlib.sha256(
        (prefix + base["id"] + "\n" + filename + "\n" + digest).encode()
    ).hexdigest()


def restoration_image(config, base):
    return config[
        "sqlite_restore_image" if engine(base) == "sqlite" else "restore_image"
    ]


@contextlib.contextmanager
def lock(path):
    with Path(path).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("concurrence", "Une exécution est déjà active.") from None
        yield


def lock_is_held(path):
    """Lire l'activité réelle, sans prendre un JSON orphelin pour un processus."""
    try:
        handle = Path(path).open("r")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def inside(root, relative):
    root = Path(root).resolve(strict=True)
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise Failure("configuration", "Chemin relatif hors source refusé.")
    # Refuser les symlinks, même s'ils reviennent dans la source.
    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise Failure("configuration", "Lien symbolique refusé.")
    if not path.resolve().is_relative_to(root):
        raise Failure("configuration", "Chemin hors source refusé.")
    return path


def validate(config):
    if not re.fullmatch(
        r"rclone:[A-Za-z0-9_-]+:[A-Za-z0-9_./-]+/mariadb-offsite-v1",
        config["repository"],
    ):
        raise Failure("configuration", "Dépôt rclone dédié mariadb-offsite-v1 requis.")
    if ".." in config["repository"].split(":", 2)[2].split("/"):
        raise Failure("configuration", "Dépôt relatif ambigu refusé.")
    if not re.fullmatch(r"[a-f0-9]{64}|SET_AFTER_INIT", config["repository_id"]):
        raise Failure("configuration", "Identité du dépôt invalide.")
    state = Path(config["state_dir"])
    if not state.is_dir() or state.stat().st_mode & 0o077:
        raise Failure("configuration", "Dossier d'état existant en mode 700 requis.")
    for key in (
        "source_root",
        "jobs_file",
        "state_dir",
        "rclone_config",
        "password_file",
    ):
        if not Path(config[key]).is_absolute():
            raise Failure("configuration", "Chemins absolus requis.")
    for key, default in (
        ("command_timeout", 1800),
        ("restore_timeout", 7200),
        ("generation_grace_seconds", 7200),
        ("transfer_grace_seconds", 3600),
    ):
        if (
            not isinstance(config.get(key, default), int)
            or not 1 <= config.get(key, default) <= 86400
        ):
            raise Failure("configuration", "Délais positifs bornés à un jour requis.")
    ids, databases, jobs = set(), set(), set()
    if not config["bases"]:
        raise Failure("configuration", "Inventaire des bases vide.")
    has_mariadb = any(engine(base) == "mariadb" for base in config["bases"])
    exclusions = config["inventory"]["exclusions"] if has_mariadb else {}
    if not isinstance(exclusions, dict) or any(
        not str(reason).strip() for reason in exclusions.values()
    ):
        raise Failure("configuration", "Chaque exclusion doit être motivée.")
    for base in config["bases"]:
        if engine(base) not in ("mariadb", "sqlite"):
            raise Failure("configuration", "Moteur de base non pris en charge.")
        for key in ("id", "database", "job"):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", base[key]):
                raise Failure("configuration", "Identifiant de base ou job invalide.")
        database_key = (engine(base), base["database"])
        if base["id"] in ids or database_key in databases or base["job"] in jobs:
            raise Failure("configuration", "Base, identifiant ou job dupliqué.")
        ids.add(base["id"])
        databases.add(database_key)
        jobs.add(base["job"])
        if base["folder"] != engine(base) + "/" + base["job"]:
            raise Failure(
                "configuration",
                "Dossier de dump différent du moteur et du job attendus.",
            )
        if engine(base) == "sqlite" and (
            not Path(base.get("sqlite_path", "")).is_absolute()
            or ".." in Path(base["sqlite_path"]).parts
            or "\n" in base["sqlite_path"]
        ):
            raise Failure("configuration", "Chemin SQLite absolu non ambigu requis.")
        if engine(base) == "mariadb" and base["database"] in exclusions:
            raise Failure("configuration", "Base attendue également exclue.")
        if (
            not croniter.is_valid(base["schedule"])
            or len(base["schedule"].split()) != 5
        ):
            raise Failure("configuration", "Expression cron à cinq champs requise.")
        ZoneInfo(base["timezone"])
        ZoneInfo(base.get("timestamp_timezone", base["timezone"]))
        if date(base["enabled_since"]) > now():
            raise Failure(
                "configuration",
                "Date d'activation future : contrôle de fraîcheur non activable.",
            )
        if base["completion"]["mode"] not in ("atomic-rename", "success-marker"):
            raise Failure("configuration", "Contrat de fin de dump non configuré.")
        if base["completion"]["mode"] == "atomic-rename" and not re.fullmatch(
            r"sha256:[a-f0-9]{64}", base["completion"].get("generator_image_id", "")
        ):
            raise Failure(
                "configuration", "Identifiant exact de l'image du générateur requis."
            )
        evidence = base["completion"].get("evidence", "")
        if len(evidence) < 20 or "REPLACE" in evidence:
            raise Failure("configuration", "Preuve du contrat de fin de dump requise.")
        if "stamp" not in re.compile(base["timestamp_regex"]).groupindex:
            raise Failure(
                "configuration", "Capture nommée stamp requise pour la date du dump."
            )
        if "/" in base["pattern"] or ".." in base["pattern"]:
            raise Failure("configuration", "Motif de fichier simple requis.")
        if int(base["minimum_sql_bytes"]) < 1:
            raise Failure("configuration", "Taille SQL minimale positive requise.")
        if (
            not isinstance(base.get("minimum_size_ratio", 0.5), (int, float))
            or not 0 <= base.get("minimum_size_ratio", 0.5) <= 1
        ):
            raise Failure("configuration", "Ratio de taille attendu entre 0 et 1.")
        if not base["required_tables"] or not base["assertions"]:
            raise Failure(
                "configuration", "Contrôles de schéma et de données requis par base."
            )
        for table in base["required_tables"]:
            if not re.fullmatch(r"[A-Za-z0-9_]+", table):
                raise Failure("configuration", "Nom de table de contrôle invalide.")
        for assertion in base["assertions"]:
            if (
                not re.match(r"^SELECT\s", assertion["sql"], re.I)
                or ";" in assertion["sql"]
            ):
                raise Failure("configuration", "Assertion SELECT unique requise.")
        if base.get("format", "gzip") not in ("gzip", "gpg-gzip"):
            raise Failure(
                "configuration", "Format non pris en charge (gzip ou gpg-gzip requis)."
            )
    if has_mariadb and not re.fullmatch(
        r"(?:[a-zA-Z0-9_.:/-]+@sha256:[a-f0-9]{64}|mariadb:\d+\.\d+(?:\.\d+)?)",
        config["restore_image"],
    ):
        raise Failure(
            "configuration", "Image MariaDB explicitement versionnée requise."
        )
    if any(engine(base) == "sqlite" for base in config["bases"]) and not re.fullmatch(
        r"sha256:[a-f0-9]{64}", config.get("sqlite_restore_image", "")
    ):
        raise Failure("configuration", "Identifiant immuable de l'image SQLite requis.")
    if not re.fullmatch(r"[1-9][0-9]*[mg]", config.get("restore_memory", "2g")):
        raise Failure("configuration", "Limite mémoire invalide.")


def slots(base, start, end):
    """Parcours UTC : aucun décalage silencieux ni oubli au passage été/hiver."""
    current = start.astimezone(UTC).replace(second=0, microsecond=0)
    if current < start:
        current += dt.timedelta(minutes=1)
    zone = ZoneInfo(base["timezone"])
    while current <= end:
        if croniter.match(
            base["schedule"], current.astimezone(zone).replace(tzinfo=None)
        ):
            yield current
        current += dt.timedelta(minutes=1)


def previous_slot(base, generated):
    current = generated.replace(second=0, microsecond=0)
    zone = ZoneInfo(base["timezone"])
    for _ in range(8 * 24 * 60):
        if croniter.match(
            base["schedule"], current.astimezone(zone).replace(tzinfo=None)
        ):
            return current
        current -= dt.timedelta(minutes=1)
    raise Failure("configuration", "Fréquence maximale prise en charge : hebdomadaire.")


def filename_date(base, name):
    match = re.fullmatch(base["timestamp_regex"], name)
    if not match:
        raise Failure("generation", "Nom de dump sans horodatage identifiable.")
    parsed = dt.datetime.strptime(match["stamp"], base["timestamp_format"])
    if parsed.tzinfo is None:
        zone = ZoneInfo(base.get("timestamp_timezone", base["timezone"]))
        first, second = (
            parsed.replace(tzinfo=zone, fold=0),
            parsed.replace(tzinfo=zone, fold=1),
        )
        if first.utcoffset() != second.utcoffset():
            raise Failure(
                "generation",
                "Horodatage local ambigu ou inexistant : utiliser UTC/offset.",
            )
        parsed = first
    # Restic et les manifestes expriment leurs dates à la seconde.
    return parsed.astimezone(UTC).replace(microsecond=0)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode):
        raise Failure("generation", "Dump régulier requis.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def check_gzip(path, minimum):
    size = 0
    try:
        with gzip.open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
    except (OSError, EOFError, zlib.error):
        raise Failure("corruption", "Archive gzip tronquée ou corrompue.") from None
    if size < minimum:
        raise Failure("corruption", "Volume SQL inférieur au minimum attendu.")
    return size


def retention_candidates(snapshots, protected, at):
    """30 jours complets + 12 mois calendaires + derniers états prouvés."""
    ordered = sorted(snapshots, key=lambda item: date(item["time"]), reverse=True)
    keep = set(protected)
    if ordered:
        keep.add(ordered[0]["id"])
    months = set()
    cutoff = at - dt.timedelta(days=30)
    month_index = at.year * 12 + at.month - 1
    for item in ordered:
        when = date(item["time"])
        index = when.year * 12 + when.month - 1
        if when >= cutoff:
            keep.add(item["id"])
        if 0 <= month_index - index < 12 and index not in months:
            keep.add(item["id"])
            months.add(index)
    return [item["id"] for item in ordered if item["id"] not in keep]
