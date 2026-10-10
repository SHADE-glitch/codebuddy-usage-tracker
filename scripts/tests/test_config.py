"""test_config.py — the settings layer: file, environment, precedence, and loud failure.

B12 chose a TOML file outside the repository (`~/.config/cbut/config.toml`) with the
environment able to override it, because the standard library has read a TOML parser since
3.11 and adding one dependency for this was not worth it.

Three properties this suite exists to hold:

1. **Nothing changes when no file exists.** The defaults are the values the interface had
   before any of this, asserted against the constants the TUI still carries — a settings
   layer that quietly retuned the app would be a behaviour change wearing a feature's clothes.
2. **A broken file is an error, never a silent default.** The failure names the path and the
   reason. Silently falling back would leave someone editing a config file that is not being
   read, which is worse than refusing to start.
3. **Precedence is env > file > default**, and an unknown key is rejected rather than ignored,
   because a typo in a key name is exactly the bug nobody notices.

Standard library only; every case points `CBUT_CONFIG` at a temp file so nothing here reads
or writes `~/.config`.
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import cbut_db as db  # noqa: E402


def write_config(tmp, text):
    path = Path(tmp) / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load_tui(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "cbut-tui.py")
    tui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tui)
    return tui


class DefaultsTest(unittest.TestCase):
    def test_no_file_gives_the_values_the_interface_already_had(self):
        cfg = db.load_config(Path("/nonexistent/cbut/config.toml"))
        self.assertEqual(cfg["top_n"], 5)
        self.assertEqual(cfg["log_limit"], 100)
        self.assertEqual(cfg["detail_limit"], 200)
        self.assertEqual(cfg["name_cap"], 28, "the cap is a measured default, not a round number")
        self.assertEqual(cfg["refresh_secs"], 5)
        self.assertEqual(cfg["sync_secs"], 30)

    def test_the_defaults_match_the_tuis_own_constants(self):
        """The defaults are not a second opinion about what the app does.

        If a class constant in the TUI and DEFAULTS ever disagree, one of them is a lie
        about the shipped behaviour, so this compares them instead of trusting either.
        """
        tui = load_tui("cbut_tui_for_config")
        app = tui.TrackerApp
        for key, attr in (("top_n", "DASH_TOP"), ("log_limit", "USAGE_LOG_LIMIT"),
                          ("detail_limit", "DETAIL_LIMIT"), ("name_cap", "NAME_CAP")):
            self.assertEqual(
                db.DEFAULTS[key], getattr(app, attr),
                f"DEFAULTS[{key!r}] no longer matches TrackerApp.{attr} — the settings "
                "layer and the interface disagree about the shipped default")


class FileTest(unittest.TestCase):
    def test_a_valid_file_changes_what_the_app_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, (
                'top_n = 3\n'
                'log_limit = 25\n'
                'refresh_secs = 1.5\n'
                'sync_secs = 120\n'
                'detail_limit = 50\n'
                'name_cap = 40\n'
            ))
            cfg = db.load_config(path)
        self.assertEqual(cfg["top_n"], 3)
        self.assertEqual(cfg["log_limit"], 25)
        self.assertEqual(cfg["refresh_secs"], 1.5)
        self.assertEqual(cfg["sync_secs"], 120)
        self.assertEqual(cfg["detail_limit"], 50)
        self.assertEqual(cfg["name_cap"], 40)

    def test_a_comment_and_blank_lines_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "# my settings\n\ntop_n = 8\n")
            cfg = db.load_config(path)
        self.assertEqual(cfg["top_n"], 8)
        self.assertEqual(cfg["log_limit"], 100, "unlisted keys keep their default")

    def test_an_unknown_key_is_refused_not_ignored(self):
        """A typo'd key would otherwise silently do nothing forever."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "topn = 3\n")
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(path)
        self.assertIn("topn", str(ctx.exception))


class BrokenFileTest(unittest.TestCase):
    def test_malformed_toml_names_the_path_and_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "top_n = \n")
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(path)
        message = str(ctx.exception)
        self.assertIn(str(path), message, "the message must say which file is broken")
        self.assertTrue(message.strip(), "an empty error is not an error")

    def test_a_value_of_the_wrong_type_is_named_by_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, 'top_n = "five"\n')
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(path)
        self.assertIn("top_n", str(ctx.exception))

    def test_an_out_of_range_value_is_refused(self):
        """Zero rows of Top tools is not a preference, it is a broken panel."""
        for key, bad in (("top_n", 0), ("log_limit", -5), ("refresh_secs", 0),
                         ("sync_secs", -1), ("name_cap", 0), ("name_cap", 4000)):
            # a cap of 0 hides every value, a cap of 4000 is not a cap: both are typos,
            # and a typo has to fail loudly rather than render a blank screen
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = write_config(tmp, f"{key} = {bad}\n")
                with self.assertRaises(db.ConfigError):
                    db.load_config(path)


