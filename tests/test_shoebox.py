"""Shoebox tests: standard library unittest, no network, no database.  python3 -m unittest discover -s tests"""

import datetime as dt
import gzip
import hashlib
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import shoebox  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BASE = """<?xml version="1.0"?>
<shoebox><config>
  <archive_label>OLD_BOX</archive_label><local_folder>/tmp/x/</local_folder><tar_exe>/bin/tar</tar_exe>
  <dropbox_chunksize>4194304</dropbox_chunksize>
</config><folder_list>
  <folder><folder_label>OPT</folder_label><folder_path>/opt/</folder_path><folder_split>n</folder_split>
    <folder_exclude>opt/hermes</folder_exclude><folder_split_exclude><exclude>TODELETE</exclude></folder_split_exclude></folder>
</folder_list></shoebox>"""


def with_dbs(servers, base=None):
    """BASE plus a <databases> section."""
    return (base or BASE).replace("</folder_list>", "</folder_list><databases>%s</databases>" % servers)


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    return path


class ConfigTests(unittest.TestCase):
    def test_example_config(self):
        cfg = shoebox.Config(os.path.join(ROOT, "backupconfig.example.xml"), {"SMTP_PASSWORD": "x"})
        self.assertEqual(cfg.label, "MYSERVER")
        self.assertEqual(cfg.chunk_size, 32 * 1024 * 1024)
        self.assertEqual(cfg.retention, {"daily": 14, "weekly": 8, "monthly": 12})
        self.assertEqual([d.name for d in cfg.db_servers], ["host", "shop", "crm"])
        host, shop, crm = cfg.db_servers
        self.assertEqual((host.host, host.container, host.username, host.password), ("localhost", "", "root", None))
        self.assertEqual((shop.container, shop.password_container_env), ("shop-db", "MARIADB_ROOT_PASSWORD"))
        self.assertEqual((crm.password_env, crm.error), ("CRM_DB_PASSWORD", "CRM_DB_PASSWORD is not set in .env"))
        self.assertEqual(cfg.dropbox_folder, "/MYSERVER")
        self.assertEqual([f["label"] for f in cfg.folders], ["WWW", "ETC", "OPT"])
        self.assertEqual(cfg.folders[2]["excludes"], ["opt/big-thing", "*.tmp"])
        self.assertEqual([f["split"] for f in cfg.folders], [True, False, False])
        self.assertEqual(cfg.warnings, [])

    def bad_xml(self, xml, text, env=None):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(shoebox.ShoeboxError) as e:
                shoebox.Config(write(os.path.join(d, "c.xml"), xml), env or {})
        self.assertIn(text, str(e.exception))

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), BASE), {})
        self.assertEqual(cfg.db_servers, [])
        self.assertIsNone(cfg.retention)
        self.assertEqual((cfg.email["server"], cfg.email["port"], cfg.email["auth"]), ("", 587, True))
        self.assertEqual(cfg.warnings, [])

    def test_unknown_elements_are_refused(self):
        self.bad_xml(
            BASE.replace("</config>", "<email_server>m</email_server></config>"),
            "unknown element <email_server> in <shoebox><config>: the report email settings go in .env",
        )
        self.bad_xml(BASE.replace("</config>", "<mysql>y</mysql></config>"), "<database_server> inside <databases>")
        self.bad_xml(
            BASE.replace("</config>", "<retention><dayly>3</dayly></retention></config>"),
            "unknown element <dayly> in <shoebox><config><retention>",
        )
        self.bad_xml(BASE.replace("<folder_split>n</folder_split>", "<split>n</split>"), "unknown element <split>")
        self.bad_xml(
            BASE.replace("<shoebox>", "<nubackups>").replace("</shoebox>", "</nubackups>"),
            "top element must be <shoebox>",
        )
        with tempfile.TemporaryDirectory() as d:  # comments are fine
            shoebox.Config(write(os.path.join(d, "c.xml"), BASE.replace("<config>", "<config><!-- hi -->")), {})

    def test_yes_no_values(self):
        for word, want in (
            ("y", True),
            ("YES", True),
            ("true", True),
            ("1", True),
            ("n", False),
            ("No", False),
            ("false", False),
            ("", False),
        ):
            with tempfile.TemporaryDirectory() as d:
                xml = BASE.replace("<folder_split>n</folder_split>", "<folder_split>%s</folder_split>" % word)
                cfg = shoebox.Config(write(os.path.join(d, "c.xml"), xml), {})
            self.assertEqual(cfg.folders[0]["split"], want, word)
        self.bad_xml(BASE.replace("<folder_split>n", "<folder_split>maybe"), "must be yes or no")
        self.bad_xml(BASE, "SMTP_AUTH in .env must be yes or no", {"SMTP_AUTH": "sometimes"})

    def test_bad_email_env(self):
        self.bad_xml(BASE, "SMTP_PORT in .env must be a number", {"SMTP_PORT": "abc"})
        self.bad_xml(BASE, "SMTP_SECURE in .env must be tls, ssl or empty", {"SMTP_SECURE": "starttls"})

    def test_bad_folder(self):
        self.bad_xml(BASE.replace("<folder_label>OPT</folder_label>", ""), "needs a <folder_path> and a <folder_label>")
        self.bad_xml(BASE.replace("<folder_label>OPT", "<folder_label>my files"), "needs a <folder_path>")

    def test_connections_from_env(self):
        env = {
            "SMTP_HOST": "smtp.env.test",
            "SMTP_PORT": "465",
            "SMTP_SECURE": "SSL",
            "SMTP_AUTH": "n",
            "SMTP_USERNAME": "envuser",
            "EMAIL_TO": "to@env.test",
            "EMAIL_FROM": "from@env.test",
            "EMAIL_FROM_NAME": "Env Box",
        }
        with tempfile.TemporaryDirectory() as d:
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), BASE), env)
        e = cfg.email
        self.assertEqual((e["server"], e["port"], e["secure"], e["auth"]), ("smtp.env.test", 465, "ssl", False))
        self.assertEqual(
            (e["username"], e["to"], e["from"], e["from_name"]), ("envuser", "to@env.test", "from@env.test", "Env Box")
        )

    def test_example_env_file(self):
        env = shoebox.load_env(os.path.join(ROOT, ".env.example"))
        cfg = shoebox.Config(os.path.join(ROOT, "backupconfig.example.xml"), env)
        self.assertEqual(cfg.email["server"], "smtp.example.com")
        self.assertEqual(cfg.email["secure"], "tls")
        self.assertEqual(cfg.warnings, [])

    def test_bad_label(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.xml"), BASE.replace("OLD_BOX", "bad label/.."))
            with self.assertRaises(shoebox.ShoeboxError):
                shoebox.Config(p, {})

    def test_split_folders(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("siteA", "siteB", "TODELETE"):
                os.makedirs(os.path.join(d, "www", sub))
            write(os.path.join(d, "www", "index.html"), "hi")
            xml = BASE.replace(
                "<folder_path>/opt/</folder_path><folder_split>n</folder_split>",
                "<folder_path>%s/www/</folder_path><folder_split>y</folder_split>" % d,
            )
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), xml), {})
            labels = [i["label"] for i in cfg.folder_items()]
        self.assertEqual(labels, ["OPT-siteA", "OPT-siteB", "OPT-_top"])


