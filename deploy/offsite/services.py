"""Accès externes : erreurs expurgées et appels sans shell."""

import configparser
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pymysql
import yaml
from core import (
    TAG,
    UTC,
    Failure,
    artifact_id,
    date,
    engine,
    load,
    private_file,
    restoration_image,
    secret,
    sha256,
)


def execute(
    args, *, env=None, timeout=1800, stdin=None, stdout=None, category="transfert"
):
    """Ne jamais relayer stderr : il peut contenir une URL secrète ou du SQL."""
    try:
        process = subprocess.Popen(
            [str(arg) for arg in args],
            stdin=stdin,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError:
        raise Failure(category, "Commande requise indisponible.") from None
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException as error:
        # Inclut interruption : supprimer aussi le rclone enfant de Restic.
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise Failure(category, "Commande interrompue ou délai dépassé.") from None
    if process.returncode:
        raise Failure(category, f"Commande échouée (code {process.returncode}).")
    return output or b""


def ensure_dropbox(config):
    """Ne pas prendre un remote local portant le nom Dropbox pour une copie externe."""
    document = configparser.ConfigParser(interpolation=None)
    document.read(private_file(config["rclone_config"]), encoding="utf-8")
    remote = config["repository"].split(":", 2)[1]
    visited = set()
    for _ in range(8):
        if remote in visited or not document.has_section(remote):
            break
        visited.add(remote)
        section = document[remote]
        backend = section.get("type", "").strip()
        if backend == "dropbox":
            return
        if backend not in ("alias", "crypt"):
            break
        target = section.get("remote", "")
        if ":" not in target:
            break
        remote = target.split(":", 1)[0]
    raise Failure(
        "configuration",
        "Le dépôt doit aboutir à un remote Dropbox, directement ou via alias/crypt.",
    )


class Repository:
    def __init__(self, config):
        self.c = config
        ensure_dropbox(config)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "HOME", "SSL_CERT_FILE", "SSL_CERT_DIR")
        }
        self.env.update(
            {
                "RESTIC_REPOSITORY": config["repository"],
                "RESTIC_PASSWORD_FILE": str(private_file(config["password_file"])),
                "RCLONE_CONFIG": str(private_file(config["rclone_config"])),
                "RESTIC_CACHE_DIR": str(Path(config["state_dir"]) / "restic-cache"),
                "TZ": "UTC",
            }
        )
        secret(config["password_file"])

    def run(self, *args, stdout=None):
        return execute(
            ["restic", "--retry-lock", "30s", *args],
            env=self.env,
            timeout=self.c.get("command_timeout", 1800),
            stdout=stdout,
        )

    def identity(self):
        return json.loads(self.run("cat", "config"))["id"]

    def check_identity(self):
        if (
            self.c["repository_id"] == "SET_AFTER_INIT"
            or self.identity() != self.c["repository_id"]
        ):
            raise Failure(
                "configuration", "Identité du dépôt différente de celle configurée."
            )

    def snapshots(self, base=None, kind="dump"):
        args = ["snapshots", "--json"]
        if base is not None:
            args += ["--tag", f"{TAG},base={base['id']},kind={kind}"]
        return json.loads(self.run(*args)) or []

    def create(self, base, folder, generated, artifact, kind="dump"):
        output = self.run(
            "backup",
            "--json",
            "--host",
            "mariadb-offsite",
            "--time",
            generated.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "--tag",
            TAG,
            "--tag",
            f"base={base['id']}",
            "--tag",
            f"kind={kind}",
            "--tag",
            f"artifact={artifact}",
            "--",
            str(folder),
        )
        summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
        result = next(
            (
                item
                for item in reversed(summaries)
                if item.get("message_type") == "summary"
            ),
            None,
        )
        if not result or not result.get("snapshot_id"):
            raise Failure("transfert", "Instantané Restic non confirmé.")
        # L'identifiant court retourné doit être résolu en identifiant complet.
        candidates = [
            item
            for item in self.snapshots(base, kind)
            if item["id"].startswith(result["snapshot_id"])
        ]
        if len(candidates) != 1:
            raise Failure("transfert", "Identifiant Restic ambigu.")
        return candidates[0]

    def fetch(self, snapshot, name, destination):
        if name not in ("manifest.json", "dump", "restore-report.json"):
            raise Failure("configuration", "Nom d'artefact refusé.")
        if len(snapshot["paths"]) != 1 or not snapshot["paths"][0].startswith("/"):
            raise Failure("corruption", "Chemin d'instantané invalide.")
        with Path(destination).open("wb") as output:
            self.run(
                "dump",
                snapshot["id"],
                snapshot["paths"][0].rstrip("/") + "/" + name,
                stdout=output,
            )

    def verify(self, base, snapshot, folder):
        folder = Path(folder)
        self.fetch(snapshot, "manifest.json", folder / "manifest.json")
        manifest = load(folder / "manifest.json")
        if (
            manifest.get("version") != 1
            or manifest.get("base") != base["id"]
            or manifest.get("database") != base["database"]
            or manifest.get("engine", "mariadb") != engine(base)
            or manifest.get("format") not in ("gzip", "gpg-gzip")
        ):
            raise Failure(
                "corruption", "Manifeste externe incompatible avec la base attendue."
            )
        generated = date(manifest["generated_at"])
        slot = date(manifest["slot"])
        if (
            generated != date(snapshot["time"])
            or slot > generated
            or slot.second != 0
            or slot.microsecond != 0
            or date(manifest["completed_at"]) < generated
        ):
            raise Failure("corruption", "Échéance ou date du manifeste incohérente.")
        artifact = artifact_id(base, manifest["filename"], manifest["sha256"])
        if (
            manifest["artifact"] != artifact
            or "artifact=" + artifact not in snapshot.get("tags", [])
            or "kind=dump" not in snapshot.get("tags", [])
            or "base=" + base["id"] not in snapshot.get("tags", [])
            or TAG not in snapshot.get("tags", [])
        ):
            raise Failure("corruption", "Identité de dump incohérente.")
        self.fetch(snapshot, "dump", folder / "dump")
        dump = folder / "dump"
        if (
            dump.stat().st_size != manifest["bytes"]
            or sha256(dump) != manifest["sha256"]
        ):
            raise Failure(
                "corruption", "Empreinte ou taille de la copie externe incorrecte."
            )
        return manifest


