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
LEGACY = """<?xml version="1.0"?>
<nubackups><config>
  <archive_label>OLD_BOX</archive_label><local_folder>/tmp/x/</local_folder><tar_exe>/bin/tar</tar_exe>
  <dropbox_chunksize>4194304</dropbox_chunksize>
  <email_server>mail.example.com</email_server><email_port>2525</email_port><email_secure>tls</email_secure>
  <email_auth>y</email_auth><email_username>u</email_username><email_password>inline-pass</email_password>
  <email_to>a@example.com</email_to><email_from>b@example.com</email_from><email_from_name>Sys</email_from_name>
  <mysql>n</mysql><mysql_host>localhost</mysql_host>
  <mysql_exe>mysqldump --add-drop-table --single-transaction --force </mysql_exe>
  <mysql_username>root</mysql_username><mysql_password>dbpass</mysql_password>
  <mysql_query>SELECT SCHEMA_NAME AS Dname FROM SCHEMATA</mysql_query>
</config><folder_list>
  <folder><folder_label>OPT</folder_label><folder_path>/opt/</folder_path><folder_split>n</folder_split>
    <folder_exclude>opt/hermes</folder_exclude><folder_split_exclude><exclude>TODELETE</exclude></folder_split_exclude></folder>
</folder_list></nubackups>"""


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
        self.assertEqual(cfg.docker_containers, ["app-db"])
        self.assertEqual(cfg.dropbox_folder, "/MYSERVER")
        self.assertEqual([f["label"] for f in cfg.folders], ["WWW", "ETC", "OPT"])
        self.assertEqual(cfg.folders[2]["excludes"], ["opt/big-thing"])
        self.assertEqual(cfg.warnings, [])

    def test_legacy_config_secrets_and_force(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), LEGACY), {})
        self.assertEqual(cfg.email["password"], "inline-pass")
        self.assertEqual(cfg.mysql_password, "dbpass")
        self.assertEqual(len(cfg.warnings), 3)  # email_password, mysql_password, --force
        self.assertNotIn("--force", cfg.dump_opts)
        for o in shoebox.REQUIRED_DUMP_OPTS:
            self.assertIn(o, cfg.dump_opts)
        self.assertEqual(cfg.dump_opts.count("--single-transaction"), 1)
        self.assertIsNone(cfg.retention)
        self.assertEqual(cfg.email["port"], 2525)

    def test_env_overrides_xml(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), LEGACY), {"SMTP_PASSWORD": "from-env"})
        self.assertEqual(cfg.email["password"], "from-env")

    def test_connections_from_env(self):
        env = {"SMTP_HOST": "smtp.env.test", "SMTP_PORT": "465", "SMTP_SECURE": "SSL", "SMTP_AUTH": "n",
               "SMTP_USERNAME": "envuser", "EMAIL_TO": "to@env.test", "EMAIL_FROM": "from@env.test",
               "EMAIL_FROM_NAME": "Env Box", "MYSQL_HOST": "db.env.test", "MYSQL_USERNAME": "backup"}
        with tempfile.TemporaryDirectory() as d:
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), LEGACY), env)
        e = cfg.email
        self.assertEqual((e["server"], e["port"], e["secure"], e["auth"]), ("smtp.env.test", 465, "ssl", False))
        self.assertEqual((e["username"], e["to"], e["from"], e["from_name"]),
                         ("envuser", "to@env.test", "from@env.test", "Env Box"))
        self.assertEqual((cfg.mysql_host, cfg.mysql_username), ("db.env.test", "backup"))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(shoebox.ShoeboxError):
                shoebox.Config(write(os.path.join(d, "c.xml"), LEGACY), {"SMTP_PORT": "abc"})

    def test_example_env_file(self):
        env = shoebox.load_env(os.path.join(ROOT, ".env.example"))
        cfg = shoebox.Config(os.path.join(ROOT, "backupconfig.example.xml"), env)
        self.assertEqual(cfg.email["server"], "smtp.example.com")
        self.assertEqual(cfg.email["secure"], "tls")
        self.assertEqual(cfg.mysql_username, "root")
        self.assertEqual(cfg.warnings, [])

    def test_bad_label(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, "c.xml"), LEGACY.replace("OLD_BOX", "bad label/.."))
            with self.assertRaises(shoebox.ShoeboxError):
                shoebox.Config(p, {})

    def test_split_folders(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("siteA", "siteB", "TODELETE"):
                os.makedirs(os.path.join(d, "www", sub))
            write(os.path.join(d, "www", "index.html"), "hi")
            xml = LEGACY.replace("<folder_path>/opt/</folder_path><folder_split>n</folder_split>",
                                 "<folder_path>%s/www/</folder_path><folder_split>y</folder_split>" % d)
            cfg = shoebox.Config(write(os.path.join(d, "c.xml"), xml), {})
            labels = [i["label"] for i in cfg.folder_items()]
        self.assertEqual(labels, ["OPT-siteA", "OPT-siteB", "OPT-_top"])


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
            self.assertIn("# c", open(p).read())


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
        self.assertEqual(shoebox.plan_retention(["notes", "2026-11-24-0030", "2026-11-01-0030"], 0, 0, 0),
                         {"2026-11-24-0030"})


class HashTests(unittest.TestCase):
    def test_content_hash(self):
        with tempfile.TemporaryDirectory() as d:
            empty = write(os.path.join(d, "e"), "")
            self.assertEqual(shoebox.file_hashes(empty), (hashlib.sha256(b"").hexdigest(), hashlib.sha256(b"").hexdigest()))
            data = os.urandom(5 * 1024 * 1024 + 7)
            p = os.path.join(d, "f")
            with open(p, "wb") as f:
                f.write(data)
            b = 4 * 1024 * 1024
            want = hashlib.sha256(hashlib.sha256(data[:b]).digest() + hashlib.sha256(data[b:]).digest()).hexdigest()
            self.assertEqual(shoebox.file_hashes(p), (hashlib.sha256(data).hexdigest(), want))


class DumpTests(unittest.TestCase):
    GOOD = "printf '%s\\n' '-- header' 'CREATE TABLE `a` (' ');' 'INSERT INTO a VALUES (1);' 'CREATE TABLE `b` (' ');' '-- Dump completed on 2026-11-24  0:30:01'"

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
            xml = LEGACY.replace("/tmp/x/", os.path.join(d, "out")) \
                        .replace("<folder_path>/opt/</folder_path>", "<folder_path>%s</folder_path>" % src) \
                        .replace("<folder_exclude>opt/hermes</folder_exclude>", "<folder_exclude>*/skip</folder_exclude>") \
                        .replace("<tar_exe>/bin/tar</tar_exe>", "<tar_exe>tar</tar_exe><status_file>%s/st.json</status_file>" % d)
            cfgp = write(os.path.join(d, "c.xml"), xml)
            rc = shoebox.main(["--config", cfgp, "--env", os.path.join(d, "none"), "run", "--no-dropbox", "--no-email"])
            self.assertEqual(rc, 0)
            st = json.load(open(os.path.join(d, "st.json")))
            self.assertEqual(st["errors"], 0)
            self.assertEqual(len(st["folders"]), 1)
            run_dir = st["local_dir"]
            man = json.load(open(os.path.join(run_dir, "manifest.json")))
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
            xml = LEGACY.replace("/tmp/x/", os.path.join(d, "out")) \
                        .replace("<folder_path>/opt/</folder_path>", "<folder_path>%s</folder_path>" % d)
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