class DatabaseServerTests(unittest.TestCase):
    def cfg(self, servers, env=None):
        with tempfile.TemporaryDirectory() as d:
            return shoebox.Config(write(os.path.join(d, "c.xml"), with_dbs(servers)), env or {})

    def bad(self, servers, text):
        with self.assertRaises(shoebox.ShoeboxError) as e:
            self.cfg(servers)
        self.assertIn(text, str(e.exception))

    def test_invalid_servers(self):
        self.bad("<database_server><host>h</host><username>u</username></database_server>", "<label>")
        self.bad("<database_server><label>a</label><username>u</username></database_server>", "exactly one")
        self.bad(
            "<database_server><label>a</label><host>h</host><docker_container>c</docker_container>"
            "<username>u</username></database_server>",
            "exactly one",
        )
        self.bad("<database_server><label>a</label><host>h</host></database_server>", "<username>")
        self.bad(
            "<database_server><label>a</label><host>h</host><username>u</username>"
            "<password>x</password></database_server>",
            "do not go in the XML",
        )
        self.bad(
            "<database_server><label>a</label><host>h</host><username>u</username>"
            "<password_container_env>P</password_container_env></database_server>",
            "only goes with",
        )
        self.bad(
            "<database_server><label>a</label><docker_container>c</docker_container><username>u</username>"
            "<password_env>P</password_env><password_container_env>Q</password_container_env>"
            "</database_server>",
            "not both",
        )
        self.bad(
            "<database_server><label>a</label><host>h</host><username>u</username>"
            "<password_env>bad name</password_env></database_server>",
            "variable name",
        )
        self.bad(
            "<database_server><label>a</label><host>h</host><port>x</port><username>u</username>" "</database_server>",
            "<port>",
        )
        self.bad(
            "<database_server><label>a</label><docker_container>c; rm</docker_container>"
            "<username>u</username></database_server>",
            "container name",
        )
        one = "<database_server><label>a</label><host>h</host><username>u</username></database_server>"
        self.bad(one + one, "unique")

    def test_dump_options(self):
        cfg = self.cfg(
            "<database_server><label>a</label><host>h</host><username>u</username>"
            "<mysql_exe>/usr/bin/mysqldump --single-transaction --force</mysql_exe></database_server>"
        )
        srv = cfg.db_servers[0]
        self.assertEqual(srv.exe, "/usr/bin/mysqldump")
        self.assertNotIn("--force", srv.dump_opts)
        for o in shoebox.REQUIRED_DUMP_OPTS:
            self.assertIn(o, srv.dump_opts)
        self.assertEqual(srv.dump_opts.count("--single-transaction"), 1)
        self.assertEqual(len([w for w in cfg.warnings if "--force" in w]), 1)
        self.assertEqual(srv.query_sql, shoebox.DEFAULT_DB_QUERY)

    def test_passwords_never_on_the_command_line(self):
        cfg = self.cfg(
            "<database_server><label>remote</label><host>db.test</host><port>3307</port><username>bk</username>"
            "<password_env>REMOTE_PW</password_env></database_server>"
            "<database_server><label>app</label><docker_container>app-db</docker_container><username>bk</username>"
            "<password_env>APP_PW</password_env><mysql_exe>mariadb-dump</mysql_exe></database_server>"
            "<database_server><label>shop</label><docker_container>shop-db</docker_container><username>root</username>"
            "<password_container_env>MARIADB_ROOT_PASSWORD</password_container_env></database_server>"
            "<database_server><label>sock</label><host>localhost</host><username>root</username></database_server>"
            "<database_server><label>nopw</label><docker_container>open-db</docker_container><username>root</username>"
            "</database_server>",
            {"REMOTE_PW": "s3cret-remote", "APP_PW": "s3cret-app"},
        )
        remote, app, shop, sock, nopw = cfg.db_servers
        cmd = remote.dump_cmd("crm")
        self.assertEqual(cmd[:7], ["mysqldump", "-h", "db.test", "-P", "3307", "-u", "bk"])
        self.assertEqual(cmd[-1], "crm")
        self.assertNotIn("s3cret-remote", " ".join(cmd))
        self.assertEqual(remote._env()["MYSQL_PWD"], "s3cret-remote")
        cmd = app.dump_cmd("app")
        self.assertEqual(cmd[:6], ["docker", "exec", "-e", "MYSQL_PWD", "app-db", "sh"])
        self.assertIn("mariadb-dump", cmd)
        self.assertNotIn("s3cret-app", " ".join(cmd))
        self.assertEqual(app._env()["MYSQL_PWD"], "s3cret-app")
        cmd = shop.dump_cmd("shop")
        self.assertNotIn("-e", cmd[:3])
        self.assertIn('MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-}"', cmd[5])
        self.assertNotIn("MYSQL_PWD", shop._env())
        self.assertEqual(sock.dump_cmd("x")[:5], ["mysqldump", "-h", "localhost", "-u", "root"])
        self.assertNotIn("MYSQL_PWD", sock._env())
        self.assertTrue(nopw.dump_cmd("x")[5].startswith("unset MYSQL_PWD; "))

    def test_missing_env_password_fails_only_that_server(self):
        srv = self.cfg(
            "<database_server><label>a</label><host>h</host><username>u</username>"
            "<password_env>NOPE</password_env></database_server>"
        ).db_servers[0]
        with self.assertRaises(shoebox.ShoeboxError) as e:
            srv.databases()
        self.assertIn("NOPE is not set in .env", str(e.exception))

    def test_run_dumps_with_fake_client(self):
        """A full local run against fake mysql/mysqldump programs that check the password arrives in MYSQL_PWD."""
        with tempfile.TemporaryDirectory() as d:
            bindir = os.path.join(d, "bin")
            os.makedirs(bindir)
            write(
                os.path.join(bindir, "mysql"),
                '#!/bin/sh\n[ "$MYSQL_PWD" = pw123 ] || { echo "Access denied" >&2; exit 1; }\n'
                'case "$*" in *"COUNT(*)"*) echo 2 ;; *) printf "blog\\nshop\\n" ;; esac\n',
            )
            write(
                os.path.join(bindir, "mysqldump"),
                '#!/bin/sh\n[ "$MYSQL_PWD" = pw123 ] || exit 1\n'
                'printf "CREATE TABLE `a` (\\n);\\nCREATE TABLE `b` (\\n);\\n-- Dump completed on now\\n"\n',
            )
            for f in ("mysql", "mysqldump"):
                os.chmod(os.path.join(bindir, f), 0o755)
            xml = with_dbs(
                "<database_server><label>main</label><host>localhost</host><username>bk</username>"
                "<password_env>DB_PW</password_env></database_server>",
                BASE.replace("/tmp/x/", os.path.join(d, "out"))
                .replace("<folder_path>/opt/</folder_path>", "<folder_path>%s</folder_path>" % bindir)
                .replace(
                    "<tar_exe>/bin/tar</tar_exe>", "<tar_exe>tar</tar_exe><status_file>%s/st.json</status_file>" % d
                ),
            )
            cfgp = write(os.path.join(d, "c.xml"), xml)
            envp = write(os.path.join(d, ".env"), "DB_PW=pw123\n")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = bindir + os.pathsep + old_path
            try:
                rc = shoebox.main(["--config", cfgp, "--env", envp, "run", "--no-dropbox", "--no-email"])
            finally:
                os.environ["PATH"] = old_path
            st = read_json(os.path.join(d, "st.json"))
            self.assertEqual((rc, st["errors"]), (0, 0))
            self.assertEqual(
                [(x["source"], x["db"], x["tables"], x["expected"]) for x in st["databases"]],
                [("main", "blog", 2, 2), ("main", "shop", 2, 2)],
            )
            self.assertTrue(os.path.exists(os.path.join(st["local_dir"], st["databases"][0]["file"])))
            self.assertTrue(st["databases"][0]["file"].endswith("-main-blog.sql.gz"))


