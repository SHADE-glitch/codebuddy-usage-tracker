"""test_config.py — the settings layer: file, environment, precedence, and loud failure.

B12 chose a TOML file outside the repository (`~/.config/cbut/config.toml`) with the
environment able to override it, because the standard library has read a TOML parser since
3.11 and adding one dependency for this was not worth it.

Four properties this suite exists to hold:

1. **Nothing changes when no file exists.** The defaults are the values the interface had
   before any of this, asserted against the constants the TUI still carries — a settings
   layer that quietly retuned the app would be a behaviour change wearing a feature's clothes.
2. **A broken file is an error, never a silent default.** The failure names the path and the
   reason. Silently falling back would leave someone editing a config file that is not being
   read, which is worse than refusing to start.
3. **Precedence is env > file > default**, and an unknown key is rejected rather than ignored,
   because a typo in a key name is exactly the bug nobody notices.
4. **No case depends on the shell it runs in.** Several cases assert what the *file* or the
   *defaults* produce, and an exported ``CBUT_TOP_N`` outranks both — so without the isolation
   below, this suite passes on a clean shell and fails on a configured one, which reads exactly
   like a regression in the settings layer.

Standard library only; every case points `CBUT_CONFIG` at a temp file so nothing here reads
or writes `~/.config`.
"""

import contextlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402,F401 -- tripwire: the suite must never open the real database

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import cbut_db as db  # noqa: E402


@contextlib.contextmanager
def no_settings_env():
    """Hide every setting name the loader can read, then put it back exactly.

    Derived from ``db.ENV_NAMES`` rather than listed, because a hand-copied set is how the
    next key added to the layer gets forgotten here too.
    """
    saved = {name: os.environ.pop(name, None) for name in db.ENV_NAMES.values()}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
        with no_settings_env():
            cfg = db.load_config(Path("/nonexistent/cbut/config.toml"))
        self.assertEqual(cfg["top_n"], 5)
        self.assertEqual(cfg["log_limit"], 100)
        self.assertEqual(cfg["detail_limit"], 200)
        self.assertEqual(cfg["name_cap"], 28, "the cap is a measured default, not a round number")

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
        with tempfile.TemporaryDirectory() as tmp, no_settings_env():
            path = write_config(tmp, (
                'top_n = 3\n'
                'log_limit = 25\n'
                'detail_limit = 50\n'
                'name_cap = 40\n'
            ))
            cfg = db.load_config(path)
        self.assertEqual(cfg["top_n"], 3)
        self.assertEqual(cfg["log_limit"], 25)
        self.assertEqual(cfg["detail_limit"], 50)
        self.assertEqual(cfg["name_cap"], 40)

    def test_a_comment_and_blank_lines_are_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, no_settings_env():
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

    def test_a_file_saved_in_another_encoding_is_a_config_error(self):
        """The bytes are not UTF-8 is the same class of problem as the TOML not parsing:
        the file is the user's to fix, and every caller already turns a ConfigError into
        a sentence. It used to raise UnicodeDecodeError straight through main() and
        cbut health — the one command you run to find out what is wrong — so a settings
        file saved by an editor in another encoding was a traceback.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_bytes(b"top_n = 5\n# \xff\xfe\n")
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(path)
        message = str(ctx.exception)
        self.assertIn(str(path), message, "the message must say which file is unusable")
        self.assertIn("cannot be read", message)

    def test_a_file_that_cannot_be_read_is_a_config_error_too(self):
        """A permission problem is a settings file the user can fix, not a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, "top_n = 5\n")
            with mock.patch.object(Path, "read_text",
                                   side_effect=PermissionError("permission denied")):
                with self.assertRaises(db.ConfigError) as ctx:
                    db.load_config(path)
        self.assertIn("cannot be read", str(ctx.exception))

    def test_an_out_of_range_value_is_refused(self):
        """Zero rows of Top tools is not a preference, it is a broken panel."""
        for key, bad in (("top_n", 0), ("log_limit", -5),
                         ("detail_limit", 0), ("name_cap", 0), ("name_cap", 4000)):
            # a cap of 0 hides every value, a cap of 4000 is not a cap: both are typos,
            # and a typo has to fail loudly rather than render a blank screen
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = write_config(tmp, f"{key} = {bad}\n")
                with self.assertRaises(db.ConfigError):
                    db.load_config(path)