def discover(config):
    settings = config["inventory"]
    connection = None
    try:
        connection = pymysql.connect(
            host=settings["host"],
            port=settings["port"],
            user=settings["user"],
            password=secret(settings["password_file"]),
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
            autocommit=True,
        )
        with connection.cursor() as cursor:
            cursor.execute("SHOW GRANTS FOR CURRENT_USER")
            grants = "\n".join(str(row[0]) for row in cursor.fetchall())
            # Sans ce privilège global, SHOW DATABASES masque les bases oubliées.
            if not any(
                " ON *.* " in line
                and ("SHOW DATABASES" in line or "ALL PRIVILEGES" in line)
                for line in grants.splitlines()
            ):
                raise Failure(
                    "configuration", "Compte de découverte sans SHOW DATABASES global."
                )
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
            if (
                not version.startswith(settings["version_prefix"])
                or "MariaDB" not in version
            ):
                raise Failure(
                    "configuration",
                    "Version MariaDB source différente de celle prévue.",
                )
            cursor.execute("SHOW DATABASES")
            return {row[0] for row in cursor.fetchall()}
    except pymysql.MySQLError:
        raise Failure("configuration", "Inventaire MariaDB indisponible.") from None
    finally:
        if connection is not None:
            connection.close()


def compare_inventory(config, actual):
    expected = {
        base["database"] for base in config["bases"] if engine(base) == "mariadb"
    }
    excluded = set(config.get("inventory", {}).get("exclusions", {}))
    errors = []
    for database in sorted(actual - expected - excluded):
        # Ne pas copier de noms externes arbitraires dans Discord/logs.
        safe = (
            database
            if all(char.isalnum() or char in "_-" for char in database)
            else "<nom non standard>"
        )
        errors.append(f"Base non couverte : {safe}.")
    for database in sorted(expected - actual):
        errors.append(f"Base attendue absente : {database}.")
    return errors


