"""Tests for kadash.py. Temp STATE_DIR + fixture status.json; free port (0) only.
Run from the package root: python3 -m unittest discover -s tests -v   (Python 3.10+)
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import kadash  # noqa: E402

POSIX = os.name != "nt"


def mode_ok(path, mode):
    """File permission bits equal mode; always True on Windows (no POSIX modes there)."""
    return not POSIX or stat.S_IMODE(os.stat(path).st_mode) == mode

SID_A = "5f2c8e1a-7b3d-4c9e-a1f0-2d6b8c4e9a07"
SID_B = "0b1c2d3e-aaaa-bbbb-cccc-000000000002"


def fixture(now=None):
    now = now or time.time()
    return {
        "schema": 1, "updated": now - 5, "proxy_pid": 28588, "global_off": False,
        "sessions": [
            {"sid": SID_A, "model": "claude-opus-5-5", "prefix_tokens": 443880,
             "last_real_ts": now - 1200, "last_refresh_ts": now - 1200, "next_ping_ts": now + 2100,
             "pings": 0, "cap_h": 5, "cap_end_ts": now + 16800, "state": "active", "stop_reason": None,
             "est_ping_usd": 0.115, "est_rewarm_usd": 3.67},
            {"sid": SID_B, "model": "claude-sonnet-5", "prefix_tokens": 120500,
             "last_real_ts": now - 7000, "last_refresh_ts": now - 3500, "next_ping_ts": now + 100,
             "pings": 2, "cap_h": 5, "cap_end_ts": now + 11000, "state": "stopped", "stop_reason": "flag",
             "est_ping_usd": 0.03, "est_rewarm_usd": 0.81},
        ],
        "today": {"pings": 3, "ping_usd": 0.31, "avoided_rewarm_usd": 7.2,
                  "stops": {"cap": 1, "flag": 0, "verify-fail": 1}},
    }


class _DashBase(unittest.TestCase):
    """Server fixture + helpers; no tests of its own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sd = self._tmp.name
        os.environ["KA_STATE_DIR"] = self.sd          # belt and braces: never the real ~/.claude/ka
        self._ptmp = tempfile.TemporaryDirectory()
        self.pd = self._ptmp.name
        os.environ["KA_PROJECTS_DIR"] = self.pd       # never the real ~/.claude/projects
        self.fix = fixture()
        self.write_status(self.fix)
        self.srv = kadash.make_server(self.sd, 0, projects_dir=self.pd)
        self.port = self.srv.server_address[1]
        self.assertNotIn(self.port, (8787, 8788))
        self.tok = self.srv.token
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._tmp.cleanup()
        self._ptmp.cleanup()

    def write_status(self, obj):
        with open(os.path.join(self.sd, "status.json"), "w") as f:
            json.dump(obj, f)

    def req(self, method, path, host=None, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not False:
            c.putheader("Host", host or "127.0.0.1:%d" % self.port)
        data = json.dumps(body).encode() if body is not None else b""
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = r.status, r.read().decode()
        c.close()
        return out

    def act(self, action, sid=None, headers=None):
        h = {"X-KA-Dash": "1", "Content-Type": "application/json"} if headers is None else headers
        return self.req("POST", "/%s/action" % self.tok, headers=h, body={"action": action, "sid": sid})

    def stopfile(self, sid):
        return os.path.join(self.sd, "stop", sid)


class DashTest(_DashBase):
    # (1)
    def test_token_required(self):
        self.assertEqual(self.req("GET", "/")[0], 404)
        self.assertEqual(self.req("GET", "/status.json")[0], 404)
        self.assertEqual(self.req("GET", "/wrongtoken/")[0], 404)
        self.assertEqual(self.req("GET", "/wrongtoken/status.json")[0], 404)
        self.assertEqual(self.req("GET", "/%sX/" % self.tok)[0], 404)
        self.assertEqual(self.req("GET", "/%s/" % self.tok)[0], 200)
        self.assertEqual(self.req("GET", "/%s/" % self.tok, host="localhost:%d" % self.port)[0], 200)

    def test_token_file_0600_and_stable(self):
        p = os.path.join(self.sd, "dash.token")
        self.assertTrue(mode_ok(p, 0o600))
        self.assertEqual(kadash.load_or_create_token(self.sd), self.tok)

    # (2)
    def test_wrong_host(self):
        for h in ("evil.example:%d" % self.port, "127.0.0.1:1", "127.0.0.1", "0.0.0.0:%d" % self.port):
            self.assertEqual(self.req("GET", "/%s/" % self.tok, host=h)[0], 403, h)
        self.assertEqual(self.req("GET", "/%s/" % self.tok, host=False)[0], 403)
        self.assertEqual(self.req("POST", "/%s/action" % self.tok, host="evil.example",
                                  headers={"X-KA-Dash": "1"}, body={"action": "stop_all"})[0], 403)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "off")))

    # (3)
    def test_post_requires_header(self):
        self.assertEqual(self.act("stop", SID_A, headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.act("stop_all", headers={"X-KA-Dash": "0"})[0], 403)
        self.assertFalse(os.path.exists(self.stopfile(SID_A)))
        self.assertFalse(os.path.exists(os.path.join(self.sd, "off")))
        # cross-origin header also rejected
        h = {"X-KA-Dash": "1", "Origin": "http://evil.example"}
        self.assertEqual(self.act("stop_all", headers=h)[0], 403)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "off")))
        # GET cannot act
        self.assertEqual(self.req("GET", "/%s/action" % self.tok)[0], 404)

    # (4)
    def test_stop_listed_and_rejects(self):
        code, body = self.act("stop", SID_A)
        self.assertEqual(code, 200, body)
        self.assertTrue(os.path.isfile(self.stopfile(SID_A)))
        self.assertTrue(mode_ok(self.stopfile(SID_A), 0o600))
        bad = ["deadbeef-0000", "../x", "../../off", "..", ".", "", "/etc/passwd", SID_A + "/../x",
               SID_A[:8], None, 42, ["x"]]
        for sid in bad:
            self.assertEqual(self.act("stop", sid)[0], 400, repr(sid))
        self.assertEqual(sorted(os.listdir(os.path.join(self.sd, "stop"))), [SID_A])
        self.assertFalse(os.path.exists(os.path.join(self.sd, "x")))
        self.assertFalse(os.path.exists(os.path.join(self.sd, "off")))
        self.assertEqual(self.act("bogus", SID_A)[0], 400)

    def test_listed_but_unsafe_sid_rejected(self):
        fx = fixture()
        fx["sessions"][0]["sid"] = "../escape"
        self.write_status(fx)
        self.assertEqual(self.act("stop", "../escape")[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "escape")))

    # (5)
    def test_resume(self):
        self.act("stop", SID_A)
        self.assertTrue(os.path.exists(self.stopfile(SID_A)))
        self.assertEqual(self.act("resume", SID_A)[0], 200)
        self.assertFalse(os.path.exists(self.stopfile(SID_A)))
        self.assertEqual(self.act("resume", SID_A)[0], 200)      # idempotent
        self.assertEqual(self.act("resume", "../x")[0], 400)

    def test_stop_all_resume_all(self):
        self.act("stop", SID_A)
        self.act("stop", SID_B)
        self.assertEqual(self.act("stop_all")[0], 200)
        self.assertTrue(os.path.isfile(os.path.join(self.sd, "off")))
        code, body = self.act("resume_all")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["stop_files_removed"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.sd, "off")))
        self.assertEqual(os.listdir(os.path.join(self.sd, "stop")), [])
        self.assertEqual(self.act("resume_all")[0], 200)          # nothing to remove: still fine

    # (6)
    def test_status_returns_fixture(self):
        self.act("stop", SID_A)
        code, body = self.req("GET", "/%s/status.json" % self.tok)
        self.assertEqual(code, 200)
        got = json.loads(body)
        dash = got.pop("_dash")
        got.pop("_titles")
        self.assertEqual(got, self.fix)
        self.assertEqual(dash["stop_files"], [SID_A])
        self.assertFalse(dash["off_file"])

    def test_missing_or_invalid_status(self):
        os.unlink(os.path.join(self.sd, "status.json"))
        code, page = self.req("GET", "/%s/" % self.tok)
        self.assertEqual(code, 200)
        self.assertIn("proxy not reporting", page)
        code, body = self.req("GET", "/%s/status.json" % self.tok)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["error"], "proxy not reporting")
        self.assertEqual(self.act("stop", SID_A)[0], 400)          # nothing listed -> no stop file
        for junk in ("{not json", "[]", '{"schema":1}'):
            with open(os.path.join(self.sd, "status.json"), "w") as f:
                f.write(junk)
            self.assertIn("proxy not reporting", self.req("GET", "/%s/" % self.tok)[1], junk)
            self.assertIn("error", json.loads(self.req("GET", "/%s/status.json" % self.tok)[1]))

    def test_page_shape(self):
        code, page = self.req("GET", "/%s/" % self.tok)
        self.assertEqual(code, 200)
        self.assertNotIn("proxy not reporting</span>", page.split("<script>")[0])
        self.assertIn("setInterval(refresh, 5000)", page)
        self.assertNotRegex(page, r"(src|href)=")                   # no external assets
        self.assertNotIn("<link", page)
        self.assertNotIn(self.tok, page)                            # relative URLs only

    # (7)
    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertEqual(self.srv.socket.getsockname()[0], "127.0.0.1")
        self.assertEqual(self.srv.socket.family, socket.AF_INET)
        # a non-loopback local address must not reach it
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))                             # no packet sent; picks the LAN iface
            lan = s.getsockname()[0]
            s.close()
        except OSError:
            lan = None
        if lan and not lan.startswith("127."):
            with self.assertRaises(OSError):
                socket.create_connection((lan, self.port), timeout=2).close()