class PrecedenceTest(unittest.TestCase):
    def test_environment_beats_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "top_n = 3\n")
            old = os.environ.get("CBUT_TOP_N")
            os.environ["CBUT_TOP_N"] = "9"
            try:
                cfg = db.load_config(path)
            finally:
                if old is None:
                    del os.environ["CBUT_TOP_N"]
                else:
                    os.environ["CBUT_TOP_N"] = old
        self.assertEqual(cfg["top_n"], 9)

    def test_an_environment_value_is_validated_too(self):
        old = os.environ.get("CBUT_TOP_N")
        os.environ["CBUT_TOP_N"] = "not-a-number"
        try:
            with self.assertRaises(db.ConfigError):
                db.load_config(Path("/nonexistent/cbut/config.toml"))
        finally:
            if old is None:
                del os.environ["CBUT_TOP_N"]
            else:
                os.environ["CBUT_TOP_N"] = old


class WiringTest(unittest.TestCase):
    """A loader nobody calls is not a settings layer."""

    def _app(self, config):
        tui = load_tui("cbut_tui_for_wiring")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "usage.db"
            real = db.open_db(db_path)
            real.close()
            return tui.TrackerApp(db_path=db_path, config=config)

    def test_the_app_reads_its_limits_from_the_config(self):
        app = self._app({"top_n": 2, "log_limit": 7, "detail_limit": 11,
                         "refresh_secs": 5, "sync_secs": 30, "name_cap": 15})
        self.assertEqual(app.DASH_TOP, 2)
        self.assertEqual(app.USAGE_LOG_LIMIT, 7)
        self.assertEqual(app.DETAIL_LIMIT, 11)
        self.assertEqual(app.name_cap, 15,
                         "the app must expose the cap it will actually render with")

    def test_with_no_config_the_app_keeps_the_shipped_defaults(self):
        app = self._app(None)
        self.assertEqual(app.DASH_TOP, db.DEFAULTS["top_n"])
        self.assertEqual(app.USAGE_LOG_LIMIT, db.DEFAULTS["log_limit"])
        self.assertEqual(app.DETAIL_LIMIT, db.DEFAULTS["detail_limit"])
        self.assertEqual(app.name_cap, db.DEFAULTS["name_cap"])


class CliWiringTest(unittest.TestCase):
    """``main()`` is the settings layer's only production caller.

    ``WiringTest`` hands the app a dict, which skips the path a user actually takes:
    file -> ``load_config`` -> ``TrackerApp``. These cases drive ``main()`` itself with
    ``TrackerApp.run`` replaced, because a real run in a test would block forever — and
    a regression that ignored the config file would then look like a hang instead of a
    failure.
    """

    def _main(self, cfg_text):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        tui = load_tui("cbut_tui_for_cli")
        started = []
        # ``run`` is inherited from App, so deleting is the correct restore.
        self.addCleanup(lambda: delattr(tui.TrackerApp, "run"))
        tui.TrackerApp.run = lambda self: started.append(self)
        old_path = db.CONFIG_PATH
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = (write_config(tmp, cfg_text) if cfg_text is not None
                        else Path(tmp) / "absent.toml")
            db.CONFIG_PATH = cfg_path
            conn = db.open_db(Path(tmp) / "u.db")
            db.ensure_schema(conn)
            conn.close()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = tui.main(["--db", str(Path(tmp) / "u.db")])
            finally:
                db.CONFIG_PATH = old_path
        return rc, (started[0] if started else None), out.getvalue() + err.getvalue(), cfg_path

    def test_the_settings_file_reaches_the_app_through_main(self):
        rc, app, out, cfg = self._main(
            "top_n = 3\nlog_limit = 25\ndetail_limit = 50\nname_cap = 33\n")
        self.assertEqual(rc, 0, out)
        self.assertIsNotNone(app, "main() never started the app")
        self.assertEqual((app.DASH_TOP, app.USAGE_LOG_LIMIT, app.DETAIL_LIMIT), (3, 25, 50),
                         f"main() built an app that ignored {cfg}")
        self.assertEqual(app.name_cap, 33,
                         f"main() built an app ignoring the cap in {cfg}")

    def test_no_file_means_main_starts_with_the_shipped_defaults(self):
        rc, app, out, _ = self._main(None)
        self.assertEqual(rc, 0, out)
        self.assertIsNotNone(app)
        self.assertEqual(app.DASH_TOP, db.DEFAULTS["top_n"])
        self.assertEqual(app.name_cap, db.DEFAULTS["name_cap"])
        self.assertEqual(app.USAGE_LOG_LIMIT, db.DEFAULTS["log_limit"])
        self.assertEqual(app.DETAIL_LIMIT, db.DEFAULTS["detail_limit"])

    def test_a_broken_file_stops_main_before_the_tui_starts(self):
        rc, app, out, cfg = self._main("top_n = \n")
        self.assertEqual(rc, 2, out)
        self.assertIsNone(app, "the TUI ran on a settings file that cannot be read")
        self.assertIn(str(cfg), out)


