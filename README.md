# Shoebox

[![ci](https://github.com/gelvogit/shoebox-backup/actions/workflows/ci.yml/badge.svg)](https://github.com/gelvogit/shoebox-backup/actions/workflows/ci.yml)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Plain, verified nightly backups of folders and MySQL/MariaDB databases to Dropbox.**

Like a shoebox of photos: no key, no special tool, just open the lid. Folders become `.tgz` files and
databases become `.sql.gz` files that open with `tar` and `zcat` on any machine, years from now, without
Shoebox. Use it next to a "real" backup tool (restic, Borg, ...) as the copy you can always read, or on its
own for a small server.

- **One file, no dependencies.** `shoebox.py` needs only Python 3.8+ and its standard library. No pip.
- **Any MySQL/MariaDB server.** On this machine, on another host, or in a Docker container (dumped inside the
  container). Each server has its own login, dump options and choice of databases; nothing is assumed.
- **Verified dumps.** A database dump counts only if `mysqldump` exits 0, the file ends with
  `-- Dump completed`, and it has one `CREATE TABLE` per table in the database. `--force` is never used.
- **Verified uploads.** Every file is checked against Dropbox's `content_hash` after upload. Big files go up in
  chunks, with retries on rate limits and network errors.
- **A manifest per run** with every file's size and SHA-256, so a copy can be checked without Shoebox.
- **Retention.** Keep the newest run of each of the last N days, weeks and months in Dropbox, and N days of
  local copies. Dropbox is never thinned out after a run with errors.
- **Reports.** An HTML email after every run, a status JSON for monitoring, and an exit code equal to the
  number of errors.
- **No secrets in the config or on a command line.** Passwords live in a root-only `.env` and reach the
  database tools through the environment, never through `ps`-visible arguments.
- **Easy Dropbox login** over SSH: open a link, click Allow, paste the code. No web server, no app secret.

## Contents

- [Is Shoebox right for you?](#is-shoebox-right-for-you)
- [Requirements](#requirements)
- [Install](#install)
- [Connect Dropbox](#connect-dropbox)
- [Configure](#configure): [general](#general-settings), [folders](#folders), [databases](#databases),
  [retention](#retention), [email report](#email-report)
- [First run and scheduling](#first-run-and-scheduling)
- [Commands](#commands)
- [What a backup looks like](#what-a-backup-looks-like): [layout](#layout), [file names](#file-names),
  [manifest](#manifestjson)
- [Restore](#restore)
- [Monitoring](#monitoring)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Upgrade and uninstall](#upgrade-and-uninstall)
- [Development](#development)

## Is Shoebox right for you?

Good fits:

- **A single Linux server or VPS** with websites, config and a few databases, where you want a nightly copy
  off the machine and an email that says it worked.
- **A Docker host** running MySQL or MariaDB containers: each container is dumped with its own credentials,
  without installing database tools on the host or opening ports.
- **A second, readable copy next to an encrypted backup tool.** restic and Borg are excellent, but you need
  the tool and the key to get anything back. Shoebox's copy opens with `tar` and `zcat`.

Not a good fit:

- **Data that must be encrypted at rest.** Shoebox does not encrypt. Anyone who can open the Dropbox app
  folder can read the backups (see [Security](#security)).
- **Very large or fast-changing data.** Every run is a full copy: nothing is deduplicated or incremental. A
  few GB a night is comfortable; hundreds of GB is a job for restic or Borg.
- **Other storage or databases.** Dropbox only; MySQL and MariaDB only (no PostgreSQL yet). Linux only.

## Requirements

- **Linux** with **Python 3.8 or newer** (`python3 --version`).
- **GNU tar** (the `tar` on every mainstream distribution; BusyBox and BSD tar will not work).
- **For databases on this machine or another host:** the `mysql` (or `mariadb`) client and `mysqldump`
  (or `mariadb-dump`), from your distribution's `mysql-client` or `mariadb-client` package.
- **For databases in Docker:** the `docker` command on the host. The dump runs inside the container, which
  must have the client tools; the official `mysql` and `mariadb` images do.
- **Disk space** in the local folder for about `local_keep_days + 1` runs. Each run is written there in full
  before it is uploaded.
- **A Dropbox account** with room for your retention (roughly the run size times the number of runs kept).
- **Outbound HTTPS** to `api.dropboxapi.com` and `content.dropboxapi.com`, and SMTP to your mail server for
  the report.
- **Run it as root**, or as a user that can read everything it backs up and run `docker`.

## Install

```sh
sudo git clone https://github.com/gelvogit/shoebox-backup.git /opt/shoebox
sudo ln -s /opt/shoebox/shoebox.py /usr/local/bin/shoebox

sudo install -d -m 700 /etc/shoebox
sudo cp /opt/shoebox/backupconfig.example.xml /etc/shoebox/backupconfig.xml
sudo install -m 600 /opt/shoebox/.env.example /etc/shoebox/.env
```

Shoebox looks for its config in this order: `--config FILE` (or `-c FILE`), the `SHOEBOX_CONFIG`
environment variable, `backupconfig.xml` next to `shoebox.py`, then `/etc/shoebox/backupconfig.xml`. The
`.env` is read from the same folder as the config, unless you pass `--env FILE`.

The examples below assume the `/usr/local/bin/shoebox` link and a config in `/etc/shoebox/`. Commands that
read backups, databases or `/etc/shoebox` need root, so run them with `sudo` or as root.

## Connect Dropbox

A one-time setup, about five minutes. You need a web browser signed in to the Dropbox account the backups
should go to, and a shell on the server.

1. **Create the app.** Go to <https://www.dropbox.com/developers/apps> and click **Create app**:
   - API: **Scoped access**
   - Access: **App folder** (Shoebox can then only see its own folder, `Apps/<app name>/`, never the rest
     of your Dropbox)
   - Name: anything unique, e.g. `shoebox-myserver`. This becomes the folder name under `Apps/`.
2. **Set the permissions.** On the app's **Permissions** tab, tick `files.metadata.read`,
   `files.metadata.write`, `files.content.read` and `files.content.write`, then click **Submit** at the
   bottom. Do this before step 4: a login only gets the permissions the app had when you approved it.
3. **Copy the App key.** It is on the app's **Settings** tab. Put it in `/etc/shoebox/.env` as
   `DROPBOX_APP_KEY=...`, or paste it when step 4 asks. You do not need the App secret, a redirect URI or a
   "generated access token". The app can stay in **Development** status: that is enough for your own account.
4. **Log in from the server:**

   ```sh
   shoebox auth
   ```

   Open the link it prints (on any device), sign in to the right Dropbox account, click **Allow**, and
   paste the access code Dropbox shows. The code is not echoed. Shoebox saves a long-lived refresh token to
   `.env` (mode 600) and never prints it, then shows which account it connected to.
5. **Check it:** `shoebox check` should show `ok      Dropbox <account name>`.

If you later change the app's permissions, run `auth` again. To disconnect a server, remove the app under
<https://www.dropbox.com/account/connected_apps> (or delete the app) and clear `DROPBOX_REFRESH_TOKEN`.

## Configure

Two files, two jobs:

| File | What it holds | Secret? |
|---|---|---|
| `backupconfig.xml` | What to back up, how to reach the databases, how long to keep it | No: safe to keep in git |
| `.env` | The Dropbox login, the report email settings, database passwords | Yes: root-only, never in git |

[`backupconfig.example.xml`](backupconfig.example.xml) and [`.env.example`](.env.example) show every
option with comments. Shoebox refuses XML elements it does not know, so a typo stops the run with an error
instead of silently switching an option off. Yes/no values accept `yes`/`no`, `true`/`false`, `y`/`n` or
`1`/`0`.

After any change, run `shoebox check`.

### General settings

Inside `<config>`:

| Element | Default | Meaning |
|---|---|---|
| `archive_label` | required | A name for this server, used in every file name and as the Dropbox folder. Letters, digits, `_ . -` |
| `local_folder` | `/var/backups/shoebox` | Where each run is written before upload (one sub-folder per run) |
| `local_keep_days` | `7` | Local runs older than this are deleted after each run |
| `log_file` | `<local_folder>/shoebox.log` | Appended to on every run |
| `status_file` | `<local_folder>/status.json` | The last run's result, for [monitoring](#monitoring) |
| `dropbox_folder` | `archive_label` | Folder inside the Dropbox app folder |
| `dropbox_chunksize` | `33554432` (32 MiB) | Upload chunk size in bytes; rounded to 4 MiB steps, max 148 MiB |
| `tar_exe` | `tar` | The GNU tar to use |
| `retention` | keep everything | See [Retention](#retention) |

### Folders

Each `<folder>` in `<folder_list>` becomes one archive (or one per sub-folder when split):

```xml
<folder_list>
  <folder>
    <folder_label>WWW</folder_label>
    <folder_path>/var/www/html</folder_path>
    <folder_split>yes</folder_split>          <!-- one archive per site -->
    <folder_split_exclude>
      <exclude>old-site</exclude>             <!-- a sub-folder name to skip -->
    </folder_split_exclude>
  </folder>
  <folder>
    <folder_label>OPT</folder_label>
    <folder_path>/opt</folder_path>
    <folder_exclude>opt/big-thing</folder_exclude>
    <folder_exclude>*.log</folder_exclude>
  </folder>
</folder_list>
```

- `folder_label` (required) names the archive. Letters, digits, `_ . -`.
- `folder_path` (required) is the folder to archive. A missing folder is an error in the report.
- `folder_split` (default `no`): one archive per sub-folder, labelled `<folder_label>-<sub-folder>`, plus a
  `<folder_label>-_top` archive for the files sitting directly in the folder. Handy for `/var/www` or
  `/home`, where each site or user is then its own small file to restore. New sub-folders are picked up
  automatically.
- `folder_split_exclude`: sub-folder names (exact, not patterns) that a split skips.
- `folder_exclude` (repeatable): a GNU tar `--exclude` pattern. Archive paths have no leading `/`, so write
  `opt/big-thing`, not `/opt/big-thing`. Patterns like `*.log` match anywhere. Exclude anything that holds
  secrets you do not want in Dropbox, such as the folder with Shoebox's own `.env` if it is not in `/etc`.

Files that change while being archived (logs, for example) give a warning, not an error: the archive is still
kept.

### Databases

List each MySQL/MariaDB server in `<databases>`, one `<database_server>` each. There is no limit on the
number of servers, and each one can hold any number of databases.

```xml
<databases>
  <database_server>                       <!-- this machine, root over the socket -->
    <label>host</label>
    <host>localhost</host>
    <username>root</username>
  </database_server>
  <database_server>                       <!-- a Docker container, using its own root password -->
    <label>shop</label>
    <docker_container>shop-db</docker_container>
    <username>root</username>
    <password_container_env>MARIADB_ROOT_PASSWORD</password_container_env>
  </database_server>
  <database_server>                       <!-- another server, password from .env -->
    <label>crm</label>
    <host>db.internal.example</host>
    <port>3306</port>
    <username>backup</username>
    <password_env>CRM_DB_PASSWORD</password_env>
    <mysql_query>SELECT SCHEMA_NAME FROM SCHEMATA WHERE SCHEMA_NAME IN ('crm')</mysql_query>
  </database_server>
</databases>
```

| Element | Meaning |
|---|---|
| `label` | Required. Your name for the server, used in file names, reports and `run --only`. Letters, digits, `_ . -`; unique |
| `host` **or** `docker_container` | Exactly one. `localhost` means this machine over its socket. A container is dumped inside itself with `docker exec`, so the host needs only `docker` |
| `port` | Optional, with `host` only |
| `username` | Required |
| `password_env` | The name of a variable in `.env` that holds the password |
| `password_container_env` | Docker only: the name of a variable inside the container that holds the password (for example the image's `MARIADB_ROOT_PASSWORD` or `MYSQL_ROOT_PASSWORD`). The password never leaves the container |
| *(neither)* | No password: socket login as root, or a `~/.my.cnf` |
| `mysql_exe` | Optional dump program and options. Default: `mysqldump` (in a container, `mariadb-dump` or `mysqldump`, whichever it has) |
| `mysql_query` | Optional SQL that picks the databases. It runs in `information_schema` and the first column is the name. Default: every database except `information_schema`, `performance_schema`, `mysql` and `sys` |

Rules worth knowing:

- Passwords never go in the XML: a `<password>` element is refused. They reach the client through the
  `MYSQL_PWD` environment variable, never the command line.
- Shoebox always adds `--single-transaction --routines --triggers --events --hex-blob
  --default-character-set=utf8mb4` and drops `--force`, whatever `mysql_exe` says.
  `--single-transaction` gives a consistent dump of InnoDB tables without locking them; MyISAM tables are
  not locked, so they can change during the dump.
- If one server cannot be reached, or its `.env` password is missing, that server fails in the report and
  the rest of the backup still runs.
- A dedicated backup user needs at least `SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT` and `LOCK TABLES`, plus
  `PROCESS` on MySQL 8, on the databases it dumps:

  ```sql
  CREATE USER 'backup'@'%' IDENTIFIED BY 'a-long-random-password';
  GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, PROCESS ON *.* TO 'backup'@'%';
  ```

### Retention

Local copies are deleted when they are older than `local_keep_days`. Dropbox keeps everything unless you
add `<retention>` inside `<config>`:

```xml
<retention>
  <daily>14</daily>
  <weekly>8</weekly>
  <monthly>12</monthly>
</retention>
```

This keeps the newest run of each of the last 14 days, the newest of each of the last 8 weeks (ISO weeks,
Monday to Sunday), and the newest of each of the last 12 months: about 30 runs covering a year, with daily
detail for the last two weeks. A run that is the newest of its day, week and month counts once. The newest
run is always kept, and nothing is deleted from Dropbox after a run with errors. Set a number to `0` to skip
that tier.

### Email report

Set these in `.env`. The report is sent after every run, good or bad.

| Variable | Example | Meaning |
|---|---|---|
| `SMTP_HOST` | `smtp.example.com` | Mail server. Leave empty to send no email (the run then has a warning) |
| `SMTP_PORT` | `587` | Default 587 |
| `SMTP_SECURE` | `tls` | `tls` (STARTTLS, usually port 587), `ssl` (usually port 465) or empty for none |
| `SMTP_AUTH` | `yes` | `no` for a relay that needs no login |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | | The login |
| `EMAIL_TO` | `you@example.com` | Recipient |
| `EMAIL_FROM`, `EMAIL_FROM_NAME` | `backups@example.com`, `Shoebox` | Sender |

The subject is `Backup Report for <archive_label>, Size: <GB> GB, Errors:<N>`, so a mail filter can flag
`Errors:` other than `0`. The body shows the result, size, upload count and duration, then any warnings,
then a table of every folder (size, time), every database (tables found against tables expected), the
Dropbox uploads and retention, and the Dropbox space used. A plain-text version is included for mail
clients that do not show HTML.

## First run and scheduling

```sh
shoebox check                 # config, folders, databases, email and Dropbox: look for FAIL lines
shoebox run --dry-run         # what would be archived, dumped and deleted; changes nothing
shoebox run --no-email        # a real run, printed to the terminal
shoebox restore latest --target /tmp/shoebox-test   # prove you can get it back
```

Then schedule it. With cron (root's crontab, `sudo crontab -e`):

```cron
30 0 * * * /opt/shoebox/shoebox.py run > /dev/null
```

The log file and the email have the details, so normal output is discarded; errors on stderr still reach
cron's mail. A lock stops a second run for the same `archive_label` starting while one is still going.

Or with a systemd timer:

```ini
# /etc/systemd/system/shoebox.service
[Unit]
Description=Shoebox backup
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/shoebox/shoebox.py run

# /etc/systemd/system/shoebox.timer
[Unit]
Description=Nightly Shoebox backup

[Timer]
OnCalendar=*-*-* 00:30
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now shoebox.timer
```

The log file grows by a few KB a run. To rotate it, `/etc/logrotate.d/shoebox`:

```
/var/log/shoebox/shoebox.log {
    monthly
    rotate 12
    compress
    missingok
    notifempty
}
```

## Commands

```
shoebox [-c CONFIG] [--env ENV] COMMAND
```

| Command | What it does |
|---|---|
| `check` | Checks the config, every folder, every database server (and lists its databases), the email settings and the Dropbox login and space. Exit code = number of failures |
| `run` | Backs up now. Exit code = number of errors |
| `run --dry-run` | Shows what would be archived, dumped and deleted. Archives, uploads, deletes and emails nothing (it only adds to the log) |
| `run --only LABEL ...` | Only these folder labels (a split folder's label includes all its parts), database server labels or database names |
| `run --no-folders` / `--no-databases` | Skip that half |
| `run --no-dropbox` | Keep the run local only (no upload, no retention) |
| `run --no-email` / `--email-to ADDR` | No report email, or send it somewhere else this time |
| `list [--sizes]` | Lists runs in Dropbox and locally (`--sizes` adds up each Dropbox run) |
| `restore RUN --target DIR [--match TEXT ...]` | Downloads a run (`latest` or an ID like `2026-11-24-0030`) into `DIR/RUN/` and verifies every file. `--match` downloads only files whose names contain one of the texts |
| `auth` | Connects Dropbox (see [Connect Dropbox](#connect-dropbox)) |

Other exit codes: `1` for a config error or a run already in progress, `64` for no command, `130` for Ctrl-C.

## What a backup looks like

### Layout

```
Dropbox: Apps/<app name>/<dropbox_folder>/
           2026-11/                                       one folder per month
             2026-11-24-0030/                             one folder per run
               2026-11-24-0030-MYSERVER-WWW-site1-dir.tgz
               2026-11-24-0030-MYSERVER-WWW-site2-dir.tgz
               2026-11-24-0030-MYSERVER-WWW-_top-dir.tgz
               2026-11-24-0030-MYSERVER-ETC-dir.tgz
               2026-11-24-0030-MYSERVER-host-blog.sql.gz
               2026-11-24-0030-MYSERVER-shop-shop.sql.gz
               manifest.json

Local:   <local_folder>/
           2026-11-24-0030/                               the same files, kept local_keep_days
           shoebox.log, status.json                       unless log_file / status_file say otherwise
```

### File names

Every file is `<run>-<archive_label>-<item>`:

| Part | Example | Meaning |
|---|---|---|
| run | `2026-11-24-0030` | When the run started, in the server's local time (`YYYY-MM-DD-HHMM`) |
| archive_label | `MYSERVER` | From `<config>` |
| folder item | `WWW-site1-dir.tgz` | The folder label (plus the sub-folder for a split folder), then `-dir.tgz` |
| database item | `host-blog.sql.gz` | The database server label, then the database name, then `.sql.gz` |

Characters other than letters, digits, `_`, `.` and `-` in sub-folder and database names become `_`.
Because every name starts with the run ID and server label, files from different servers and nights can sit
in one folder without clashing, and sort by date.

Inside a `.tgz`, paths are the full original path without the leading `/` (`var/www/html/site1/index.php`).
A `_top` archive holds just the loose file names. A `.sql.gz` is a normal `mysqldump` of one database,
without `CREATE DATABASE` or `USE`, so it can be loaded into a database of any name.

A dump that fails its checks is kept locally as `<name>.sql.gz.failed` for a look, and is not uploaded.

### manifest.json

```json
{
 "shoebox": "0.3.0", "label": "MYSERVER", "host": "myserver.example.com",
 "run": "2026-11-24-0030", "created": "2026-11-24T00:30:01",
 "files": [
  {"file": "2026-11-24-0030-MYSERVER-ETC-dir.tgz", "kind": "folder", "item": "ETC",
   "bytes": 1843200, "sha256": "9f2c...", "content_hash": "41b7..."}
 ]
}
```

`sha256` is the plain SHA-256 of the file; `content_hash` is
[Dropbox's hash](https://www.dropbox.com/developers/reference/content-hash).

## Restore

With Shoebox:

```sh
shoebox list
shoebox restore latest --target /tmp/restore                  # the whole run
shoebox restore 2026-11-24-0030 --target /tmp/restore --match site1 blog
```

Each file is checked against Dropbox's hash and the manifest's SHA-256 and printed as `ok` or `BAD`.

Without Shoebox (on any machine): download the run folder from the Dropbox website, then check it:

```sh
cd 2026-11-24-0030
python3 -c 'import json; [print(f["sha256"] + "  " + f["file"]) for f in json.load(open("manifest.json"))["files"]]' | sha256sum -c
```

**A folder:**

```sh
tar -tzf FILE-dir.tgz | less                         # look inside
tar -xzf FILE-dir.tgz -C /tmp/restore                # everything, under /tmp/restore/var/www/...
tar -xzf FILE-dir.tgz -C /tmp/restore var/www/html/site1/index.php   # one file
sudo tar -xzpf FILE-dir.tgz -C /                     # back in place, with permissions (overwrites!)
```

**A database** (create it first; the dump does not):

```sh
mysql -e 'CREATE DATABASE blog'
zcat FILE-host-blog.sql.gz | mysql blog

# into a Docker container
docker exec shop-db sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mysql -uroot -e "CREATE DATABASE shop"'
zcat FILE-shop-shop.sql.gz | docker exec -i shop-db sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mysql -uroot shop'
```

Restore into a new, empty database first if you only need a few rows back.

## Monitoring

Besides the email, every run (except `--dry-run`) writes `status_file`, a JSON file with these fields:

| Field | Meaning |
|---|---|
| `run`, `started`, `finished`, `duration_s` | When |
| `errors`, `warnings` | Error count and warning texts |
| `size_bytes`, `subject` | Total size and the email subject |
| `folders`, `databases`, `uploads` | One entry per item, with its result |
| `retention`, `dropbox_used`, `dropbox_allocated` | What retention did, and Dropbox space |
| `local_dir`, `dropbox_dir`, `log`, `label`, `host`, `shoebox` | Where things are, and the version |

A check you can run from another job or a monitoring agent (needs `jq`):

```sh
S=/var/log/shoebox/status.json
[ "$(jq .errors "$S")" = 0 ] && [ $(( $(date +%s) - $(stat -c %Y "$S") )) -lt 93600 ] \
  && echo "backup ok" || echo "backup FAILED or older than 26 hours"
```

## Security

- **Backups are not encrypted.** Folders and dumps can hold passwords, keys and personal data. Protect the
  Dropbox account with a strong password and two-step verification. Use an **App folder** app so Shoebox
  cannot see the rest of the account, and a stolen token cannot see anything else.
- **`.env` holds the secrets.** Keep it mode 600 and owned by root. Shoebox creates it that way when `auth`
  writes the token. Exclude it from folder backups if it is not under `/etc`, or accept that it is in Dropbox.
- **Local copies** are written with mode 700 folders and 600 files.
- **No secrets on command lines.** Database passwords go through `MYSQL_PWD` in the environment; with
  `password_container_env` the password never leaves the container. The Dropbox token is sent only to
  Dropbox over HTTPS.
- To report a security problem, see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Message | Fix |
|---|---|
| `unknown element <x> in <...>` | A typo, or a setting in the wrong place. Email settings go in `.env`; database settings go in a `<database_server>` |
| `Dropbox is not set up` or `invalid_grant` | Run `shoebox auth` (again). The token stops working if the app is deleted or disconnected |
| `missing_scope` | Tick the four permissions on the app's Permissions tab, Submit, then run `auth` again |
| `X is not set in .env` | Add the variable a `<password_env>` names to `.env` |
| `X is not set in the container` | The container has no such variable: check `docker exec CONTAINER env` |
| `no mysql client in the container` / `no mysqldump in the container` | Use an image with the client tools, or dump that server through `<host>` and a published port instead |
| `N CREATE TABLE lines, database has M tables` | The dump stopped early or a table could not be read. Look at the `.failed` file and the error in the log |
| `Access denied` | Check the username, the password variable and the grants above |
| `another shoebox run for X is still going` | A run is still going (or hung): see `pgrep -af shoebox` |
| `tar exit 2` | A folder could not be read: permissions, or the path is not a folder |
| No email | `shoebox check` shows the email settings; try `run --no-dropbox --only ETC` to send a quick test |

`shoebox check` and the log (`log_file`) usually say exactly what failed.

## Upgrade and uninstall

**Upgrade:** read [CHANGELOG.md](CHANGELOG.md) for config changes, then:

```sh
cd /opt/shoebox && sudo git pull && shoebox check
```

**Uninstall:** remove the cron line or timer, then `/opt/shoebox`, `/usr/local/bin/shoebox`,
`/etc/shoebox`, the local folder and the log. Disconnect the app under
<https://www.dropbox.com/account/connected_apps>. Your backups in Dropbox stay until you delete them.

## Development

```sh
python3 -m unittest discover -s tests       # no network or database needed
ruff check . && ruff format --check .       # lint and format (settings in pyproject.toml)
```

CI runs the tests on Python 3.8 and 3.12, ruff, and a gitleaks scan of the whole history. Issues and pull
requests are welcome; please include a test with a change.

## License

[MIT](LICENSE)