PROJ = "-Users-alice-Workspace-my-project"
FILLER = json.dumps({"type": "assistant", "message": {"content": "x" * 900,
                     "note": '{"type":"custom-title","customTitle":"decoy"}'}}) + "\n"


def title_line(sid, title):
    return json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}) + "\n"


class TitleTest(unittest.TestCase):
    """TitleCache in isolation: temp projects dir, synthetic transcripts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pd = self._tmp.name
        self.tc = kadash.TitleCache(self.pd)

    def tearDown(self):
        self._tmp.cleanup()

    def transcript(self, sid, *parts, proj=PROJ):
        d = os.path.join(self.pd, proj)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, sid + ".jsonl")
        with open(p, "w") as f:
            f.write("".join(parts))
        return p

    def title(self, sid):
        return self.tc.lookup([sid])[sid]

    def test_title_near_end_of_large_transcript(self):
        big = FILLER * 1800                                        # ~1.7 MiB
        self.assertGreater(len(big), 1024 * 1024)
        self.transcript(SID_A, big, title_line(SID_A, "parser refactor"), FILLER * 3)
        t = self.title(SID_A)
        self.assertEqual(t, {"title": "parser refactor", "project": "my-project", "custom": True})

    def test_title_deep_in_file_multi_chunk(self):
        self.transcript(SID_A, FILLER * 10, title_line(SID_A, "deep"), FILLER * 2500)   # ~2.3 MiB after it
        self.assertEqual(self.title(SID_A)["title"], "deep")

    def test_scan_bounded_to_8mib(self):
        self.transcript(SID_A, title_line(SID_A, "too old"), FILLER * 9500)            # ~8.8 MiB after it
        t = self.title(SID_A)
        self.assertFalse(t["custom"])
        self.assertEqual(t["title"], "my-project")

    def test_last_custom_title_wins(self):
        self.transcript(SID_A, title_line(SID_A, "first"), FILLER * 5, title_line(SID_A, "second"),
                        FILLER * 5, title_line(SID_A, "third"), FILLER)
        self.assertEqual(self.title(SID_A)["title"], "third")

    def test_fallback_project_name(self):
        self.transcript(SID_A, FILLER * 20)
        self.assertEqual(self.title(SID_A), {"title": "my-project", "project": "my-project", "custom": False})
        self.transcript(SID_B, FILLER, proj="-Users-alice")
        self.assertEqual(self.title(SID_B)["title"], "~")
        self.assertEqual(kadash.project_label("-Users-alice-Workspace-agent-cooking"), "agent-cooking")
        self.assertEqual(kadash.project_label("-Users-alice-Library-foo"), "Library-foo")
        self.assertEqual(kadash.project_label("-home-alice-src-app"), "src-app")          # Linux
        self.assertEqual(kadash.project_label("C--Users-alice-Workspace-my-project"), "my-project")   # Windows
        self.assertEqual(kadash.project_label("C--Users-alice"), "~")

    def test_no_transcript(self):
        self.assertEqual(self.title(SID_A)["title"], "\u2014")
        self.assertFalse(self.title(SID_A)["custom"])

    def test_non_uuid_sid_ignored(self):
        for bad in ("*", "../x", SID_A.upper(), SID_A[:8], SID_A.replace("-", ""), 42, None):
            self.assertEqual(self.tc.lookup([bad]), {}, repr(bad))
        # a glob-ish sid must not match a real transcript
        self.transcript(SID_A, title_line(SID_A, "secret"))
        self.assertEqual(self.tc.lookup(["*"]), {})
        self.assertEqual(self.tc.reads, 0)

    def test_cache_skips_unchanged_file(self):
        p = self.transcript(SID_A, FILLER * 50, title_line(SID_A, "one"), FILLER)
        for _ in range(5):
            self.assertEqual(self.title(SID_A)["title"], "one")
        self.assertEqual(self.tc.reads, 1)
        with open(p, "a") as f:                                   # append: rescan only the tail
            f.write(title_line(SID_A, "two"))
        self.assertEqual(self.title(SID_A)["title"], "two")
        self.assertEqual(self.tc.reads, 2)
        with open(p, "a") as f:                                   # append w/o title: keep "two"
            f.write(FILLER * 3)
        self.assertEqual(self.title(SID_A)["title"], "two")
        self.assertEqual(self.tc.reads, 3)
        self.assertEqual(self.title(SID_A)["title"], "two")
        self.assertEqual(self.tc.reads, 3)

    def test_append_across_partial_line(self):
        p = self.transcript(SID_A, FILLER * 5)
        line = title_line(SID_A, "split")
        with open(p, "a") as f:
            f.write(line[:20])                                    # writer mid-line during a refresh
        self.assertFalse(self.title(SID_A)["custom"])
        with open(p, "a") as f:
            f.write(line[20:])
        self.assertEqual(self.title(SID_A)["title"], "split")

    def test_other_session_title_ignored_and_control_chars(self):
        self.transcript(SID_A, title_line(SID_A, "mine\x1b[31m\nx"), title_line(SID_B, "not mine"))
        self.assertEqual(self.title(SID_A)["title"], "mine [31m x")


class DashTitleTest(_DashBase):
    """Titles through the HTTP surface."""

    def put_transcript(self, sid, *parts, proj=PROJ):
        d = os.path.join(self.pd, proj)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write("".join(parts))

    def test_titles_in_status_endpoint_only_with_token(self):
        self.put_transcript(SID_A, FILLER, title_line(SID_A, "my session"))
        code, body = self.req("GET", "/%s/status.json" % self.tok)
        self.assertEqual(code, 200)
        titles = json.loads(body)["_titles"]
        self.assertEqual(titles[SID_A]["title"], "my session")
        self.assertEqual(titles[SID_B]["title"], "\u2014")
        for path in ("/status.json", "/wrong/status.json", "/"):
            code, body = self.req("GET", path)
            self.assertEqual(code, 404)
            self.assertNotIn("my session", body)
        with open(os.path.join(self.sd, "status.json")) as f:
            self.assertNotIn("my session", f.read())

    def test_page_escapes_title(self):
        evil = '<script>alert(1)</script>"\'&' + "y" * 60
        self.put_transcript(SID_A, title_line(SID_A, evil))
        code, page = self.req("GET", "/%s/" % self.tok)
        self.assertEqual(code, 200)
        self.assertNotIn("alert(1)", page)                        # never server-rendered raw
        self.assertIn("<th>SID</th><th>TITLE</th>", page)
        self.assertIn("titleCell(titles[s.sid])", page)
        t = json.loads(self.req("GET", "/%s/status.json" % self.tok)[1])["_titles"][SID_A]
        self.assertEqual(t["title"], evil)                        # JSON carries it as data
        node = shutil.which("node")
        if not POSIX:
            self.skipTest("node-based rendered-cell check runs on macOS/Linux only")
        if not node:
            self.skipTest("node not installed: rendered-cell check needs a JS runtime")
        # run the page's own pure helpers (esc/trunc/span/titleCell) on the endpoint's value
        helpers = page.split("<script>", 1)[1].split("function confirmPending", 1)[0]
        js = helpers + "\nprocess.stdout.write(titleCell(JSON.parse(require('fs').readFileSync(0,'utf8'))));"
        cell = subprocess.run([node, "-e", js], input=json.dumps(t), capture_output=True,
                              text=True, timeout=20, check=True).stdout
        self.assertNotIn("<script", cell)
        self.assertNotIn("\"'", cell)
        self.assertIn('title="&lt;script&gt;alert(1)&lt;/script&gt;&quot;&#39;&amp;' + "y" * 60 + '"', cell)
        self.assertIn(">&lt;script&gt;alert(1)&lt;/script&gt;&quot;&#39;&amp;" + "y" * 60 + "</div>", cell)
        self.assertTrue(cell.startswith('<div class="tt" '), cell)   # CSS clips it; no project suffix
        self.assertNotIn("my-project", cell)
        # fallback: project name only, muted
        fb = subprocess.run([node, "-e", js], input=json.dumps(
            {"title": "my-project", "project": "my-project", "custom": False}),
            capture_output=True, text=True, timeout=20, check=True).stdout
        self.assertEqual(fb, '<div class="tt dim" title="my-project">my-project</div>')

    def test_layout_no_inner_scroll_box(self):
        page = self.req("GET", "/%s/" % self.tok)[1]
        css = page.split("<style>", 1)[1].split("</style>", 1)[0]
        self.assertNotIn("overflow-x", css)                       # page scrolls, not an inner box
        self.assertRegex(css, r"\.tt\{[^}]*max-width:24ch[^}]*text-overflow:ellipsis")

    def test_title_never_logged(self):
        self.put_transcript(SID_A, title_line(SID_A, "TOPSECRET-title-xyz"))
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            for _ in range(3):
                self.req("GET", "/%s/status.json" % self.tok)
                self.req("GET", "/%s/" % self.tok)
            self.act("stop", SID_A)
        self.assertIn("status.json", err.getvalue())              # logging did happen
        self.assertNotIn("TOPSECRET", err.getvalue())
        self.assertNotIn("TOPSECRET", out.getvalue())
        with open(os.path.join(self.sd, "status.json")) as f:
            self.assertNotIn("TOPSECRET", f.read())


if __name__ == "__main__":
    unittest.main()
