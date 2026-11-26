#!/usr/bin/env python3
"""Shoebox - plain, verified nightly backups of folders and MySQL/MariaDB databases to Dropbox.

Everything Shoebox writes can be opened without Shoebox: folders become .tgz files, databases become
.sql.gz files, and each run has a manifest.json with sizes and SHA-256 sums. No encryption, no
deduplication, no special format: it is meant to sit next to a "real" backup tool as the copy you can
always read.

Python 3.8+, standard library only. Run `shoebox.py --help`.
"""
import argparse
import base64
import datetime as dt
import fcntl
import gzip
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import shutil
import smtplib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from email.utils import formataddr, formatdate

VERSION = "0.2.0"
HERE = os.path.dirname(os.path.realpath(__file__))
RUN_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})$")          # run id, e.g. 2026-11-24-0030
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
SYSTEM_DBS = {"information_schema", "performance_schema"}
DEFAULT_DB_QUERY = ("SELECT SCHEMA_NAME FROM SCHEMATA WHERE SCHEMA_NAME NOT IN "
                    "('information_schema','performance_schema','mysql','sys')")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")    # labels and container names
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")          # environment variable names
OLD_DB_TAGS = ("mysql", "mysql_host", "mysql_username", "mysql_password", "mysql_exe", "mysql_query", "docker_mysql")
REQUIRED_DUMP_OPTS = ["--single-transaction", "--routines", "--triggers", "--events", "--hex-blob",
                      "--default-character-set=utf8mb4"]
BANNED_DUMP_OPTS = {"--force", "-f"}  # a dump that skips errors is not a backup


class ShoeboxError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- .env
def load_env(path):
    """Read KEY=VALUE lines (optional quotes, # comments). Values are never printed."""
    env = {}
    if not path or not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env[k] = v
    return env


def save_env_value(path, key, value):
    """Set KEY=value in the .env file (created root-only), keeping every other line."""
    lines, found = [], False
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*(export\s+)?%s\s*=" % re.escape(key), line):
            lines[i] = "%s='%s'" % (key, value)
            found = True
    if not found:
        lines.append("%s='%s'" % (key, value))
    old = os.umask(0o077)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old)


# --------------------------------------------------------------------------------------------- config
def _text(node, tag, default=""):
    el = node.find(tag) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text is not None else default


def _int(node, tag, default):
    v = _text(node, tag)
    try:
        return int(v) if v != "" else default
    except ValueError:
        raise ShoeboxError("<%s> must be a number, got %r" % (tag, v))


