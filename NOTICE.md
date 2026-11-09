# Source provenance

The controller and its regression tests were extracted as a clean source copy
from the Battle-Conquest infrastructure, including MariaDB and SQLite support:

- Source: https://github.com/Battle-Conquest/Battle-Conquest
- Commit: `8fb308caa3c0e126ee3405897b70caa45273a80e`

No Git history, application code, runtime state, database dumps, or personal
configuration was imported. Rayman223 authorized publication under the MIT
license. The standalone packaging adds generic configuration examples, Unraid
User Scripts, validation workflows, and installation/recovery documentation.

The historical Restic tags, repository suffix `mariadb-offsite-v1`, Docker
labels and internal container names retain their original `bc` prefix for
archive compatibility. They do not indicate a dependency on the game.

The Docker image installs or copies third-party components, including Restic,
rclone, Python, SQLite, the Docker CLI and the packages listed in
`deploy/offsite/requirements.txt`. Those components retain their own licenses.
