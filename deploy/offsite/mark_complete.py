"""À appeler UNIQUEMENT depuis le hook de succès du producteur, après fermeture.

Ce programme n'atteste pas à lui seul le succès SQL. Son appel doit être placé
après le retour réussi du générateur, jamais dans un balayage des fichiers.
Autonome (bibliothèque standard) pour ne pas imposer les dépendances du contrôleur.
"""

import argparse
import datetime
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


def publish(path):
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
        raise ValueError("Dump régulier non vide requis.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.lstat()
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in keys):
        raise ValueError("Dump modifié pendant le hook.")
    marker = path.with_name(path.name + ".complete.json")
    data = {
        "filename": path.name,
        "bytes": before.st_size,
        "sha256": digest.hexdigest(),
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
    }
    fd, temp = tempfile.mkstemp(prefix=".complete-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, marker)
    finally:
        Path(temp).unlink(missing_ok=True)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    args = parser.parse_args()
    try:
        publish(args.file)
    except Exception:
        parser.exit(1, "Marqueur non publié : dump absent, vide ou modifié.\n")