class Config:
    """backupconfig.xml (what to back up, and how to reach the databases) + .env (passwords and logins)."""

    def __init__(self, xml_path, env):
        self.path = os.path.abspath(xml_path)
        self.warnings = []
        try:
            root = ET.parse(xml_path).getroot()
        except (OSError, ET.ParseError) as e:
            raise ShoeboxError("cannot read %s: %s" % (xml_path, e))
        c = root.find("config")
        if c is None:
            raise ShoeboxError("%s has no <config> section" % xml_path)

        self.label = _text(c, "archive_label")
        if not re.match(r"^[A-Za-z0-9_.-]+$", self.label or ""):
            raise ShoeboxError("<archive_label> must be letters, digits, _ . - (got %r)" % self.label)
        self.local_folder = _text(c, "local_folder") or "/var/backups/shoebox"
        self.tar_exe = _text(c, "tar_exe") or "tar"
        self.chunk_size = max(_int(c, "dropbox_chunksize", 32 * 1024 * 1024), 4 * 1024 * 1024)
        self.chunk_size = min(self.chunk_size // (4 * 1024 * 1024) * 4 * 1024 * 1024, 148 * 1024 * 1024)
        self.dropbox_folder = "/" + (_text(c, "dropbox_folder") or self.label).strip("/")
        self.local_keep_days = _int(c, "local_keep_days", 7)
        self.log_file = _text(c, "log_file") or os.path.join(self.local_folder, "shoebox.log")
        self.status_file = _text(c, "status_file") or os.path.join(self.local_folder, "status.json")

        # Connection settings and logins come from .env; the XML elements are only a fallback for old
        # configs (env wins). The XML is then just "what to back up and how long to keep it".
        def pick(key, tag, default=""):
            return env.get(key, "").strip() or _text(c, tag) or default

        def pick_int(key, tag, default):
            v = env.get(key, "").strip()
            if not v:
                return _int(c, tag, default)
            try:
                return int(v)
            except ValueError:
                raise ShoeboxError("%s in .env must be a number, got %r" % (key, v))

        self.email = {
            "server": pick("SMTP_HOST", "email_server"), "port": pick_int("SMTP_PORT", "email_port", 587),
            "secure": pick("SMTP_SECURE", "email_secure").lower(),
            "auth": pick("SMTP_AUTH", "email_auth", "y").lower() == "y",
            "username": pick("SMTP_USERNAME", "email_username"), "to": pick("EMAIL_TO", "email_to"),
            "from": pick("EMAIL_FROM", "email_from"), "from_name": pick("EMAIL_FROM_NAME", "email_from_name", "Shoebox"),
            "password": env.get("SMTP_PASSWORD", ""),
        }
        if _text(c, "email_password"):
            self.warnings.append("<email_password> is in the XML: move it to SMTP_PASSWORD in .env")
            self.email["password"] = self.email["password"] or _text(c, "email_password")

        # Databases: <databases><database_server>, one per MySQL/MariaDB server (this machine, another host,
        # or a Docker container). The old <mysql>/<docker_mysql> elements are refused, not guessed at.
        old = [t for t in OLD_DB_TAGS if c.find(t) is not None]
        if old:
            raise ShoeboxError("<%s> is the old database format: list each server as a <database_server> "
                               "inside <databases> (see backupconfig.example.xml)" % ">, <".join(old))
        dbs = root.find("databases")
        self.db_servers = [DbServer(n, i, env, self.warnings)
                           for i, n in enumerate(dbs.findall("database_server") if dbs is not None else [], 1)]
        labels = [d.name for d in self.db_servers]
        dupes = sorted({x for x in labels if labels.count(x) > 1})
        if dupes:
            raise ShoeboxError("<database_server> labels must be unique: %s" % ", ".join(dupes))

        # Dropbox retention (None = keep every run)
        r = c.find("retention")
        self.retention = None
        if r is not None:
            self.retention = {k: _int(r, k, 0) for k in ("daily", "weekly", "monthly")}

        # folders
        self.folders = []
        fl = root.find("folder_list")
        for f in (fl.findall("folder") if fl is not None else []):
            label = _text(f, "folder_label")
            path = _text(f, "folder_path")
            if not label or not path:
                self.warnings.append("a <folder> without <folder_label> or <folder_path> was skipped")
                continue
            self.folders.append({
                "label": label, "path": path,
                "excludes": [(e.text or "").strip() for e in f.findall("folder_exclude") if (e.text or "").strip()],
                "split": _text(f, "folder_split").lower() == "y",
                "split_excludes": [(e.text or "").strip() for e in f.findall("folder_split_exclude/exclude")
                                   if (e.text or "").strip()],
            })

        # Dropbox app (PKCE: the secret is optional)
        self.dropbox_app_key = env.get("DROPBOX_APP_KEY", "")
        self.dropbox_app_secret = env.get("DROPBOX_APP_SECRET", "")
        self.dropbox_refresh_token = env.get("DROPBOX_REFRESH_TOKEN", "")

    def folder_items(self):
        """Expand <folder_split>: one archive per sub-folder (minus split excludes), plus one for the files
        sitting directly in the split folder."""
        items = []
        for f in self.folders:
            if not f["split"]:
                items.append({"label": f["label"], "path": f["path"], "excludes": f["excludes"], "files": None})
                continue
            if not os.path.isdir(f["path"]):
                items.append({"label": f["label"], "path": f["path"], "excludes": f["excludes"], "files": None})
                continue
            loose = []
            for entry in sorted(os.listdir(f["path"])):
                full = os.path.join(f["path"], entry)
                if os.path.isdir(full) and not os.path.islink(full):
                    if entry not in f["split_excludes"]:
                        items.append({"label": "%s-%s" % (f["label"], entry), "path": full,
                                      "excludes": f["excludes"], "files": None})
                elif entry not in f["split_excludes"]:
                    loose.append(entry)
            if loose:
                items.append({"label": "%s-_top" % f["label"], "path": f["path"], "excludes": [], "files": loose})
        return items


# ------------------------------------------------------------------------------------------- helpers
def human(n):
    if n is None:
        return "-"
    n = float(n)
    for limit, unit, fmt in ((1e3, "B", "%d %s"), (1e6, "KB", "%.1f %s"), (1e9, "MB", "%.1f %s"),
                             (1e12, "GB", "%.2f %s")):
        if n < limit:
            return fmt % (n / (limit / 1e3) if unit != "B" else n, unit)
    return "%.2f TB" % (n / 1e12)


def dur(s):
    if s is None:
        return ""
    s = int(s)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm %02ds" % (s // 60, s % 60)
    return "%dh %02dm" % (s // 3600, s % 3600 // 60)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "x"


def file_hashes(path):
    """(sha256 hex, Dropbox content_hash hex) in one pass. content_hash = sha256 of the concatenated
    sha256 digests of each 4 MiB block (https://www.dropbox.com/developers/reference/content-hash)."""
    full, blocks = hashlib.sha256(), hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(4 * 1024 * 1024)
            if not block:
                break
            full.update(block)
            blocks.update(hashlib.sha256(block).digest())
    return full.hexdigest(), blocks.hexdigest()


def run_time(run_id):
    m = RUN_RE.match(run_id)
    return dt.datetime(*[int(x) for x in m.groups()]) if m else None


def plan_retention(run_ids, daily, weekly, monthly):
    """Return the run ids to keep: the newest run of each of the last `daily` days, `weekly` ISO weeks and
    `monthly` months that have runs (the restic/GFS rule). The newest run is always kept."""
    runs = sorted((r for r in run_ids if RUN_RE.match(r)), reverse=True)
    keep = set(runs[:1])
    for n, key in ((daily, lambda t: t.date()), (weekly, lambda t: tuple(t.isocalendar()[:2])),
                   (monthly, lambda t: (t.year, t.month))):
        seen = []
        for r in runs:
            if n <= 0:
                break
            k = key(run_time(r))
            if k in seen:
                continue
            if len(seen) >= n:
                break
            seen.append(k)
            keep.add(r)
    return keep


# ------------------------------------------------------------------------------------------- Dropbox
class DropboxError(ShoeboxError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        try:
            self.summary = json.loads(body).get("error_summary", body)
        except (ValueError, AttributeError):
            self.summary = body
        super().__init__("Dropbox HTTP %s: %s" % (status, str(self.summary)[:300]))


class Dropbox:
    """Just enough of the Dropbox v2 HTTP API, with token refresh and retries (429, 5xx, network)."""
    API, CONTENT, TOKEN = "https://api.dropboxapi.com/2", "https://content.dropboxapi.com/2", \
        "https://api.dropboxapi.com/oauth2/token"

    def __init__(self, app_key, refresh_token, app_secret="", log=None):
        if not app_key or not refresh_token:
            raise ShoeboxError("Dropbox is not set up: run `shoebox.py auth` (needs DROPBOX_APP_KEY in .env)")
        self.app_key, self.refresh_token, self.app_secret = app_key, refresh_token, app_secret
        self.log = log or (lambda m: None)
        self.access_token, self.expires = None, 0

    def _refresh(self):
        data = {"grant_type": "refresh_token", "refresh_token": self.refresh_token, "client_id": self.app_key}
        if self.app_secret:
            data["client_secret"] = self.app_secret
        r = self._http(self.TOKEN, urllib.parse.urlencode(data).encode(),
                       {"Content-Type": "application/x-www-form-urlencoded"}, auth=False)
        tok = json.loads(r.read())
        self.access_token = tok["access_token"]
        self.expires = time.time() + int(tok.get("expires_in", 14400)) - 300

    def _http(self, url, body, headers, auth=True, stream=False, tries=7):
        delay = 2
        for attempt in range(1, tries + 1):
            h = dict(headers)
            if auth:
                if not self.access_token or time.time() > self.expires:
                    self._refresh()
                h["Authorization"] = "Bearer " + self.access_token
            req = urllib.request.Request(url, data=body, headers=h, method="POST")
            try:
                return urllib.request.urlopen(req, timeout=600)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                if e.code == 401 and auth and attempt == 1:
                    self.access_token = None  # expired early: refresh and retry once
                    continue
                if (e.code == 429 or e.code >= 500) and attempt < tries:
                    wait = int(e.headers.get("Retry-After") or delay)
                    self.log("  Dropbox HTTP %s, retrying in %ss" % (e.code, wait))
                    time.sleep(wait)
                    delay = min(delay * 2, 120)
                    continue
                raise DropboxError(e.code, text)
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                if attempt < tries:
                    self.log("  network error (%s), retrying in %ss" % (e, delay))
                    time.sleep(delay)
                    delay = min(delay * 2, 120)
                    continue
                raise ShoeboxError("network error talking to Dropbox: %s" % e)
        raise ShoeboxError("Dropbox: too many retries")

    def rpc(self, endpoint, args=None):
        body = json.dumps(args).encode() if args is not None else b"null"
        return json.loads(self._http(self.API + endpoint, body, {"Content-Type": "application/json"}).read() or b"null")

    def _content(self, endpoint, arg, data=b""):
        return self._http(self.CONTENT + endpoint, data,
                          {"Dropbox-API-Arg": json.dumps(arg), "Content-Type": "application/octet-stream"})

    def upload(self, local, dest, chunk):
        size = os.path.getsize(local)
        commit = {"path": dest, "mode": "overwrite", "mute": True, "strict_conflict": False}
        with open(local, "rb") as f:
            if size <= chunk:
                return json.loads(self._content("/files/upload", commit, f.read()).read())
            sid = json.loads(self._content("/files/upload_session/start", {"close": False}, f.read(chunk)).read())["session_id"]
            offset = chunk
            while size - offset > chunk:
                f.seek(offset)
                try:
                    self._content("/files/upload_session/append_v2",
                                  {"cursor": {"session_id": sid, "offset": offset}, "close": False}, f.read(chunk))
                except DropboxError as e:  # a retried append that had already landed: resume where Dropbox is
                    correct = _correct_offset(e)
                    if correct is None:
                        raise
                    offset = correct
                    continue
                offset += chunk
            while True:
                f.seek(offset)
                try:
                    r = self._content("/files/upload_session/finish",
                                      {"cursor": {"session_id": sid, "offset": offset}, "commit": commit}, f.read())
                    return json.loads(r.read())
                except DropboxError as e:
                    correct = _correct_offset(e)
                    if correct is None or correct == offset:
                        raise
                    offset = correct

    def list_folder(self, path, recursive=False):
        try:
            r = self.rpc("/files/list_folder", {"path": path, "recursive": recursive, "limit": 2000})
        except DropboxError as e:
            if "not_found" in str(e.summary):
                return []
            raise
        entries = r["entries"]
        while r.get("has_more"):
            r = self.rpc("/files/list_folder/continue", {"cursor": r["cursor"]})
            entries += r["entries"]
        return entries

    def delete(self, path):
        return self.rpc("/files/delete_v2", {"path": path})

    def download(self, path, local):
        r = self._content("/files/download", {"path": path})
        meta = json.loads(r.headers.get("Dropbox-API-Result") or "{}")
        tmp = local + ".part"
        with open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
        os.replace(tmp, local)
        return meta

    def space(self):
        r = self.rpc("/users/get_space_usage")
        alloc = r.get("allocation", {})
        return r.get("used"), alloc.get("allocated")


def _correct_offset(err):
    try:
        e = json.loads(err.body)["error"]
        return e.get("lookup_failed", e).get("correct_offset")
    except (ValueError, KeyError, AttributeError, TypeError):
        return None


# ------------------------------------------------------------------------------------------- dumping
def parse_dump_exe(text, where, warnings):
    """<mysql_exe>: the dump program plus options. --force is dropped (a dump that skips errors is not a
    backup) and the options a restorable dump needs are added. Returns (program or None, options)."""
    exe = shlex.split(text or "")
    banned = [o for o in exe[1:] if o in BANNED_DUMP_OPTS]
    if banned:
        warnings.append("%s: <mysql_exe> option %s ignored: a dump that skips errors is not a backup"
                        % (where, " ".join(banned)))
    opts = [o for o in exe[1:] if o not in BANNED_DUMP_OPTS]
    opts += [o for o in REQUIRED_DUMP_OPTS if o.split("=")[0] not in [x.split("=")[0] for x in opts]]
    return (exe[0] if exe else None), opts


class DbServer:
    """One <database_server>: a MySQL/MariaDB server on this machine or another host (<host>), or in a Docker
    container (<docker_container>, dumped inside the container). Nothing is assumed: the label, the login
    and where its password comes from are all in the XML; only password values live in .env."""

    def __init__(self, node, index, env, warnings):
        where = "<database_server> #%d" % index
        self.name = _text(node, "label")
        if not NAME_RE.match(self.name):
            raise ShoeboxError("%s needs a <label> of letters, digits, _ . - (got %r)" % (where, self.name))
        where = "<database_server> %s" % self.name
        self.where = where
        self.container = _text(node, "docker_container")
        self.host = _text(node, "host")
        self.port = _text(node, "port")
        if bool(self.container) == bool(self.host):
            raise ShoeboxError("%s needs exactly one of <host> (this machine or another server) or "
                               "<docker_container>" % where)
        if self.container and not NAME_RE.match(self.container):
            raise ShoeboxError("%s: <docker_container> %r is not a valid container name" % (where, self.container))
        if self.port and (self.container or not self.port.isdigit()):
            raise ShoeboxError("%s: <port> must be a number, and only goes with <host>" % where)
        self.username = _text(node, "username")
        if not self.username:
            raise ShoeboxError("%s needs a <username>" % where)
        if node.find("password") is not None:
            raise ShoeboxError("%s: passwords do not go in the XML. Put it in .env and name that variable "
                               "in <password_env>" % where)
        self.password_env = _text(node, "password_env")
        self.password_container_env = _text(node, "password_container_env")
        if self.password_env and self.password_container_env:
            raise ShoeboxError("%s: use <password_env> or <password_container_env>, not both" % where)
        for tag, v in (("password_env", self.password_env), ("password_container_env", self.password_container_env)):
            if v and not VAR_RE.match(v):
                raise ShoeboxError("%s: <%s> must be a variable name, got %r" % (where, tag, v))
        if self.password_container_env and not self.container:
            raise ShoeboxError("%s: <password_container_env> only goes with <docker_container>" % where)
        # A missing .env value fails this server at run time (the rest of the backup still runs)
        self.password, self.error = None, ""
        if self.password_env:
            self.password = env.get(self.password_env, "")
            if not self.password:
                self.error = "%s is not set in .env" % self.password_env
        self.exe, self.dump_opts = parse_dump_exe(_text(node, "mysql_exe"), where, warnings)
        self.query_sql = _text(node, "mysql_query") or DEFAULT_DB_QUERY

    def describe(self):
        if self.container:
            return "docker %s as %s" % (self.container, self.username)
        return "%s%s as %s" % (self.host, ":" + self.port if self.port else "", self.username)

    def _env(self):
        env = dict(os.environ)
        env.pop("MYSQL_PWD", None)
        if self.password:
            env["MYSQL_PWD"] = self.password  # passed in the environment, never on a command line (`ps`)
        return env

    def _docker(self, script, args):
        cmd = ["docker", "exec"]
        if self.password:
            cmd += ["-e", "MYSQL_PWD"]  # no value here: docker copies it from our environment
            pre = ""
        elif self.password_container_env:
            v = self.password_container_env  # checked against VAR_RE
            pre = ('MYSQL_PWD="${%s:-}"; [ -n "$MYSQL_PWD" ] || { echo "%s is not set in the container" >&2; '
                   'exit 1; }; export MYSQL_PWD; ' % (v, v))
        else:
            pre = "unset MYSQL_PWD; "
        return cmd + [self.container, "sh", "-c", pre + script, "sh"] + list(args)

    def _host_args(self):
        return ["-h", self.host] + (["-P", self.port] if self.port else []) + ["-u", self.username]

    def query(self, sql, db="information_schema"):
        if self.error:
            raise ShoeboxError(self.error)
        if self.container:
            cmd = self._docker('c=$(command -v mariadb || command -v mysql) || { echo "no mysql client in the '
                               'container" >&2; exit 127; }; exec "$c" -u "$1" -NB -D "$2" -e "$3"',
                               [self.username, db, sql])
        else:
            client = shutil.which("mysql") or shutil.which("mariadb") or "mysql"
            cmd = [client] + self._host_args() + ["-NB", "-D", db, "-e", sql]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env())
        except OSError as e:
            raise ShoeboxError("%s: %s" % (self.name, e))
        if p.returncode != 0:
            raise ShoeboxError("%s: %s" % (self.name, p.stderr.decode("utf-8", "replace").strip()[-300:]))
        return [l for l in p.stdout.decode().splitlines() if l.strip()]

    def databases(self):
        return [d.split("\t")[0] for d in self.query(self.query_sql) if d.split("\t")[0] not in SYSTEM_DBS]

    def table_count(self, db):
        rows = self.query("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='%s' "
                          "AND TABLE_TYPE='BASE TABLE'" % db.replace("'", "''"))
        return int(rows[0]) if rows else None

    def dump_cmd(self, db):
        if self.container:
            if self.exe:
                return self._docker('exec "$@"', [self.exe, "-u", self.username] + self.dump_opts + [db])
            return self._docker('d=$(command -v mariadb-dump || command -v mysqldump) || { echo "no mysqldump in '
                                'the container" >&2; exit 127; }; exec "$d" "$@"',
                                ["-u", self.username] + self.dump_opts + [db])
        return [self.exe or "mysqldump"] + self._host_args() + self.dump_opts + [db]


def dump_to_gzip(cmd, out_path, env=None):
    """Stream a dump command into a .gz file. Returns (exit code, CREATE TABLE count, last line, stderr tail).
    The SQL never touches the disk uncompressed."""
    creates, last = 0, b""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, env=env)
        with gzip.open(out_path, "wb", compresslevel=6) as gz:
            for line in p.stdout:
                gz.write(line)
                if line.startswith(b"CREATE TABLE "):
                    creates += 1
                if line.strip():
                    last = line
        rc = p.wait()
        err.seek(0)
        tail = err.read().decode("utf-8", "replace").strip()[-300:]
    return rc, creates, last.decode("utf-8", "replace").strip(), tail


def check_dump(rc, creates, last, expected):
    """A dump is good only if it exited 0, ends with '-- Dump completed', and has one CREATE TABLE per
    BASE TABLE. Returns '' when good, else the reason."""
    if rc != 0:
        return "dump exit code %s" % rc
    if not last.startswith("-- Dump completed"):
        return "no '-- Dump completed' at the end"
    if expected is None or creates != expected:
        return "%s CREATE TABLE lines, database has %s tables" % (creates, expected)
    return ""


# ---------------------------------------------------------------------------------------------- run
class Run:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.started = dt.datetime.now()
        self.run_id = self.started.strftime("%Y-%m-%d-%H%M")
        self.local_dir = os.path.join(cfg.local_folder, self.run_id)
        self.remote_dir = "%s/%s/%s" % (cfg.dropbox_folder, self.run_id[:7], self.run_id)
        self.errors, self.warnings = 0, list(cfg.warnings)
        self.folders, self.databases, self.uploads, self.files = [], [], [], []
        self.retention = {}
        self.dropbox_space = (None, None)
        self.logf = None

    # logging
    def log(self, msg):
        line = "%s %s" % (dt.datetime.now().strftime("%F %T"), msg)
        print(line, flush=True)
        if self.logf:
            self.logf.write(line + "\n")
            self.logf.flush()

    def fail(self, msg):
        self.errors += 1
        self.log("FAIL " + msg)

    def name(self, *parts):
        return "-".join([self.run_id, self.cfg.label] + [safe_name(p) for p in parts])

    # steps
    def do_folders(self):
        only = self.args.only
        for it in self.cfg.folder_items():
            if only and not any(it["label"] == o or it["label"].startswith(o + "-") for o in only):
                continue
            fname = self.name(it["label"], "dir") + ".tgz"
            out = os.path.join(self.local_dir, fname)
            rec = {"label": it["label"], "path": it["path"], "file": fname, "rc": None, "bytes": 0, "secs": 0, "detail": ""}
            self.folders.append(rec)
            if not os.path.isdir(it["path"]):
                rec["rc"], rec["detail"] = "missing", "not a folder"
                self.fail("folder %s: %s is not a folder" % (it["label"], it["path"]))
                continue
            if self.args.dry_run:
                rec["rc"], rec["detail"] = "dry-run", "would archive"
                self.log("dry-run: would archive %s -> %s" % (it["path"], fname))
                continue
            cmd = [self.cfg.tar_exe, "--create", "--gzip", "--file", out, "--warning=no-file-changed"]
            cmd += ["--exclude=%s" % e for e in it["excludes"]]
            if it["files"]:
                cmd += ["-C", it["path"], "--"] + it["files"]
            else:
                cmd += ["--", it["path"]]
            t = time.time()
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rec["secs"] = round(time.time() - t)
            rec["bytes"] = os.path.getsize(out) if os.path.exists(out) else 0
            msg = p.stderr.decode("utf-8", "replace").strip()
            if p.returncode == 0:
                rec["rc"] = "0"
            elif p.returncode == 1 and rec["bytes"] > 0:  # GNU tar: some files changed while being read
                rec["rc"], rec["detail"] = "warn", "some files changed while archiving"
                self.warnings.append("%s: some files changed while being archived" % it["label"])
            else:
                rec["rc"], rec["detail"] = str(p.returncode), msg[-200:]
                self.fail("folder %s: tar exit %s %s" % (it["label"], p.returncode, msg[-200:]))
                continue
            self.files.append({"file": fname, "kind": "folder", "item": it["label"]})
            self.log("OK   folder %-28s %9s %5ss" % (it["label"], human(rec["bytes"]), rec["secs"]))

    def do_databases(self):
        for src in self.cfg.db_servers:
            try:
                dbs = src.databases()
            except ShoeboxError as e:
                self.databases.append({"source": src.name, "db": "*", "rc": "1", "problem": str(e)})
                self.fail("databases on %s: %s" % (src.name, e))
                continue
            for db in dbs:
                if self.args.only and src.name not in self.args.only and db not in self.args.only:
                    continue
                self.dump_one(src, db)

    def dump_one(self, src, db):
        fname = self.name(src.name, db) + ".sql.gz"
        out = os.path.join(self.local_dir, fname)
        rec = {"source": src.name, "db": db, "file": fname, "rc": None, "bytes": 0, "tables": None,
               "expected": None, "secs": 0, "problem": ""}
        self.databases.append(rec)
        if self.args.dry_run:
            rec["rc"] = "dry-run"
            self.log("dry-run: would dump %s/%s -> %s" % (src.name, db, fname))
            return
        t = time.time()
        try:
            rec["expected"] = src.table_count(db)
            rc, creates, last, err = dump_to_gzip(src.dump_cmd(db), out + ".part", src._env())
        except (ShoeboxError, OSError) as e:
            rc, creates, last, err = 1, 0, "", str(e)
        rec["tables"], rec["secs"] = creates, round(time.time() - t)
        problem = check_dump(rc, creates, last, rec["expected"])
        if problem:
            rec["rc"], rec["problem"] = str(rc or 1), problem + (" (%s)" % err if err and rc else "")
            if os.path.exists(out + ".part"):
                os.replace(out + ".part", out + ".failed")
            self.fail("dump %s/%s: %s" % (src.name, db, rec["problem"]))
            return
        os.replace(out + ".part", out)
        rec["rc"], rec["bytes"] = "0", os.path.getsize(out)
        self.files.append({"file": fname, "kind": "database", "item": "%s/%s" % (src.name, db)})
        self.log("OK   dump   %-28s %9s %5ss  %s/%s tables" % ("%s/%s" % (src.name, db), human(rec["bytes"]),
                                                              rec["secs"], creates, rec["expected"]))

    def write_manifest(self):
        for f in self.files:
            path = os.path.join(self.local_dir, f["file"])
            f["bytes"] = os.path.getsize(path)
            f["sha256"], f["content_hash"] = file_hashes(path)
        manifest = {"shoebox": VERSION, "label": self.cfg.label, "host": socket.getfqdn(), "run": self.run_id,
                    "created": self.started.isoformat(timespec="seconds"), "files": self.files}
        path = os.path.join(self.local_dir, "manifest.json")
        with open(path, "w") as f:
            json.dump(manifest, f, indent=1)
        return path

    def do_upload(self, dbx):
        names = [f["file"] for f in self.files] + ["manifest.json"]
        hashes = {f["file"]: f["content_hash"] for f in self.files}
        hashes["manifest.json"] = file_hashes(os.path.join(self.local_dir, "manifest.json"))[1]
        for n in names:
            local = os.path.join(self.local_dir, n)
            rec = {"file": n, "bytes": os.path.getsize(local), "ok": False, "secs": 0, "detail": ""}
            self.uploads.append(rec)
            t = time.time()
            try:
                meta = dbx.upload(local, "%s/%s" % (self.remote_dir, n), self.cfg.chunk_size)
                rec["secs"] = round(time.time() - t)
                if meta.get("content_hash") != hashes[n] or meta.get("size") != rec["bytes"]:
                    rec["detail"] = "checksum mismatch after upload"
                    self.fail("upload %s: Dropbox copy does not match (size %s vs %s)" % (n, meta.get("size"), rec["bytes"]))
                    continue
                rec["ok"] = True
                self.log("OK   upload %-40s %9s %5ss  hash verified" % (n[len(self.run_id) + 1:] or n, human(rec["bytes"]), rec["secs"]))
            except ShoeboxError as e:
                rec["secs"], rec["detail"] = round(time.time() - t), str(e)[:200]
                self.fail("upload %s: %s" % (n, e))

    def do_retention(self, dbx):
        r = self.cfg.retention
        if r is None:
            self.retention = {"policy": "keep everything", "deleted": [], "kept": None}
            return
        runs = {}
        for m in dbx.list_folder(self.cfg.dropbox_folder):
            if m[".tag"] == "folder" and MONTH_RE.match(m["name"]):
                for e in dbx.list_folder(m["path_lower"]):
                    if e[".tag"] == "folder" and RUN_RE.match(e["name"]):
                        runs[e["name"]] = e["path_display"]
        keep = plan_retention(runs, r["daily"], r["weekly"], r["monthly"])
        keep.add(self.run_id)
        gone = sorted(set(runs) - keep)
        self.retention = {"policy": "keep %(daily)s daily, %(weekly)s weekly, %(monthly)s monthly" % r,
                          "deleted": gone, "kept": len(set(runs) & keep)}
        for rid in gone:
            if self.args.dry_run:
                self.log("dry-run: would delete Dropbox run %s" % rid)
                continue
            dbx.delete(runs[rid])
            self.log("OK   retention: deleted Dropbox run %s" % rid)
        if not self.args.dry_run:  # drop month folders left empty
            for m in dbx.list_folder(self.cfg.dropbox_folder):
                if m[".tag"] == "folder" and MONTH_RE.match(m["name"]) and not dbx.list_folder(m["path_lower"]):
                    dbx.delete(m["path_display"])

    def prune_local(self):
        cutoff = self.started - dt.timedelta(days=self.cfg.local_keep_days)
        removed = []
        for e in sorted(os.listdir(self.cfg.local_folder)):
            t = run_time(e)
            full = os.path.join(self.cfg.local_folder, e)
            if t and e != self.run_id and t < cutoff and os.path.isdir(full):
                if not self.args.dry_run:
                    shutil.rmtree(full)
                removed.append(e)
        if removed:
            self.log("OK   local: %s %d run(s) older than %d days (%s)" % (
                "would remove" if self.args.dry_run else "removed", len(removed), self.cfg.local_keep_days, ", ".join(removed)))

    def execute(self):
        a = self.args
        os.makedirs(self.cfg.local_folder, mode=0o700, exist_ok=True)
        os.makedirs(os.path.dirname(self.cfg.log_file) or ".", exist_ok=True)
        self.logf = open(self.cfg.log_file, "a")
        self.log("===== shoebox %s run %s (%s)%s" % (VERSION, self.run_id, self.cfg.label, " DRY RUN" if a.dry_run else ""))
        for w in self.warnings:
            self.log("WARN " + w)
        if not a.dry_run:
            os.makedirs(self.local_dir, mode=0o700, exist_ok=True)
        if not a.no_folders:
            self.do_folders()
        if not a.no_mysql:
            self.do_databases()

        dbx = None
        if not a.no_dropbox:
            try:
                dbx = Dropbox(self.cfg.dropbox_app_key, self.cfg.dropbox_refresh_token, self.cfg.dropbox_app_secret, self.log)
            except ShoeboxError as e:
                self.fail(str(e))
        if not a.dry_run and self.files:
            self.write_manifest()
            if dbx:
                self.do_upload(dbx)
        if dbx:
            try:
                if self.errors == 0:  # never thin out Dropbox after a bad night
                    self.do_retention(dbx)
                else:
                    self.retention = {"policy": "skipped: this run had errors", "deleted": [], "kept": None}
                self.dropbox_space = dbx.space()
            except ShoeboxError as e:
                self.fail("Dropbox retention/space: %s" % e)
        try:
            self.prune_local()
        except OSError as e:
            self.fail("local prune: %s" % e)

        self.finished = dt.datetime.now()
        status = self.status()
        if not a.dry_run:
            self.write_status(status)
        if not (a.no_email or a.dry_run):
            self.send_email(status)
            status["errors"] = self.errors
            self.write_status(status)
        self.log("===== end: %d error(s), %d warning(s)" % (self.errors, len(self.warnings)))
        return self.errors

    # reporting
    def status(self):
        size = sum(f.get("bytes", 0) for f in self.files)
        used, alloc = self.dropbox_space
        return {
            "shoebox": VERSION, "label": self.cfg.label, "host": socket.getfqdn(), "run": self.run_id,
            "started": self.started.strftime("%F %T"), "finished": self.finished.strftime("%F %T"),
            "duration_s": round((self.finished - self.started).total_seconds()),
            "errors": self.errors, "warnings": self.warnings, "size_bytes": size,
            "subject": "Backup Report for %s, Size: %.2f GB, Errors:%d" % (self.cfg.label, size / 1e9, self.errors),
            "local_dir": self.local_dir, "dropbox_dir": self.remote_dir,
            "folders": self.folders, "databases": self.databases, "uploads": self.uploads,
            "retention": self.retention, "dropbox_used": used, "dropbox_allocated": alloc,
            "log": self.cfg.log_file,
        }

    def write_status(self, status):
        os.makedirs(os.path.dirname(self.cfg.status_file) or ".", exist_ok=True)
        tmp = self.cfg.status_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=1)
        os.replace(tmp, self.cfg.status_file)

    def send_email(self, st):
        e = self.cfg.email
        to = self.args.email_to or e["to"]
        if not (e["server"] and to and e["from"]):
            self.warnings.append("email not configured (SMTP_HOST, EMAIL_TO, EMAIL_FROM in .env)")
            return
        text = report_text(st)
        try:
            page = report_html(st)
        except Exception as ex:  # never lose the report because of the HTML
            page = None
            self.warnings.append("HTML report failed (%s: %s), sent as plain text" % (type(ex).__name__, ex))
            text += "\n(The HTML report failed: %s. This is the plain-text version.)\n" % ex
        msg = EmailMessage()
        msg["From"] = formataddr((e["from_name"], e["from"]))
        msg["To"] = to
        msg["Subject"] = st["subject"]
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(text)
        if page:
            msg.add_alternative(page, subtype="html")
        try:
            cls = smtplib.SMTP_SSL if e["secure"] == "ssl" else smtplib.SMTP
            with cls(e["server"], e["port"], timeout=60) as s:
                if e["secure"] == "tls":
                    s.starttls()
                if e["auth"]:
                    s.login(e["username"], e["password"])
                s.send_message(msg)
            self.log("OK   email %s to %s" % ("HTML" if page else "plain text", to))
        except (OSError, smtplib.SMTPException) as ex:
            self.fail("email to %s: %s: %s" % (to, type(ex).__name__, str(ex)[:200]))