def check_jobs(config):
    with Path(config["jobs_file"]).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    jobs = document.get("jobs", [])
    if not isinstance(jobs, list):
        raise Failure("configuration", "Liste jobs de database-backup illisible.")
    # L'absence de TZ doit être corrigée dans le générateur, pas devinée ici.
    container = config.get("backup_container", "database-backup")
    inspected = json.loads(
        execute(["docker", "inspect", container], category="configuration", timeout=30)
    )[0]
    environment = dict(
        item.split("=", 1) for item in inspected["Config"].get("Env", []) if "=" in item
    )
    timezone = environment.get("TZ")
    if not timezone:
        raise Failure("configuration", "TZ explicite requis sur database-backup.")
    errors = {}
    for base in config["bases"]:
        if (
            base["completion"]["mode"] == "atomic-rename"
            and base["completion"].get("generator_image_id") != inspected["Image"]
        ):
            errors[base["id"]] = (
                "Image du générateur différente du contrat de publication atomique."
            )
            continue
        matches = [job for job in jobs if job.get("name") == base["job"]]
        if len(matches) != 1:
            errors[base["id"]] = "Job database-backup absent ou dupliqué."
            continue
        job = matches[0]
        if engine(base) == "sqlite":
            target_matches = job.get("path") == base["sqlite_path"]
        else:
            target_matches = (
                job.get("database") == base["database"]
                and job.get("host") == config["inventory"]["host"]
                and int(job.get("port", 3306)) == config["inventory"]["port"]
            )
        if (
            not target_matches
            or job.get("schedule") != base["schedule"]
            or job.get("type") != engine(base)
            or timezone != base["timezone"]
        ):
            errors[base["id"]] = (
                "Job différent de l'inventaire (base, serveur, horaire ou TZ)."
            )
    return errors


def request(url, *, content=None):
    body = json.dumps(content).encode() if content is not None else b""
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )

    # Pas de redirection susceptible d'envoyer le secret vers un autre domaine.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise OSError()
    except Exception:
        raise Failure(
            "notification", "Notification non remise ; prochain passage requis."
        ) from None


def notification_url(config, key):
    url = secret(config["notifications"][key])
    parsed = urlsplit(url)
    if key == "discord_url_file":
        if (
            parsed.scheme != "https"
            or parsed.netloc != "discord.com"
            or not parsed.path.startswith("/api/webhooks/")
        ):
            raise Failure("configuration", "Webhook Discord HTTPS officiel requis.")
    elif (
        parsed.scheme != "https"
        or parsed.netloc != "hc-ping.com"
        or len(parsed.path) < 10
        or parsed.query
        or parsed.fragment
    ):
        raise Failure("configuration", "URL Healthchecks HTTPS hc-ping.com requise.")
    return url


def ping(config, key, success):
    url = notification_url(config, key)
    request(url.rstrip("/") + ("" if success else "/fail"))


def notification_messages(issues):
    if not issues:
        return ["Sauvegardes bases de données — retour à la normale."]
    repeated = {}
    details = []
    for issue in issues:
        match = re.fullmatch(r"(.+pour l'échéance )(\S+)\.", issue)
        if match:
            repeated.setdefault(match[1], []).append(match[2])
        else:
            details.append(issue)
    for prefix, dates in repeated.items():
        if len(dates) == 1:
            details.append(prefix + dates[0] + ".")
        else:
            details.append(
                f"{prefix}{min(dates)} … {max(dates)} ({len(dates)} échéances)."
            )
    body = "\n".join(sorted(details))
    chunks = [body[start : start + 1800] for start in range(0, len(body), 1800)]
    return [
        f"Sauvegardes bases de données — anomalies ({index + 1}/{len(chunks)}) :\n{chunk}"
        for index, chunk in enumerate(chunks)
    ]


def notify(config, state, issues):
    signature = json.dumps(issues, sort_keys=True)
    previous = state.get("notified")
    progress = state.get("notification_progress", {})
    if previous == signature and not progress.get("next", 0):
        return
    # Un premier passage sain est silencieux. Les reprises ne le sont pas.
    if previous is None and not issues and not progress.get("next", 0):
        state["notified"] = signature
        return
    url = notification_url(config, "discord_url_file")
    messages = notification_messages(issues)
    start = progress.get("next", 0) if progress.get("signature") == signature else 0
    for index in range(start, len(messages)):
        request(
            url, content={"content": messages[index], "allowed_mentions": {"parse": []}}
        )
        # Le watcher persiste cette progression même si le prochain POST échoue.
        state["notification_progress"] = {"signature": signature, "next": index + 1}
    state["notified"] = signature
    state.pop("notification_progress", None)