class PathTests(unittest.TestCase):
    """Paths in the XML work with or without a trailing slash, through symlinks, and never loop."""

    def make(self, d, folders, local=None):
        xml = """<shoebox><config><archive_label>BOX</archive_label><local_folder>%s</local_folder></config>
            <folder_list>%s</folder_list></shoebox>""" % (local or os.path.join(d, "out"), folders)
        return write(os.path.join(d, "c.xml"), xml)

    def members(self, d, label):
        run = [e for e in os.listdir(os.path.join(d, "out")) if shoebox.RUN_RE.match(e)][0]
        name = [f for f in os.listdir(os.path.join(d, "out", run)) if "-%s-dir" % label in f][0]
        with tarfile.open(os.path.join(d, "out", run, name)) as t:
            return sorted(m.name for m in t.getmembers())

    def backup(self, cfgp):
        return shoebox.main(["-c", cfgp, "--env", "/nonexistent", "run", "--no-dropbox", "--no-email"])

    def test_tidy_paths(self):
        self.assertEqual(shoebox.clean_path(" /var//www/html/ ", "x"), "/var/www/html")
        self.assertEqual(shoebox.clean_path("//srv/./a/../b", "x"), "/srv/b")
        self.assertEqual(shoebox.clean_path("~/x", "x"), os.path.join(os.path.expanduser("~"), "x"))
        with self.assertRaises(shoebox.ShoeboxError) as e:
            shoebox.clean_path("var/www", "<folder_path> of WWW")
        self.assertIn("must be an absolute path", str(e.exception))
        self.assertEqual(shoebox.clean_pattern(" /opt/big/ "), "opt/big")

    def test_trailing_slash_makes_no_difference(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src", "big"))
            write(os.path.join(d, "src", "a.txt"), "a")
            write(os.path.join(d, "src", "big", "b.txt"), "b")
            src = os.path.join(d, "src")
            folders = (
                "<folder><folder_label>A</folder_label><folder_path>%s</folder_path>"
                "<folder_exclude>%s/big/</folder_exclude></folder>"
                "<folder><folder_label>B</folder_label><folder_path>%s//</folder_path>"
                "<folder_exclude>/%s/big</folder_exclude></folder>" % (src, src.strip("/"), src, src.strip("/"))
            )
            self.assertEqual(self.backup(self.make(d, folders)), 0)
            a, b = self.members(d, "A"), self.members(d, "B")
            self.assertEqual(a, b)
            self.assertIn(src.strip("/") + "/a.txt", a)
            self.assertFalse([m for m in a if "big" in m])

    def test_global_exclude_drops_env_files_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(os.path.join(src, "site", "deep"))
            for rel in (".env", "keep.txt", "site/.env", "site/.env.bak-1", "site/deep/.env-live", "site/app.php"):
                write(os.path.join(src, rel), "x")
            xml = """<shoebox><config><archive_label>BOX</archive_label><local_folder>%s</local_folder>
                <global_exclude>.env</global_exclude><global_exclude>.env.*</global_exclude>
                <global_exclude>/.env-*/</global_exclude></config><folder_list>
                <folder><folder_label>ALL</folder_label><folder_path>%s</folder_path></folder>
                <folder><folder_label>SPLIT</folder_label><folder_path>%s</folder_path><folder_split>y</folder_split>
                </folder></folder_list></shoebox>""" % (os.path.join(d, "out"), src, src)
            self.assertEqual(self.backup(write(os.path.join(d, "c.xml"), xml)), 0)
            for label in ("ALL", "SPLIT-site", "SPLIT-_top"):
                m = self.members(d, label)
                self.assertTrue(m, label)
                self.assertFalse([x for x in m if "/.env" in "/" + x], "%s: %s" % (label, m))
            self.assertIn("keep.txt", self.members(d, "SPLIT-_top"))
            self.assertIn(src.strip("/") + "/site/app.php", self.members(d, "SPLIT-site"))

    def test_symlinked_folder_is_followed(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "real", "skip"))
            write(os.path.join(d, "real", "a.txt"), "a")
            write(os.path.join(d, "real", "skip", "s.txt"), "s")
            link = os.path.join(d, "link")
            os.symlink(os.path.join(d, "real"), link)
            folders = (
                "<folder><folder_label>L</folder_label><folder_path>%s/</folder_path>"
                "<folder_exclude>%s/skip</folder_exclude></folder>" % (link, link.strip("/"))
            )
            self.assertEqual(self.backup(self.make(d, folders)), 0)
            m = self.members(d, "L")
            real = os.path.realpath(os.path.join(d, "real")).strip("/")
            self.assertIn(real + "/a.txt", m)
            self.assertFalse([x for x in m if "skip" in x])

    def test_local_folder_is_never_archived(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "site"))
            write(os.path.join(d, "site", "i.html"), "x")
            folders = "<folder><folder_label>ALL</folder_label><folder_path>%s</folder_path></folder>" % d
            self.assertEqual(self.backup(self.make(d, folders)), 0)
            self.assertFalse([m for m in self.members(d, "ALL") if "/out" in m])
            split = folders.replace("</folder_path>", "</folder_path><folder_split>yes</folder_split>")
            cfg = shoebox.Config(self.make(d, split), {})
            self.assertEqual([i["label"] for i in cfg.folder_items()], ["ALL-site", "ALL-_top"])
            inside = "<folder><folder_label>X</folder_label><folder_path>%s/out/sub</folder_path></folder>" % d
            with self.assertRaises(shoebox.ShoeboxError) as e:
                shoebox.Config(self.make(d, inside), {})
            self.assertIn("inside <local_folder>", str(e.exception))

    def test_bad_paths(self):
        with tempfile.TemporaryDirectory() as d:
            rel = "<folder><folder_label>R</folder_label><folder_path>var/www</folder_path></folder>"
            with self.assertRaises(shoebox.ShoeboxError):
                shoebox.Config(self.make(d, rel), {})
            xml = BASE.replace("</config>", "<log_file>%s</log_file></config>" % d)
            with self.assertRaises(shoebox.ShoeboxError) as e:
                shoebox.Config(write(os.path.join(d, "c.xml"), xml), {})
            self.assertIn("is a folder", str(e.exception))

    def test_second_run_in_the_same_minute_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src"))
            cfgp = self.make(d, "<folder><folder_label>S</folder_label><folder_path>%s/src</folder_path></folder>" % d)
            cfg = shoebox.Config(cfgp, {})
            args = shoebox.argparse.Namespace(
                dry_run=False, only=None, no_folders=False, no_mysql=True, no_dropbox=True, no_email=True, email_to=None
            )
            r = shoebox.Run(cfg, args)
            os.makedirs(r.local_dir)
            with self.assertRaises(shoebox.ShoeboxError) as e:
                r.execute()
            self.assertIn("already exists", str(e.exception))