# ------------------------------------------------------------------------------------------- reports
def report_text(st):
    out = [st["subject"], "",
           "Host:     %s" % st["host"], "Run:      %s" % st["run"],
           "Started:  %s" % st["started"], "Finished: %s (%s)" % (st["finished"], dur(st["duration_s"])),
           "Dropbox:  %s" % st["dropbox_dir"], ""]
    if st["warnings"]:
        out += ["Warnings:"] + ["  " + w for w in st["warnings"]] + [""]
    if st["folders"]:
        out.append("Folders:")
        out += ["  %-8s %-30s %10s  %s" % (f["rc"], f["label"], human(f["bytes"]), f.get("detail", "")) for f in st["folders"]]
        out.append("")
    if st["databases"]:
        out.append("Databases:")
        out += ["  %-8s %-30s %10s  %s/%s tables %s" % (d["rc"], "%s/%s" % (d["source"], d["db"]), human(d.get("bytes")),
                                                     d.get("tables"), d.get("expected"), d.get("problem", ""))
                for d in st["databases"]]
        out.append("")
    ok = sum(1 for u in st["uploads"] if u["ok"])
    out.append("Uploads: %d of %d files, checksums verified" % (ok, len(st["uploads"])))
    bad = [u for u in st["uploads"] if not u["ok"]]
    out += ["  FAILED %s: %s" % (u["file"], u["detail"]) for u in bad]
    r = st["retention"]
    if r:
        out.append("Retention: %s; deleted %d run(s)" % (r.get("policy"), len(r.get("deleted") or [])))
    out += ["", "Log: %s" % st["log"]]
    return "\n".join(out) + "\n"