class RestoreSandbox:
    """Le SQL importé ne voit aucun réseau, secret source, bind mount ou socket."""

    def __init__(self, config):
        self.c = config

    def cleanup_stale(self):
        output = execute(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                "label=bc.offsite=restore",
                "--filter",
                "label=bc.offsite.repository=" + self.c["repository_id"],
                "--format",
                "{{.Names}}",
            ],
            category="restauration",
            timeout=30,
        )
        for name in output.decode().splitlines():
            if (
                not name.startswith("bc-offsite-restore-")
                or not name.removeprefix("bc-offsite-restore-").isalnum()
            ):
                raise Failure("restauration", "Conteneur de contrôle non identifiable.")
            settings = json.loads(
                execute(
                    ["docker", "inspect", name], category="restauration", timeout=30
                )
            )[0]
            labels = settings["Config"]["Labels"]
            if (
                labels.get("bc.offsite") != "restore"
                or labels.get("bc.offsite.repository") != self.c["repository_id"]
            ):
                raise Failure("restauration", "Conteneur étranger : nettoyage refusé.")
            if time.time() > float(labels["bc.offsite.deadline"]):
                execute(
                    ["docker", "rm", "--force", name],
                    category="restauration",
                    timeout=60,
                )

    def run_sqlite(self, base, sql):
        name = "bc-offsite-restore-" + secrets.token_hex(8)
        timeout = self.c.get("restore_timeout", 7200)
        try:
            execute(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--pull=never",
                    "--name",
                    name,
                    "--label",
                    "bc.offsite=restore",
                    "--label",
                    "bc.offsite.repository=" + self.c["repository_id"],
                    "--label",
                    "bc.offsite.deadline=" + str(time.time() + timeout + 300),
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--user",
                    "65534:65534",
                    "--pids-limit",
                    "32",
                    "--memory",
                    self.c.get("restore_memory", "2g"),
                    "--cpus",
                    "1",
                    "--tmpfs",
                    "/restore:rw,nosuid,nodev,noexec,mode=0700,uid=65534,gid=65534,size="
                    + self.c.get("restore_memory", "2g"),
                    "--workdir",
                    "/restore",
                    "--env",
                    "HOME=/nonexistent",
                    "--entrypoint",
                    "python3",
                    restoration_image(self.c, base),
                    "-c",
                    f"import time; time.sleep({timeout + 300})",
                ],
                category="restauration",
                timeout=60,
            )
            # Seuls les contrôles de cette base traversent stdin. Aucun secret,
            # chemin source, montage hôte ou socket n'est transmis à l'enfant.
            checks = {
                "required_tables": base["required_tables"],
                "assertions": base["assertions"],
                "timeout": timeout,
            }
            with tempfile.TemporaryFile() as handle:
                handle.write(json.dumps(checks).encode())
                handle.seek(0)
                execute(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "sh",
                        "-c",
                        "umask 077; cat > /restore/checks.json",
                    ],
                    stdin=handle,
                    category="restauration",
                    timeout=30,
                )
            with Path(sql).open("rb") as handle:
                execute(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "python3",
                        "/opt/offsite/sqlite_restore.py",
                        "/restore/checks.json",
                    ],
                    stdin=handle,
                    category="restauration",
                    timeout=timeout,
                )
        finally:
            execute(
                ["docker", "rm", "--force", name], category="restauration", timeout=60
            )

    def run(self, base, sql):
        if engine(base) == "sqlite":
            return self.run_sqlite(base, sql)
        name = "bc-offsite-restore-" + secrets.token_hex(8)
        timeout = self.c.get("restore_timeout", 7200)
        password = secrets.token_urlsafe(32)
        credential = f"[client]\nuser=root\npassword={password}\nprotocol=socket\n"
        deadline_epoch = (
            time.time()
            + timeout
            + 300
            + 30 * (len(base["required_tables"]) + len(base["assertions"]) + 4)
        )
        # Le secret éphémère est transmis par stdin et reste dans le conteneur.
        try:
            execute(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--pull=never",
                    "--name",
                    name,
                    "--label",
                    "bc.offsite=restore",
                    "--network",
                    "none",
                    "--label",
                    "bc.offsite.repository=" + self.c["repository_id"],
                    "--label",
                    "bc.offsite.deadline=" + str(deadline_epoch),
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "SETUID",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--pids-limit",
                    "256",
                    "--memory",
                    self.c.get("restore_memory", "2g"),
                    "--cpus",
                    "1",
                    "--tmpfs",
                    "/var/lib/mysql:rw,nosuid,nodev,size="
                    + self.c.get("restore_memory", "2g"),
                    "--tmpfs",
                    "/run/mysqld:rw,nosuid,nodev,size=16m",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=64m",
                    "--env",
                    "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1",
                    self.c["restore_image"],
                    "--skip-networking",
                    "--event-scheduler=DISABLED",
                    # MariaDB n'interprète pas NULL comme MySQL : utiliser le dossier
                    # vide de l'image, sur la racine montée en lecture seule.
                    "--local-infile=0",
                    "--secure-file-priv=/docker-entrypoint-initdb.d",
                ],
                category="restauration",
                timeout=60,
            )
            deadline = time.monotonic() + min(timeout, 180)
            while True:
                try:
                    execute(
                        [
                            "docker",
                            "exec",
                            name,
                            "mariadb-admin",
                            "--protocol=socket",
                            "-uroot",
                            "ping",
                        ],
                        category="restauration",
                        timeout=10,
                    )
                    break
                except Failure:
                    if time.monotonic() >= deadline:
                        raise Failure(
                            "restauration", "MariaDB isolée non prête."
                        ) from None
                    time.sleep(2)
            # La phase d'initialisation de l'image peut encore utiliser son serveur
            # temporaire. Attendre l'entrypoint final avant tout import.
            while True:
                output = (
                    execute(
                        ["docker", "exec", name, "sh", "-c", "cat /proc/1/comm"],
                        category="restauration",
                        timeout=10,
                    )
                    .decode()
                    .strip()
                )
                if output in ("mariadbd", "mysqld"):
                    break
                if time.monotonic() >= deadline:
                    raise Failure(
                        "restauration", "Initialisation MariaDB isolée inachevée."
                    )
                time.sleep(2)
            setup = (
                f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{password}';\n"
                f"CREATE DATABASE `{base['database']}`;\n"
            ).encode()
            # TemporaryFile permet un stdin volumineux sans communiquer le SQL
            # dans argv ni conserver ses erreurs dans les journaux.
            with tempfile.TemporaryFile() as handle:
                handle.write(setup)
                handle.seek(0)
                execute(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "mariadb",
                        "--protocol=socket",
                        "-uroot",
                    ],
                    stdin=handle,
                    category="restauration",
                    timeout=30,
                )
            with tempfile.TemporaryFile() as handle:
                handle.write(credential.encode())
                handle.seek(0)
                execute(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "sh",
                        "-c",
                        "umask 077; cat > /run/mysqld/check.cnf",
                    ],
                    stdin=handle,
                    category="restauration",
                    timeout=30,
                )
            client = [
                "docker",
                "exec",
                "-i",
                name,
                "mariadb",
                "--defaults-extra-file=/run/mysqld/check.cnf",
                "--binary-mode",
                "--batch",
                "--skip-column-names",
                base["database"],
            ]
            import_and_check(self.c, base, sql, client)
        finally:
            # Aucun autre conteneur ne peut être ciblé par ce nom aléatoire.
            execute(
                ["docker", "rm", "--force", name], category="restauration", timeout=60
            )