class LogRotationTests(unittest.TestCase):
    def test_rotate_keeps_n(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "shoebox.log")
            for i in range(5):
                write(log, "run %d\n" % i + "x" * 100)
                self.assertTrue(shoebox.rotate_log(log, 50, 3))
            self.assertEqual(sorted(os.listdir(d)), ["shoebox.log.1", "shoebox.log.2", "shoebox.log.3"])
            with open(log + ".1") as f:
                self.assertTrue(f.read().startswith("run 4"))
            with open(log + ".3") as f:
                self.assertTrue(f.read().startswith("run 2"))
            write(log, "small")
            self.assertFalse(shoebox.rotate_log(log, 50, 3))
            write(log, "x" * 100)
            self.assertTrue(shoebox.rotate_log(log, 50, 0))
            self.assertFalse(os.path.exists(log))

    def test_settings(self):
        with tempfile.TemporaryDirectory() as d:
            xml = BASE.replace("</config>", "<log_max_kb>2048</log_max_kb><log_keep>3</log_keep></config>")
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), xml), {})
        self.assertEqual((cfg.log_max_bytes, cfg.log_keep), (2048 * 1024, 3))


class SpaceTests(unittest.TestCase):
    def test_low_local_space_warns(self):
        with tempfile.TemporaryDirectory() as d:
            xml = BASE.replace("/tmp/x/", d).replace("</config>", "<status_file>%s/st.json</status_file></config>" % d)
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), xml), {})
            r = shoebox.Run(cfg, None)
            write(os.path.join(d, "st.json"), json.dumps({"size_bytes": 1}))
            r.check_local_space()
            self.assertEqual(r.warnings, [])
            write(os.path.join(d, "st.json"), json.dumps({"size_bytes": 10**18}))
            r.check_local_space()
            self.assertIn("free in", r.warnings[0])