GREEN, RED, AMBER, GREY = "#1a7f37", "#cf222e", "#9a6700", "#57606a"
TD = 'style="padding:6px 10px;border-bottom:1px solid #eaeef2;vertical-align:top;word-break:break-word;overflow-wrap:anywhere"'
TH = ('style="padding:6px 10px;border-bottom:2px solid #d0d7de;text-align:left;font-size:14px;color:#57606a;'
      'text-transform:uppercase;letter-spacing:.04em;white-space:nowrap"')
TDR = 'style="padding:6px 10px;border-bottom:1px solid #eaeef2;vertical-align:top;text-align:right;white-space:nowrap"'
esc = html.escape


def _code(text):
    """Monospace text; the empty spans around dots stop mail clients turning names like site.com into links."""
    return '<code style="font-size:15px">%s</code>' % esc(str(text)).replace(".", "<span>.</span>")


def _pill(colour, text):
    return ('<span style="display:inline-block;padding:1px 8px;border-radius:10px;white-space:nowrap;'
            'background:%s;color:#fff;font-size:14px;font-weight:600">%s</span>' % (colour, esc(text)))


def _badge(rc):
    rc = str(rc)
    return {"0": _pill(GREEN, "OK"), "True": _pill(GREEN, "OK"), "warn": _pill(AMBER, "WARNING"),
            "dry-run": _pill(GREY, "DRY RUN")}.get(rc, _pill(RED, "FAILED"))