def import_and_check(config, base, sql, client):
    """Importer sans --force, puis contrôler version, isolation et données."""
    version = (
        execute(
            client + ["--execute", "SELECT VERSION()"],
            category="restauration",
            timeout=30,
        )
        .decode()
        .strip()
    )
    if (
        not version.startswith(config["inventory"]["version_prefix"])
        or "MariaDB" not in version
    ):
        raise Failure(
            "restauration",
            "Version de restauration incompatible avec la source configurée.",
        )
    flags = (
        execute(
            client + ["--execute", "SELECT @@event_scheduler, @@skip_networking"],
            category="restauration",
            timeout=30,
        )
        .decode()
        .strip()
    )
    if flags != "DISABLED\t1":
        raise Failure("restauration", "Événements ou réseau insuffisamment isolés.")
    with Path(sql).open("rb") as handle:
        execute(
            client,
            stdin=handle,
            category="restauration",
            timeout=config.get("restore_timeout", 7200),
        )
    # Le dump pourrait changer le compte root : les contrôles échoueraient
    # alors explicitement, sans recours aux accès PRD ou QUA.
    for table in base["required_tables"]:
        execute(
            client + ["--execute", f"SELECT 1 FROM `{table}` LIMIT 0"],
            category="restauration",
            timeout=30,
        )
    for assertion in base["assertions"]:
        value = (
            execute(
                client + ["--execute", assertion["sql"]],
                category="restauration",
                timeout=30,
            )
            .decode()
            .strip()
        )
        if value != str(assertion["expected"]):
            raise Failure("restauration", "Assertion de données non satisfaite.")