class DefaultConfigTests(unittest.TestCase):
    def test_lookup_order(self):
        old_env, old_here = os.environ.pop("SHOEBOX_CONFIG", None), shoebox.HERE
        try:
            with tempfile.TemporaryDirectory() as d:
                shoebox.HERE = d
                self.assertEqual(shoebox.default_config(), shoebox.ETC_CONFIG)
                write(os.path.join(d, "backupconfig.xml"), BASE)
                self.assertEqual(shoebox.default_config(), os.path.join(d, "backupconfig.xml"))
                os.environ["SHOEBOX_CONFIG"] = "/x/y.xml"
                self.assertEqual(shoebox.default_config(), "/x/y.xml")
        finally:
            shoebox.HERE = old_here
            os.environ.pop("SHOEBOX_CONFIG", None)
            if old_env is not None:
                os.environ["SHOEBOX_CONFIG"] = old_env


class EnvTests(unittest.TestCase):
    def test_load_and_save(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, ".env"), "# c\nA=1\nexport B='two words'\nC=\"3\"\n\nbad line\n")
            self.assertEqual(shoebox.load_env(p), {"A": "1", "B": "two words", "C": "3"})
            shoebox.save_env_value(p, "B", "new")
            shoebox.save_env_value(p, "D", "4")
            env = shoebox.load_env(p)
            self.assertEqual((env["A"], env["B"], env["D"]), ("1", "new", "4"))
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            with open(p) as f:
                self.assertIn("# c", f.read())