def _section(title, body, note=""):
    return '<h3 style="margin:26px 0 8px;font-size:18px;color:#24292f">%s</h3>%s%s' % (
        esc(title), '<p style="margin:0 0 8px;color:#57606a;font-size:15px">%s</p>' % note if note else "", body)


def _table(head, rows, right=()):
    h = "".join("<th %s>%s</th>" % (TH, esc(x)) for x in head)
    b = "".join("<tr>%s</tr>" % "".join("<td %s>%s</td>" % (TDR if n in right else TD, c) for n, c in enumerate(r))
                for r in rows)
    return ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%%;font-size:16px">'
            '<tr>%s</tr>%s</table>' % (h, b))


def _tile(label, value, sub=""):
    return ('<td style="padding:10px 12px;background:#f6f8fa;border-radius:6px;width:25%%;vertical-align:top">'
            '<div style="font-size:14px;color:#57606a">%s</div>'
            '<div style="font-size:21px;font-weight:600;color:#24292f">%s</div>'
            '<div style="font-size:14px;color:#57606a">%s</div></td>' % (esc(label), esc(value), esc(sub)))


def report_html(st):
    errors = st["errors"]
    banner_c = GREEN if errors == 0 else RED
    banner_t = "&#10004; Backup OK" if errors == 0 else "&#10008; %d error%s" % (errors, "" if errors == 1 else "s")
    p = ['<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:1200px;margin:0 auto;color:#24292f">',
         '<div style="background:%s;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">'
         '<div style="font-size:15px;opacity:.9">%s &middot; Shoebox &middot; %s</div>'
         '<div style="font-size:25px;font-weight:700">%s</div></div>' % (banner_c, esc(st["label"]), esc(st["started"]), banner_t),
         '<div style="border:1px solid #d0d7de;border-top:0;border-radius:0 0 8px 8px;padding:16px 20px">']
    if st["warnings"]:
        p.append('<div style="background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:10px 12px;'
                 'margin-bottom:12px;color:%s"><b>Warnings</b><br>%s</div>' % (AMBER, "<br>".join(esc(w) for w in st["warnings"])))
    ok = sum(1 for u in st["uploads"] if u["ok"])
    used, alloc = st.get("dropbox_used"), st.get("dropbox_allocated")
    space = "%.1f%% of Dropbox used" % (used * 100.0 / alloc) if used and alloc else ""
    p.append('<table cellspacing="6" cellpadding="0" style="width:100%%;border-collapse:separate"><tr>%s%s%s%s</tr></table>' % (
        _tile("Run", st["run"][-4:], st["run"][:10]),
        _tile("Size", human(st["size_bytes"]), "%d files" % len(st["uploads"])),
        _tile("Uploaded", "%d / %d" % (ok, len(st["uploads"])), space or "checksums verified"),
        _tile("Duration", dur(st["duration_s"]), "%s to %s" % (st["started"][11:16], st["finished"][11:16]))))
    if st["folders"]:
        rows = [[_code(f["label"]), _badge(f["rc"]), _code(f["path"]),
                 human(f["bytes"]), dur(f["secs"])] for f in st["folders"]]
        p.append(_section("Folders", _table(["Archive", "Result", "Path", "Size", "Time"], rows, right=(3, 4)),
                          "Each folder is a plain <code>.tgz</code>: open it with any archive tool."))
    if st["databases"]:
        rows = [[_code("%s/%s" % (d["source"], d["db"])), _badge(d["rc"]),
                 "%s / %s" % (d.get("tables"), d.get("expected")), human(d.get("bytes")), dur(d.get("secs")),
                 esc(d.get("problem", ""))] for d in st["databases"]]
        p.append(_section("Databases", _table(["Database", "Result", "Tables", "Size", "Time", ""], rows, right=(2, 3, 4)),
                          "Plain <code>.sql.gz</code>. Good only if the dump exits 0, ends with "
                          "<code>-- Dump completed</code> and has as many tables as the database."))
    bad = [u for u in st["uploads"] if not u["ok"]]
    note = "Every file was checked against Dropbox's content hash after upload." if not bad else ""
    rows = [[_code(u["file"]), _badge(u["ok"]), esc(u["detail"])] for u in bad] or \
           [["%d files to %s" % (ok, _code(st["dropbox_dir"])), _badge(True), ""]]
    p.append(_section("Dropbox", _table(["Files", "Result", ""], rows), note))
    r = st.get("retention") or {}
    if r:
        gone = r.get("deleted") or []
        p.append(_section("Retention", _table(["Policy", "Deleted", "Kept"], [[
            esc(r.get("policy", "")), esc(", ".join(gone)) or "none", esc(str(r.get("kept") if r.get("kept") is not None else "-"))]])))
    p.append('<p style="margin-top:22px;font-size:14px;color:#8c959f">Sent by Shoebox %s on %s. Log: %s</p></div></div>'
             % (esc(st["shoebox"]), esc(st["host"]), esc(st["log"])))
    return ("<!doctype html><html><body style='margin:0;padding:12px;background:#ffffff'>%s</body></html>" % "".join(p))