class HealthSurfaceTest(unittest.TestCase):
    """`cbut health` is where a user finds out that settings exist and where they live."""

    def _health(self, env):
        import contextlib
        import io
        import subprocess
        # Run the CLI in a child process: the config is read from the environment, and
        # poking os.environ in-process would leak into every later test in this file.
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "cbut-stats.py"), "--db", env["db"], "health"],
            capture_output=True, text=True, env=env["os"])
        return proc.returncode, proc.stdout + proc.stderr

    def _env(self, db_path, extra):
        # Strip every setting the loader can read, not a hand-picked few: an
        # ambient CBUT_* value would otherwise change the output this file asserts on.
        hidden = set(db.ENV_NAMES.values()) | {"CBUT_CONFIG"}
        base = {k: v for k, v in os.environ.items() if k not in hidden}
        base.update(extra)
        return {"os": base, "db": str(db_path)}

    def _make_db(self, tmp):
        path = Path(tmp) / "u.db"
        conn = db.open_db(path)
        db.ensure_schema(conn)
        conn.close()
        return path

    def test_health_names_the_config_file_and_the_values_in_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = write_config(tmp, "top_n = 4\n")
            rc, out = self._health(self._env(self._make_db(tmp), {"CBUT_CONFIG": str(cfg)}))
        self.assertEqual(rc, 0, out)
        self.assertIn(str(cfg), out, "health must say which settings file it read")
        self.assertIn("top_n=4", out)

    def test_health_prints_every_key_the_loader_knows(self):
        """The report is the only place a user learns a setting exists and what it is
        set to right now. The line used to hand-list five keys, so a sixth could be
        read by the app, honoured by the tables, and still be absent here — the same
        shape as every other "wired but not in force" entry in the record. Listing
        what was loaded makes the claim un-rottable; this case is what proves it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Point at a file that does not exist inside the tmpdir: an unset
            # CBUT_CONFIG would fall back to the real ~/.config/cbut/config.toml,
            # and whatever that person happens to have set is not this test's input.
            absent = Path(tmp) / "absent" / "config.toml"
            rc, out = self._health(self._env(self._make_db(tmp),
                                             {"CBUT_CONFIG": str(absent)}))
        self.assertEqual(rc, 0, out)
        missing = sorted(k for k in db.DEFAULTS if f"{k}=" not in out)
        self.assertFalse(missing,
                         f"health reads these settings but never reports them: {missing}")

    def test_a_broken_settings_file_is_reported_instead_of_hiding(self):
        """A syntax error is owed the path and a location, not a key name.

        (A *type* error does name the key — see
        ``BrokenFileTest.test_a_value_of_the_wrong_type_is_named_by_key``.)
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = write_config(tmp, "top_n = \n")
            rc, out = self._health(self._env(self._make_db(tmp), {"CBUT_CONFIG": str(cfg)}))
        self.assertIn(str(cfg), out, "the report must say which file is unusable")
        self.assertRegex(out, r"(?i)not valid toml|toml")
        self.assertRegex(out, r"line \d+", "and where in it the parse stopped")

    def test_health_reports_the_database_it_was_handed(self):
        """`--db` must not print a different path than the one it opened."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_db(tmp)
            rc, out = self._health(self._env(path, {}))
        self.assertEqual(rc, 0, out)
        self.assertIn(str(path), out)
        self.assertNotIn(str(db.DB_PATH), out,
                         "health printed the default path, not the one --db gave")


if __name__ == "__main__":
    unittest.main()