class RetentionTests(unittest.TestCase):
    def runs(self, days, start=dt.datetime(2026, 11, 24, 0, 30)):
        return ["%s" % (start - dt.timedelta(days=i)).strftime("%Y-%m-%d-%H%M") for i in range(days)]

    def test_gfs(self):
        runs = self.runs(400)
        keep = shoebox.plan_retention(runs, 14, 8, 12)
        self.assertIn(runs[0], keep)
        for r in runs[:14]:
            self.assertIn(r, keep)
        self.assertLessEqual(len(keep), 14 + 8 + 12)
        self.assertGreaterEqual(len(keep), 25)
        months = {r[:7] for r in keep}
        self.assertEqual(len(months), 12)
        # applying it again keeps the same set
        self.assertEqual(shoebox.plan_retention(sorted(keep), 14, 8, 12), keep)

    def test_two_runs_a_day_keeps_newest(self):
        runs = ["2026-11-24-0030", "2026-11-24-1600", "2026-11-23-0030"]
        self.assertEqual(shoebox.plan_retention(runs, 2, 0, 0), {"2026-11-24-1600", "2026-11-23-0030"})

    def test_ignores_other_names_and_zero_policy(self):
        self.assertEqual(
            shoebox.plan_retention(["notes", "2026-11-24-0030", "2026-11-01-0030"], 0, 0, 0), {"2026-11-24-0030"}
        )