# ------------------------------------------------------------------------------------------ commands
def cmd_auth(cfg, env_path, args):
    """Dropbox OAuth2 with PKCE and no redirect: open a link, approve, paste the code. Stores a refresh token."""
    key = cfg.dropbox_app_key or input("Dropbox App key: ").strip()
    if not key:
        raise ShoeboxError("no app key")
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": key, "response_type": "code", "code_challenge": challenge,
        "code_challenge_method": "S256", "token_access_type": "offline"})
    print("\n1. Open this link (any device), sign in to Dropbox and click Allow:\n\n   %s\n" % url)
    print("2. Dropbox shows an access code. Paste it here (it is not shown on screen).")
    import getpass
    code = getpass.getpass("   Access code: ").strip()
    if not code:
        raise ShoeboxError("no code entered")
    body = urllib.parse.urlencode({"code": code, "grant_type": "authorization_code",
                                   "code_verifier": verifier, "client_id": key}).encode()
    try:
        r = urllib.request.urlopen(urllib.request.Request(Dropbox.TOKEN, data=body, method="POST"), timeout=60)
    except urllib.error.HTTPError as e:
        raise ShoeboxError("Dropbox refused the code: %s" % e.read().decode("utf-8", "replace")[:300])
    tok = json.loads(r.read())
    if "refresh_token" not in tok:
        raise ShoeboxError("Dropbox did not return a refresh token")
    if not cfg.dropbox_app_key:
        save_env_value(env_path, "DROPBOX_APP_KEY", key)
    save_env_value(env_path, "DROPBOX_REFRESH_TOKEN", tok["refresh_token"])
    print("\nSaved DROPBOX_REFRESH_TOKEN to %s (mode 600). Scopes granted: %s" % (env_path, tok.get("scope", "?")))
    dbx = Dropbox(key, tok["refresh_token"])
    acct = dbx.rpc("/users/get_current_account")
    dbx.list_folder("")  # proves files.metadata.read
    print("Connected to the Dropbox account of %s. Run `shoebox.py check` next." % acct["name"]["display_name"])
    return 0


