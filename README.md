# Shoebox

**Plain, verified nightly backups of folders and MySQL/MariaDB databases to Dropbox.**

Like a shoebox of photos: no key, no special tool, just open the lid. Folders become `.tgz` files and
databases become `.sql.gz` files that open with `tar` and `zcat` on any machine, years from now, without
Shoebox. Use it next to a "real" backup tool (restic, Borg, ...) as the copy you can always read.

- **One file, no dependencies:** `shoebox.py`, Python 3.8+ standard library only. No pip, no composer.
- **Verified dumps:** a database dump counts only if `mysqldump` exits 0, the file ends with
  `-- Dump completed`, and it has one `CREATE TABLE` per table in the database. `--force` is never used.
- **Any MySQL/MariaDB server:** on this machine, on another host, or in a Docker container (dumped inside the
  container with `docker exec`). Each server has its own login, dump options and choice of databases; nothing
  is assumed. A password comes from `.env` or from a variable inside the container, never from the XML or a
  command line.
- **Verified uploads:** every file is checked against Dropbox's `content_hash` after upload. Big files go up in
  chunks, with retries on rate limits and network errors.
- **Manifest:** each run has a `manifest.json` with every file's size and SHA-256.
- **Retention:** keep the newest run of each of the last N days, weeks and months in Dropbox (optional), and
  N days of local copies. Nothing is pruned after a run with errors.
- **Reports:** an HTML email (plain-text fallback) with the subject
  `Backup Report for <LABEL>, Size: <GB> GB, Errors:<N>`, a status JSON for monitoring, and an exit code equal
  to the number of errors. A lock stops two runs overlapping.
- **Easy Dropbox login:** `shoebox.py auth` uses OAuth2 PKCE with no redirect: open a link, click Allow,
  paste the code. No web server, no app secret, works over SSH.

## Install

```sh
git clone https://github.com/gelvogit/shoebox-backup.git /opt/shoebox
cp /opt/shoebox/backupconfig.example.xml /etc/shoebox/backupconfig.xml   # or anywhere you like
cp /opt/shoebox/.env.example /etc/shoebox/.env && chmod 600 /etc/shoebox/.env
```

Needs GNU `tar`, and `mysql` + `mysqldump` (or `docker`) for databases. Run it as root (or a user that can
read everything you back up).

Config and `.env` can live anywhere: pass `--config FILE` (the `.env` is read from the same folder, or
`--env FILE`). Both are git-ignored here, so keeping them in this folder is safe too.

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
3. **Copy the App key.** It is on the app's **Settings** tab. Put it in `.env` as `DROPBOX_APP_KEY=...`
   (or paste it when step 4 asks). You do not need the App secret, a redirect URI or a "generated access
   token". The app can stay in **Development** status: that is enough for your own account.
4. **Log in from the server:**

   ```sh
   shoebox.py --config /etc/shoebox/backupconfig.xml auth
   ```

   Open the link it prints (on any device), sign in to the right Dropbox account, click **Allow**, and
   paste the access code Dropbox shows. The code is not echoed. Shoebox saves a long-lived refresh token to
   `.env` (mode 600) and never prints it, then shows which account it connected to.
5. **Check it:** `shoebox.py --config /etc/shoebox/backupconfig.xml check` should show
   `ok      Dropbox <account name>` and no `FAIL` lines.

If you later change the app's permissions, run `auth` again. To disconnect a server, remove the app under
<https://www.dropbox.com/account/connected_apps> (or delete the app) and clear `DROPBOX_REFRESH_TOKEN`.

## Use

```sh
shoebox.py --config CONF check                  # config, folders, databases, email, Dropbox
shoebox.py --config CONF run --dry-run          # what would happen
shoebox.py --config CONF run --no-email         # a real run, report to the terminal + log
shoebox.py --config CONF run                    # nightly (cron)
shoebox.py --config CONF run --only WWW host    # some folder labels / DB server labels / DB names
shoebox.py --config CONF list [--sizes]         # runs in Dropbox and locally
shoebox.py --config CONF restore latest --target /tmp/restore [--match .sql.gz]
```

`restore` downloads a run and checks every file against Dropbox's hash and the manifest's SHA-256. Then:
`tar -xzf FILE.tgz -C DIR` for folders, `zcat FILE.sql.gz | mysql DBNAME` for databases.

Cron example: `30 0 * * * /opt/shoebox/shoebox.py --config /etc/shoebox/backupconfig.xml run >/dev/null`

## Layout

```
Dropbox:  Apps/<your app>/<LABEL>/2026-11/2026-11-24-0030/
              2026-11-24-0030-LABEL-WWW-site-dir.tgz
              2026-11-24-0030-LABEL-host-blog.sql.gz         (database server labelled "host")
              2026-11-24-0030-LABEL-shop-shop.sql.gz         (server "shop", a Docker container)
              manifest.json
Local:    <local_folder>/2026-11-24-0030/ (same files), shoebox.log, status.json
```

## Configuration

Two files, two jobs:

- **`backupconfig.xml`: what to back up.** Folders, database servers, retention, local paths. It holds no
  passwords, so it is safe to keep in git. See [`backupconfig.example.xml`](backupconfig.example.xml) (every
  option, commented).
- **`.env`: this server's secrets.** Database passwords (any variable names you choose), the report email
  (`SMTP_*`, `EMAIL_*`) and the Dropbox app (`DROPBOX_*`). Root-only, never in git. See
  [`.env.example`](.env.example).

### Databases

List each MySQL/MariaDB server in `<databases>`, one `<database_server>` each:

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
    <host>db.internal</host>
    <port>3306</port>
    <username>backup</username>
    <password_env>CRM_DB_PASSWORD</password_env>
    <mysql_query>SELECT SCHEMA_NAME FROM SCHEMATA WHERE SCHEMA_NAME IN ('crm')</mysql_query>
  </database_server>
</databases>
```

- `<label>` names the server in file names, reports and `run --only`.
- Exactly one of `<host>` or `<docker_container>`. A container is dumped inside itself, so the host needs
  only `docker`.
- The password comes from `<password_env>` (a variable in `.env`), `<password_container_env>` (a variable
  inside the container; it never leaves it), or neither (no password: socket login or `~/.my.cnf`). It reaches
  `mysqldump` through `MYSQL_PWD`, never the command line. A `<password>` element is refused.
- `<mysql_exe>` (optional) is the dump program and options; the options a restorable dump needs are always
  added, and `--force` is dropped. `<mysql_query>` (optional) picks the databases: it runs in
  `information_schema` and the first column is the name. The default is every database except
  `information_schema`, `performance_schema`, `mysql` and `sys`.
- A backup user needs at least `SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT` and `LOCK TABLES` (plus `PROCESS`
  on MySQL 8) on the databases it dumps.

The email settings are also accepted as XML elements (`<email_server>`, `<email_username>` and so on; a
`.env` value wins). An inline `<email_password>` works too, with a warning until it is moved to `.env`.

## Tests

```sh
python3 -m unittest discover -s tests
```

No network or database needed. CI runs them on Python 3.8 and 3.12, plus a gitleaks secret scan.

## License

MIT