class HashTests(unittest.TestCase):
    def test_content_hash(self):
        with tempfile.TemporaryDirectory() as d:
            empty = write(os.path.join(d, "e"), "")
            self.assertEqual(
                shoebox.file_hashes(empty), (hashlib.sha256(b"").hexdigest(), hashlib.sha256(b"").hexdigest())
            )
            data = os.urandom(5 * 1024 * 1024 + 7)
            p = os.path.join(d, "f")
            with open(p, "wb") as f:
                f.write(data)
            b = 4 * 1024 * 1024
            want = hashlib.sha256(hashlib.sha256(data[:b]).digest() + hashlib.sha256(data[b:]).digest()).hexdigest()
            self.assertEqual(shoebox.file_hashes(p), (hashlib.sha256(data).hexdigest(), want))


class DumpTests(unittest.TestCase):
    GOOD = (
        "printf '%s\\n' '-- header' 'CREATE TABLE `a` (' ');' 'INSERT INTO a VALUES (1);' "
        "'CREATE TABLE `b` (' ');' '-- Dump completed on 2026-11-24  0:30:01'"
    )

    def dump(self, script):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "x.sql.gz")
            res = shoebox.dump_to_gzip(["sh", "-c", script], out)
            with gzip.open(out, "rt") as f:
                body = f.read()
        return res, body

    def test_good(self):
        (rc, creates, last, err), body = self.dump(self.GOOD)
        self.assertEqual((rc, creates), (0, 2))
        self.assertIn("INSERT INTO a", body)
        self.assertEqual(shoebox.check_dump(rc, creates, last, 2), "")

    def test_bad_cases(self):
        (rc, creates, last, _), _b = self.dump(self.GOOD)
        self.assertIn("tables", shoebox.check_dump(rc, creates, last, 3))
        self.assertIn("tables", shoebox.check_dump(rc, creates, last, None))
        (rc, creates, last, _), _b = self.dump("printf 'CREATE TABLE `a` (\\n'")
        self.assertIn("Dump completed", shoebox.check_dump(rc, creates, last, 1))
        (rc, creates, last, err), _b = self.dump(self.GOOD + "; echo boom >&2; exit 2")
        self.assertEqual(rc, 2)
        self.assertIn("exit code 2", shoebox.check_dump(rc, creates, last, 2))
        self.assertEqual(err, "boom")