def cmd_check(cfg, args):
    print("Config  %s" % cfg.path)
    print("Label   %s   Dropbox folder %s (app folder)   Local %s (keep %d days)"
          % (cfg.label, cfg.dropbox_folder, cfg.local_folder, cfg.local_keep_days))
    for w in cfg.warnings:
        print("WARN    %s" % w)
    bad = 0
    for it in cfg.folder_items():
        ok = os.path.isdir(it["path"])
        bad += not ok
        print("%-7s folder %-30s %s%s" % ("ok" if ok else "MISSING", it["label"], it["path"],
                                         "  (excl %s)" % ", ".join(it["excludes"]) if it["excludes"] else ""))
    for s in cfg.db_servers:
        try:
            print("ok      db     %-30s %s  (%s)" % (s.name, ", ".join(s.databases()) or "no databases", s.describe()))
        except ShoeboxError as e:
            bad += 1
            print("FAIL    db     %-30s %s  (%s)" % (s.name, e, s.describe()))
    print("Retention %s" % (cfg.retention or "none (keep every run)"))
    print("Email   %s -> %s via %s:%s (%s), password %s" % (cfg.email["from"], cfg.email["to"], cfg.email["server"],
          cfg.email["port"], cfg.email["secure"] or "plain", "set" if cfg.email["password"] else "MISSING"))
    try:
        dbx = Dropbox(cfg.dropbox_app_key, cfg.dropbox_refresh_token, cfg.dropbox_app_secret)
        acct = dbx.rpc("/users/get_current_account")
        used, alloc = dbx.space()
        dbx.list_folder("")
        print("ok      Dropbox %s, %.1f%% of %s used" % (acct["name"]["display_name"], used * 100.0 / alloc, human(alloc)))
    except ShoeboxError as e:
        bad += 1
        print("FAIL    Dropbox: %s" % e)
    return bad


