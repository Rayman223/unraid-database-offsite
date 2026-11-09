# Security

Never open a public issue containing credentials, OAuth tokens, webhook/ping
URLs, real SQL dumps, personal database rows or private runtime state. Use a
sanitized minimal reproduction and synthetic data. If GitHub private
vulnerability reporting is available on this repository, use it for sensitive
reports; otherwise ask the maintainer for a private reporting channel before
sharing details.

Runtime files belong outside the source checkout, in private directories. The
Git ignore rules prevent common mistakes; the Docker build context is an
allowlist of controller Python files and requirements. Neither mechanism can
identify every secret, so review all staged content before committing.

The validation workflow scans Git history with a pinned Gitleaks release.
For a local scan, install it outside the repository and redact output:

```bash
bash scripts/install-gitleaks.sh /tmp/offsite-gitleaks
/tmp/offsite-gitleaks/gitleaks dir --redact --no-banner .
/tmp/offsite-gitleaks/gitleaks git --redact --no-banner .
```

The helper currently supports Linux x86_64 and verifies the downloaded archive
against a fixed SHA-256 checksum. It does not upload source files to a scanning
service. Review suspected secrets privately, replace them with placeholders,
and rotate any actual credential that has been exposed before distributing a
cleaned history. Automated scanning reduces risk but cannot guarantee that an
arbitrary string is not a secret.

The controller has administrative access to Docker and reads private dumps.
Restore children receive no host mounts, source credentials, network or Docker
socket. Keep the host, dependencies, Docker daemon and private storage trusted.
Restic encryption protects archived contents; it cannot stop a Dropbox account
with deletion rights from deleting the repository.
