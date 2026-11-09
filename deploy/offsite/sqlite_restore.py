"""Import SQLite dans l'enfant isolé, sans dépendance au contrôleur ni secrets."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def check_database(path, checks, deadline):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("integrity")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("foreign keys")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        if not set(checks["required_tables"]).issubset(tables):
            raise ValueError("schema")
        for assertion in checks["assertions"]:
            cursor = connection.execute(assertion["sql"])
            rows = cursor.fetchmany(2)
            if len(rows) != 1 or len(rows[0]) != 1:
                raise ValueError("scalar")
            value = "NULL" if rows[0][0] is None else str(rows[0][0])
            if value != str(assertion["expected"]):
                raise ValueError("assertion")
    finally:
        connection.close()


def restore(checks, directory, stream):
    directory = Path(directory)
    path = directory / "restored.sqlite3"
    if path.exists():
        raise ValueError("fresh database required")
    deadline = time.monotonic() + checks["timeout"]
    # Le CLI consomme le dump en flux. --safe interdit notamment ATTACH,
    # .shell, .load et les fonctions d'accès aux fichiers ; aucune nonce.
    subprocess.run(
        ["sqlite3", "-init", "/dev/null", "-safe", "-batch", "-bail", str(path)],
        stdin=stream,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=checks["timeout"],
        check=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent", "TZ": "UTC"},
    )
    check_database(path, checks, deadline)


def main():
    os.umask(0o077)
    try:
        with Path(sys.argv[1]).open(encoding="utf-8") as handle:
            checks = json.load(handle)
        restore(checks, "/restore", sys.stdin.buffer)
        return 0
    except Exception:
        # Ni SQL, ni lignes restaurées, ni messages SQLite dans les logs.
        print("Échec du contrôle SQLite isolé.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