class RunTests(unittest.TestCase):
    def test_local_run_no_dropbox(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(os.path.join(src, "keep"))
            os.makedirs(os.path.join(src, "skip"))
            write(os.path.join(src, "keep", "a.txt"), "hello")
            write(os.path.join(src, "skip", "b.txt"), "no")
            old = os.path.join(d, "out", "2020-01-01-0030")
            os.makedirs(old)
            xml = (
                BASE.replace("/tmp/x/", os.path.join(d, "out"))
                .replace("<folder_path>/opt/</folder_path>", "<folder_path>%s</folder_path>" % src)
                .replace("<folder_exclude>opt/hermes</folder_exclude>", "<folder_exclude>*/skip</folder_exclude>")
                .replace(
                    "<tar_exe>/bin/tar</tar_exe>", "<tar_exe>tar</tar_exe><status_file>%s/st.json</status_file>" % d
                )
            )
            cfgp = write(os.path.join(d, "c.xml"), xml)
            rc = shoebox.main(["--config", cfgp, "--env", os.path.join(d, "none"), "run", "--no-dropbox", "--no-email"])
            self.assertEqual(rc, 0)
            st = read_json(os.path.join(d, "st.json"))
            self.assertEqual(st["errors"], 0)
            self.assertEqual(len(st["folders"]), 1)
            run_dir = st["local_dir"]
            man = read_json(os.path.join(run_dir, "manifest.json"))
            tgz = os.path.join(run_dir, man["files"][0]["file"])
            names = tarfile.open(tgz).getnames()
            self.assertTrue(any(n.endswith("keep/a.txt") for n in names))
            self.assertFalse(any("skip" in n for n in names))
            self.assertEqual(man["files"][0]["sha256"], shoebox.file_hashes(tgz)[0])
            self.assertFalse(os.path.exists(old))  # older than local_keep_days
            self.assertIn("Errors:0", st["subject"])
            html_page = shoebox.report_html(st)
            self.assertIn("Backup OK", html_page)
            self.assertIn("OPT", shoebox.report_text(st))

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            xml = BASE.replace("/tmp/x/", os.path.join(d, "out")).replace(
                "<folder_path>/opt/</folder_path>", "<folder_path>%s</folder_path>" % d
            )
            cfgp = write(os.path.join(d, "c.xml"), xml)
            rc = shoebox.main(["--config", cfgp, "--env", os.path.join(d, "none"), "run", "--dry-run", "--no-dropbox"])
            self.assertEqual(rc, 0)
            self.assertEqual([e for e in os.listdir(os.path.join(d, "out")) if shoebox.RUN_RE.match(e)], [])


class FormatTests(unittest.TestCase):
    def test_human_and_dur(self):
        self.assertEqual(shoebox.human(999), "999 B")
        self.assertEqual(shoebox.human(1500), "1.5 KB")
        self.assertEqual(shoebox.human(2.5e6), "2.5 MB")
        self.assertEqual(shoebox.human(2.92e9), "2.92 GB")
        self.assertEqual(shoebox.dur(59), "59s")
        self.assertEqual(shoebox.dur(125), "2m 05s")
        self.assertEqual(shoebox.dur(3700), "1h 01m")


if __name__ == "__main__":
    unittest.main()