def remote_runs(dbx, cfg):
    runs = {}
    for m in dbx.list_folder(cfg.dropbox_folder):
        if m[".tag"] == "folder" and MONTH_RE.match(m["name"]):
            for e in dbx.list_folder(m["path_lower"]):
                if e[".tag"] == "folder" and RUN_RE.match(e["name"]):
                    runs[e["name"]] = e["path_display"]
    return runs


def cmd_list(cfg, args):
    dbx = Dropbox(cfg.dropbox_app_key, cfg.dropbox_refresh_token, cfg.dropbox_app_secret)
    runs = remote_runs(dbx, cfg)
    local = set(e for e in (os.listdir(cfg.local_folder) if os.path.isdir(cfg.local_folder) else []) if RUN_RE.match(e))
    for rid in sorted(set(runs) | local):
        size = sum(e.get("size", 0) for e in dbx.list_folder(runs[rid])) if rid in runs and args.sizes else None
        print("%s  %-8s %-6s %s" % (rid, "dropbox" if rid in runs else "", "local" if rid in local else "",
                                    human(size) if size is not None else ""))
    return 0


def cmd_restore(cfg, args):
    dbx = Dropbox(cfg.dropbox_app_key, cfg.dropbox_refresh_token, cfg.dropbox_app_secret)
    runs = remote_runs(dbx, cfg)
    if not runs:
        raise ShoeboxError("no runs in Dropbox %s" % cfg.dropbox_folder)
    rid = max(runs) if args.run == "latest" else args.run
    if rid not in runs:
        raise ShoeboxError("run %s not found (try `shoebox.py list`)" % rid)
    target = os.path.join(args.target, rid)
    os.makedirs(target, mode=0o700, exist_ok=True)
    entries = [e for e in dbx.list_folder(runs[rid]) if e[".tag"] == "file"]
    manifest = {}
    bad = 0
    for e in sorted(entries, key=lambda x: x["name"] != "manifest.json"):
        if args.match and e["name"] != "manifest.json" and not any(m in e["name"] for m in args.match):
            continue
        local = os.path.join(target, e["name"])
        dbx.download(e["path_display"], local)
        sha, ch = file_hashes(local)
        if e["name"] == "manifest.json":
            with open(local) as f:
                manifest = {x["file"]: x for x in json.load(f).get("files", [])}
            continue
        good = ch == e.get("content_hash") and (e["name"] not in manifest or manifest[e["name"]]["sha256"] == sha)
        bad += not good
        print("%-6s %s  %s" % ("ok" if good else "BAD", e["name"], human(e["size"])))
    print("Restored to %s. Folders: tar -xzf FILE -C DIR   Databases: zcat FILE.sql.gz | mysql DBNAME" % target)
    return bad


# ----------------------------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="shoebox.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=os.environ.get("SHOEBOX_CONFIG", os.path.join(HERE, "backupconfig.xml")),
                    help="backupconfig.xml (default: next to shoebox.py, or $SHOEBOX_CONFIG)")
    ap.add_argument("--env", help="secrets file (default: .env next to the config)")
    ap.add_argument("--version", action="version", version="shoebox " + VERSION)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="back up now")
    r.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    r.add_argument("--only", nargs="+", metavar="LABEL", help="only these folder labels / DB sources / DB names")
    r.add_argument("--no-folders", action="store_true")
    r.add_argument("--no-databases", "--no-mysql", dest="no_mysql", action="store_true", help="skip the databases")
    r.add_argument("--no-dropbox", action="store_true", help="keep the run local only")
    r.add_argument("--no-email", action="store_true")
    r.add_argument("--email-to", metavar="ADDR", help="send this run's report here instead of EMAIL_TO")
    sub.add_parser("auth", help="connect Dropbox (PKCE: open a link, paste the code)")
    sub.add_parser("check", help="check the config, folders, databases, email and Dropbox")
    l = sub.add_parser("list", help="list runs in Dropbox and locally")
    l.add_argument("--sizes", action="store_true", help="also add up each Dropbox run's size")
    s = sub.add_parser("restore", help="download a run from Dropbox and verify it")
    s.add_argument("run", help="run id (e.g. 2026-11-24-0030) or 'latest'")
    s.add_argument("--target", required=True, help="folder to download into")
    s.add_argument("--match", nargs="+", metavar="TEXT", help="only files whose name contains TEXT")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 64

    env_path = args.env or os.path.join(os.path.dirname(os.path.abspath(args.config)), ".env")
    try:
        cfg = Config(args.config, load_env(env_path))
        if args.cmd == "auth":
            return cmd_auth(cfg, env_path, args)
        if args.cmd == "check":
            return min(cmd_check(cfg, args), 255)
        if args.cmd == "list":
            return cmd_list(cfg, args)
        if args.cmd == "restore":
            return min(cmd_restore(cfg, args), 255)
        lock_dir = "/run/lock" if os.access("/run/lock", os.W_OK) else tempfile.gettempdir()
        lock = open(os.path.join(lock_dir, "shoebox-%s.lock" % cfg.label), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another shoebox run for %s is still going, not starting" % cfg.label, file=sys.stderr)
            return 1
        old = os.umask(0o077)
        try:
            return min(Run(cfg, args).execute(), 255)
        finally:
            os.umask(old)
    except ShoeboxError as e:
        print("shoebox: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
