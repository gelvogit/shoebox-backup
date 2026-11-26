# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org/); while the version is 0.x,
a minor release can change the config format, and says so here.

## 0.3.0 - 2026-11-26

### Changed (config)

- **The XML is checked strictly.** An element Shoebox does not know is an error, with a hint, instead of
  being silently ignored. The top element must be `<shoebox>`.
- **Email settings come only from `.env`** (`SMTP_*`, `EMAIL_*`). The `<email_*>` XML elements are no
  longer accepted. `SMTP_SECURE` must be `tls`, `ssl` or empty.
- **Yes/no values** (`<folder_split>`, `SMTP_AUTH`) accept `yes`/`no`, `true`/`false`, `y`/`n` or `1`/`0`;
  anything else is an error. `<folder_split>` can be left out (it means no).
- **A `<folder>` without a valid `<folder_label>` or a `<folder_path>` is an error**, not a skipped entry.

### Added

- `-c` as a short form of `--config`, and `/etc/shoebox/backupconfig.xml` as the last place Shoebox looks
  for its config.
- `CHANGELOG.md`, `SECURITY.md`, ruff lint and format checks in CI.
- The README now covers requirements, folders, retention, email, file naming, restore (including Docker
  databases and restoring without Shoebox), monitoring, security, troubleshooting and upgrades.

### Fixed

- Log, lock and dump-pipe files are closed properly.

### Upgrading from 0.2.0

Move any `<email_...>` elements from the XML to `.env` (see `.env.example`), then run `shoebox.py check`.

## 0.2.0 - 2026-11-26

### Changed (config)

- **Databases are listed as `<database_server>` entries inside `<databases>`.** Each has its own label,
  `<host>` or `<docker_container>`, `<username>`, password source (`<password_env>` from `.env`,
  `<password_container_env>` from inside the container, or none), dump options and database query. The
  old `<mysql>`, `<mysql_*>` and `<docker_mysql>` elements, and `MYSQL_*` in `.env`, are no longer used.

## 0.1.0 - 2026-11-26

First release: folders as `.tgz`, verified MySQL/MariaDB dumps as `.sql.gz`, verified uploads to Dropbox,
per-run manifest, retention, HTML email report, status JSON, `auth`, `check`, `list` and `restore`.