class PathShapeTest(unittest.TestCase):
    """What sits at the config path, and what the loader says about it.

    `is_file()` answers False for four different situations — absent, a directory, a FIFO,
    and a path this user cannot even stat — so all four read as "no settings file" and the
    loader returned the defaults without a word. Only the first is the normal case; the
    other three are a settings file somebody has to fix, and `cbut health` reported them as
    "(absent — defaults)", which was a false statement about the path.
    """

    def test_a_directory_where_the_file_should_be_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.mkdir()
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(path)
        self.assertIn(str(path), str(ctx.exception))
        self.assertIn("not a regular file", str(ctx.exception))

    def test_a_symlink_to_a_directory_is_refused_too(self):
        """`stat` follows the link, so what is refused is what would have been read."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real"
            target.mkdir()
            link = Path(tmp) / "config.toml"
            link.symlink_to(target)
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(link)
        self.assertIn("not a regular file", str(ctx.exception))

    def test_a_symlink_to_nothing_is_still_absent(self):
        """The normal case has to stay silent: a dotfile manager that links a file it has
        not created yet is not a misconfiguration, and refusing it would make the tool
        unusable for that setup."""
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "config.toml"
            link.symlink_to(Path(tmp) / "not-there.toml")
            self.assertEqual(db.load_config(link), db.DEFAULTS)

    @unittest.skipIf(os.geteuid() == 0, "root can stat through a 0o000 directory")
    def test_a_file_this_user_cannot_stat_is_refused(self):
        """`exists()` swallows the permission error and answers False, so the file read
        as absent. It is there, and it gets the answer every other unreadable file
        gets."""
        with tempfile.TemporaryDirectory() as tmp:
            inner = Path(tmp) / "locked"
            inner.mkdir()
            (inner / "config.toml").write_text("top_n = 3\n", encoding="utf-8")
            inner.chmod(0o000)
            try:
                with self.assertRaises(db.ConfigError) as ctx:
                    db.load_config(inner / "config.toml")
            finally:
                inner.chmod(0o755)
        self.assertIn("cannot be read", str(ctx.exception))


class PrecedenceTest(unittest.TestCase):
    def test_environment_beats_the_file(self):
        with tempfile.TemporaryDirectory() as tmp, no_settings_env():
            path = write_config(tmp, "top_n = 3\n")
            os.environ["CBUT_TOP_N"] = "9"
            cfg = db.load_config(path)
        self.assertEqual(cfg["top_n"], 9)

    def test_an_environment_value_is_validated_too(self):
        with no_settings_env():
            os.environ["CBUT_TOP_N"] = "not-a-number"
            with self.assertRaises(db.ConfigError):
                db.load_config(Path("/nonexistent/cbut/config.toml"))

    def test_an_out_of_range_environment_value_is_refused(self):
        """The bounds are not only a file check.

        If `CBUT_NAME_CAP=0` sailed through, the environment would be a way to ask
        for a blank panel that the settings file refuses — and the message has to
        name the variable, since that is what the reader actually set.
        """
        with no_settings_env():
            os.environ["CBUT_NAME_CAP"] = "0"
            with self.assertRaises(db.ConfigError) as ctx:
                db.load_config(Path("/nonexistent/cbut/config.toml"))
        self.assertIn("CBUT_NAME_CAP", str(ctx.exception),
                      f"the refusal names the file, not the variable: {ctx.exception}")

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
                         "name_cap": 15})
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
        with tempfile.TemporaryDirectory() as tmp, no_settings_env():
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
        # CBUT_CODEBUDDY_DIR goes too, then is pinned below: `health` stats and globs the
        # host dir, so without a pin the child reads the real ~/.codebuddy.
        # CBUT_PRODUCT_JSON is the same kind of override for the builtin catalogue, so it
        # is hidden as well — otherwise whether the child prints the package's file or the
        # checked-in fallback depends on whose machine is running the suite.
        hidden = set(db.ENV_NAMES.values()) | {
            "CBUT_CONFIG", "CBUT_CODEBUDDY_DIR", "CBUT_PRODUCT_JSON"}
        base = {k: v for k, v in os.environ.items() if k not in hidden}
        base["CBUT_CODEBUDDY_DIR"] = str(Path(db_path).parent / "no-such-codebuddy")
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

    def test_health_says_which_builtin_catalogue_was_read(self):
        """The fallback constants are indistinguishable from the real list.

        `BUILTIN_TOOLS` names a tool upstream no longer ships, and the installed
        `product.json` names one the constants do not have, so a table built from
        the wrong list is indistinguishable from a correct one by inspection. The
        line is what tells the two apart — and it has to name the file it read, or
        "product.json" proves nothing about *which* product.json.
        """
        from _catalog import SNAPSHOT
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._health(self._env(self._make_db(tmp),
                                             {"CBUT_PRODUCT_JSON": str(SNAPSHOT)}))
        self.assertEqual(rc, 0, out)
        line = next(l for l in out.splitlines() if "builtin catalog" in l)
        self.assertIn(f"product.json @ {SNAPSHOT}", line)
        # The snapshot is the installed package's own two arrays, so the counts it
        # implies are the counts the line must print — not just "some numbers".
        raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertIn(f"{len(raw['tools'])} tools", line)
        self.assertIn(f"{len(raw['agents'])} agents", line)

    def test_health_reports_the_fallback_and_why_it_was_used(self):
        """Three states, three messages: resolved, found-but-useless, absent.

        Collapsing "the file is missing" into "the file says nothing" is how a user
        ends up deleting the wrong thing, so the path is named when there is one and
        the word "unresolved" is reserved for when there is not.
        """
        sync = db.load_sync()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent" / "product.json"
            empty = Path(tmp) / "empty.json"
            empty.write_text(json.dumps({"tools": [], "agents": []}), encoding="utf-8")
            for pinned, expected in (
                    (missing, "product.json unresolved"),
                    (empty, "carries no catalogue")):
                with self.subTest(pinned=pinned.name):
                    rc, out = self._health(self._env(self._make_db(tmp),
                                                     {"CBUT_PRODUCT_JSON": str(pinned)}))
                    self.assertEqual(rc, 0, out)
                    line = next(l for l in out.splitlines() if "builtin catalog" in l)
                    self.assertIn("fallback constants", line)
                    self.assertIn(expected, line)
                    self.assertIn(str(pinned) if pinned.exists() else "", line)

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

    def test_health_reports_an_undecodable_file_instead_of_crashing(self):
        """The report is the diagnostic. A traceback where the diagnosis belongs is the
        one failure this command cannot afford — it is what you run when a number is
        wrong, and a settings file in another encoding is a reason a number is wrong."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_bytes(b"top_n = 5\n# \xff\xfe\n")
            rc, out = self._health(self._env(self._make_db(tmp),
                                                {"CBUT_CONFIG": str(cfg)}))
        self.assertEqual(rc, 0, out)
        self.assertIn(str(cfg), out, "the report must name the file it could not read")
        self.assertIn("NOT READ", out)
        self.assertNotIn("Traceback", out)

    def test_health_reports_a_directory_instead_of_calling_it_absent(self):
        """The line said "(absent — defaults)" for a path that exists. With the loader
        refusing a non-regular file, the same sentence is the honest one it already has
        for a broken file."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.mkdir()
            rc, out = self._health(self._env(self._make_db(tmp), {"CBUT_CONFIG": str(cfg)}))
        self.assertEqual(rc, 0, out)
        self.assertIn(str(cfg), out)
        self.assertIn("is not a regular file", out)
        self.assertNotIn("absent", out)

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
