"""Offline acceptance tests for ka_proxy v1 (stdlib unittest; never touches api.anthropic.com or port 8787).

Run from the package root: python3 -m unittest discover -s tests -v   (Python 3.10+; tests use ../bin)
Unix-only checks (file modes, bash scripts) skip cleanly on Windows.
"""
import base64, collections, re, http.client, http.server, json, os, shutil, socket, ssl, subprocess, sys, tempfile, threading, time, unittest

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")   # package bin/
sys.path.insert(0, HERE)
import ka_proxy  # noqa: E402
import ka_ca  # noqa: E402

PY = sys.executable
PKG = os.path.dirname(HERE)
OPENSSL = ka_ca.find_openssl()
POSIX = os.name != "nt"
BASH = shutil.which("bash") if POSIX else None
MOD = {}


def mode_ok(path, mode):
    """File permission bits equal mode; always True on Windows (no POSIX modes there)."""
    return not POSIX or os.stat(path).st_mode & 0o777 == mode


def setUpModule():
    """One CA + leaf (via ka_ca.py) shared by all tests; each test copies it into its own STATE_DIR."""
    assert OPENSSL, "openssl not found (set KA_OPENSSL)"
    MOD["tmp"] = tempfile.mkdtemp(prefix="ka-ca-")
    state = os.path.join(MOD["tmp"], "state")
    r = subprocess.run([PY, os.path.join(HERE, "ka_ca.py")], capture_output=True, text=True,
                       env=dict(os.environ, KA_STATE_DIR=state))
    assert r.returncode == 0, r.stderr
    MOD["ca_dir"] = os.path.join(state, "ca")


def tearDownModule():
    shutil.rmtree(MOD["tmp"], ignore_errors=True)
S_PROMPT, S_AUTH, S_BODY, S_OUT = "SENTINEL_PROMPT_7f3a", "SENTINEL_AUTH_91bc", "SENTINEL_BODY_c0de", "SENTINEL_OUTPUT_q9x"
S_ERRMSG = "SENTINEL_ERRMSG_5hr_cap"   # error.message text from upstream; must never be logged
SENTINELS = (S_PROMPT, S_AUTH, S_BODY, S_OUT, S_ERRMSG, ka_proxy.COMPACTION_OPENER)
MAIN_TOOLS = ["Bash", "Read", "Edit", "Write", "Agent"]
SUB_TOOLS = ["Bash", "Read", "Grep", "Glob"]
MODEL = "claude-opus-5-5"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeUpstream:
    """Records every request. Stream requests get an SSE stream; max_tokens:0 requests get JSON."""

    def __init__(self):
        self.requests = []
        # realistic prefix (250k) so the dynamic cap is > 0 (Opus 5.5 @ 250k -> 4h)
        self.stream_usage = {"input_tokens": 5, "cache_read_input_tokens": 240000,
                             "cache_creation_input_tokens": 10000}
        self.ping_status = 200
        self.ping_delay = 0.0
        self.lock = threading.Lock()
        self.ping_active = self.ping_max = 0
        self.ping_usage = {"input_tokens": 5, "cache_read_input_tokens": 250000, "cache_creation_input_tokens": 0,
                           "output_tokens": 0}
        self.last_sse = None
        self.ping_script = []
        self.ping_script_by_sid = {}   # sid -> scripted responses (deterministic under concurrent pings)
        self.release = threading.Event()   # unblocks a stalled /v1/stall handler
        self.drip_broken = threading.Event()   # /v1/drip saw its connection closed by the proxy
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):   # /204 -> 204 no body; /v1/stall, /v1/cut -> broken stream; else 200 "hello"
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "raw": b"", "json": {}})
                if self.path in ("/v1/stall", "/v1/cut"):   # one chunk, then silence / abrupt close
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"6\r\nfirst!\r\n")
                    self.wfile.flush()
                    if self.path == "/v1/stall":
                        fake.release.wait(10)
                    self.close_connection = True
                    return
                if self.path == "/v1/nominate":   # response carries a Connection-nominated header
                    self.send_response(200)
                    self.send_header("Connection", "X-Secret-Hop , keep-alive")
                    self.send_header("X-Secret-Hop", "1")
                    self.send_header("X-Keep", "1")
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"hello")
                    return
                if self.path == "/v1/drip":   # 100 ms chunks for up to 30 s; notes when its reader goes away
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for _ in range(300):
                            self.wfile.write(b"5\r\ndrip!\r\n")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        fake.drip_broken.set()
                    self.close_connection = True
                    return
                if self.path == "/v1/short":   # declares 1000 bytes, sends 400, closes
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.write(b"s" * 400)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if self.path == "/v1/304":
                    self.send_response(304)
                    self.send_header("Content-Length", "123")
                    self.end_headers()
                    return
                if self.path == "/v1/204":
                    self.send_response(204)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "5")
                self.end_headers()
                self.wfile.write(b"hello")

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "5")
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                j = json.loads(body)
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "raw": body, "json": j})
                if j.get("stream"):
                    evs = [{"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant",
                            "model": j.get("model"), "content": [], "usage": dict(fake.stream_usage, output_tokens=1)}},
                           {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": S_OUT}},
                           {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
                           {"type": "message_stop"}]
                    sse = b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in evs)
                    fake.last_sse = sse
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for i in range(0, len(sse), 97):   # several chunks
                        c = sse[i:i + 97]
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
                    self.wfile.write(b"0\r\n\r\n")
                    return
                with fake.lock:
                    fake.ping_active += 1
                    fake.ping_max = max(fake.ping_max, fake.ping_active)
                if fake.ping_delay:
                    time.sleep(fake.ping_delay)
                with fake.lock:
                    fake.ping_active -= 1
                # scripted ping responses first (status, error_type, extra headers), then the defaults
                per_sid = fake.ping_script_by_sid.get(self.headers.get("x-claude-code-session-id"))
                status, etype, xh = (per_sid.pop(0) if per_sid else fake.ping_script.pop(0) if fake.ping_script
                                     else (fake.ping_status, "authentication_error", {}))
                if status == 200:
                    out = {"id": "m2", "type": "message", "content": [], "stop_reason": "max_tokens",
                           "usage": fake.ping_usage}
                else:
                    out = {"type": "error", "error": {"type": etype, "message": f"bad {S_AUTH} {S_ERRMSG}"}}
                data = json.dumps(out).encode()
                self.send_response(status)
                for k, v in xh.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def pings(self, sid=None):
        return [r for r in self.requests if r["json"].get("max_tokens") == 0
                and (sid is None or r["headers"].get("x-claude-code-session-id") == sid)]

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.time() + self.offset


def make_body(sid, n_msgs, tools, prompt=S_PROMPT, compaction=False, model=MODEL, lineage="main"):
    """Messages are deterministic per (lineage, index), so a later main request EXTENDS an earlier one;
    like Claude Code, the last message carries the moving cache_control breakpoint."""
    msgs = []
    for i in range(n_msgs):
        if i % 2 == 0:
            text = (ka_proxy.COMPACTION_OPENER + " ... " if (compaction and i == 0) else "") + \
                f"{prompt} {lineage} turn {i}"
            msgs.append({"role": "user", "content": [{"type": "text", "text": text}]})
        else:
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"{S_BODY} {lineage} reply {i}"}]})
    if msgs:
        msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return {"model": model, "max_tokens": 32000, "stream": True,
            "system": [{"type": "text", "text": f"{S_BODY} system", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "tools": [{"name": n, "description": f"{S_BODY} tool", "input_schema": {"type": "object"}} for n in tools],
            "messages": msgs, "metadata": {"user_id": f"user_x_account_y_session_{sid}"}}


def headers_for(sid):
    return {"Authorization": f"Bearer {S_AUTH}", "x-claude-code-session-id": sid,
            "anthropic-version": "2023-06-01", "content-type": "application/json"}


class Base(unittest.TestCase):
    cfg_over = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ka-test-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state, mode=0o700)
        shutil.copytree(MOD["ca_dir"], os.path.join(self.state, "ca"))
        self.ca_pem = os.path.join(self.state, "ca", "ca.pem")
        self.fake = FakeUpstream()
        self.clock = Clock()
        env = {"KA_UPSTREAM": f"http://127.0.0.1:{self.fake.port}", "KA_PORT": "0", "KA_STATE_DIR": self.state,
               "KA_TICK_S": "0.05"}
        cfg = ka_proxy.load_config(env)
        cfg.update(self.cfg_over)
        self.ka = ka_proxy.Proxy(cfg, clock=self.clock)
        self.port = self.ka.start()

    def tearDown(self):
        self.ka.shutdown()
        self.fake.close()
        log = self.logtext()
        for s in SENTINELS:   # case i, enforced after EVERY test
            self.assertNotIn(s, log, f"sentinel {s!r} leaked into ka.log")
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def logtext(self):
        p = os.path.join(self.state, "ka.log")
        if not os.path.exists(p):
            return ""
        with open(p) as f:
            return f.read()

    def events(self, event=None, **match):
        if "sid" in match:   # the log carries the sanitised sid (L9)
            match["sid"] = ka_proxy.log_sid(match["sid"])
        out = [json.loads(l) for l in self.logtext().splitlines() if l.strip()]
        return [e for e in out if (event is None or e["event"] == event)
                and all(e.get(k) == v for k, v in match.items())]

    def wait_for(self, fn, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            r = fn()
            if r:
                return r
            time.sleep(0.02)
        self.fail("condition not met within %.1fs" % timeout)

    def ticks(self, n=6):
        time.sleep(self.ka.cfg["tick"] * n)

    def post(self, sid, n_msgs, tools, **kw):
        body = json.dumps(make_body(sid, n_msgs, tools, **kw)).encode()
        n_real = len(self.events("real"))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/messages?beta=true", body=body, headers=headers_for(sid))
        r = c.getresponse()
        data = r.read()
        c.close()
        # the real event is written right after the response ends; wait until it appears
        self.wait_for(lambda: len(self.events("real")) > n_real)
        return r.status, data, body

    def advance(self, s):
        self.clock.offset += s

    def auth(self):
        """Correct CONNECT Proxy-Authorization for this proxy (token read from STATE_DIR, as ka_ca.py does)."""
        with open(os.path.join(self.state, "proxy.token")) as f:
            return basic_auth(f.read().strip())


class TestKeepalive(Base):
    def test_a_passthrough_byte_identical_and_captured(self):
        status, data, sent = self.post("s-a", 4, MAIN_TOOLS)
        self.assertEqual(status, 200)
        self.assertEqual(data, self.fake.last_sse)                      # client got upstream bytes verbatim
        up = self.fake.requests[-1]
        self.assertEqual(up["raw"], sent)                                # upstream got client bytes verbatim
        self.assertEqual(up["headers"]["authorization"], f"Bearer {S_AUTH}")
        self.assertEqual(up["path"], "/v1/messages?beta=true")
        (e,) = self.events("real", sid="s-a")
        self.assertTrue(e["captured"])
        self.assertFalse(e["compaction_switch"])
        self.assertEqual((e["read"], e["create"], e["out"]), (240000, 10000, 7))
        self.assertAlmostEqual(e["est_cost_usd"], (10000 * 8 + 240000 * 0.2 + 5 * 4) / 1e6)
        self.assertEqual(self.ka.sessions["s-a"]["expected_prefix"], 250000)

    def test_b_idle_ping_replays_with_max_tokens_0(self):
        _, _, sent = self.post("s-b", 4, MAIN_TOOLS)
        self.ticks()
        self.assertEqual(self.fake.pings(), [])                          # not idle yet
        self.advance(3300)
        self.wait_for(lambda: self.events("ping"))
        (p,) = self.fake.pings()
        self.assertEqual(p["json"]["max_tokens"], 0)
        self.assertIs(p["json"]["stream"], False)
        orig = json.loads(sent)
        strip = lambda d: {k: v for k, v in d.items() if k not in ("max_tokens", "stream")}
        self.assertEqual(strip(p["json"]), strip(orig))
        self.assertEqual(p["path"], "/v1/messages?beta=true")
        self.assertEqual(p["headers"]["authorization"], f"Bearer {S_AUTH}")
        (e,) = self.events("ping")
        self.assertTrue(e["ok"])
        self.assertEqual(e["pings"], 1)
        self.assertAlmostEqual(e["avoided_rewarm_usd_if_returned"], 250000 * (8 - 0.2) / 1e6)
        self.advance(3300)                                               # last_refresh moved -> second ping
        self.wait_for(lambda: len(self.events("ping")) == 2)
        e2 = self.events("ping")[1]
        self.assertTrue(e2["ok"])
        self.assertLess(e2["age_s"], 3400)
        self.assertEqual(len(self.fake.pings()), 2)

    def test_c_helper_without_tools_does_not_replace_capture(self):
        self.post("s-c", 4, MAIN_TOOLS)
        self.post("s-c", 1, [])
        self.post("s-c", 6, [])
        self.assertEqual([e["captured"] for e in self.events("real")], [True, False, False])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(len(p["messages"]), 4)
        self.assertEqual([t["name"] for t in p["tools"]], MAIN_TOOLS)

    def test_d_second_session_does_not_stop_the_first(self):
        self.post("s-d1", 4, MAIN_TOOLS)
        self.advance(1)
        self.post("s-d2", 2, MAIN_TOOLS)
        for n in (1, 2):   # two keepalive rounds: both tracks pinged each round
            self.advance(3300)
            self.wait_for(lambda: len(self.fake.pings("s-d1")) == n and len(self.fake.pings("s-d2")) == n)
        self.ticks()
        self.assertEqual(self.events("decision", action="skip"), [])
        self.assertEqual({self.ka.sessions[k]["status"] for k in ("s-d1", "s-d2")}, {"active"})

    def test_w_two_idle_sessions_both_pinged(self):
        """Both idle tracks get their own ping (dispatch is concurrent, so the log
        order is not fixed; per-tick and in-flight caps are asserted in test_dd)."""
        self.post("s-w1", 4, MAIN_TOOLS)
        self.advance(1)
        self.post("s-w2", 4, MAIN_TOOLS)
        self.advance(3300)                     # both due at once
        self.wait_for(lambda: len(self.events("ping", ok=True)) == 2)
        ticks = {e["sid"]: e["tick"] for e in self.events("ping")}
        self.assertEqual(set(ticks), {ka_proxy.log_sid("s-w1"), ka_proxy.log_sid("s-w2")})
        self.assertLessEqual(ticks[ka_proxy.log_sid("s-w1")], ticks[ka_proxy.log_sid("s-w2")])   # urgent first

    def test_e_stop_flag_via_hook(self):
        env = dict(os.environ, KA_STATE_DIR=self.state)
        forms = [[PY, os.path.join(HERE, "ka_off_hook.py")]]
        if BASH:
            forms.append([BASH, os.path.join(HERE, "ka-off-hook.sh")])
        for hook in forms:
            for sid, prompt in (("s-other", "no marker here"), ("s-inline", "add #ka-off to the routines"), ("s-e", "wrap up please\n  #ka-off")):
                r = subprocess.run(hook, input=json.dumps({"session_id": sid, "prompt": prompt,
                                   "hook_event_name": "UserPromptSubmit"}), capture_output=True, text=True, env=env)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            self.assertTrue(os.path.exists(os.path.join(self.state, "stop", "s-e")), hook)
            self.assertFalse(os.path.exists(os.path.join(self.state, "stop", "s-other")))
            self.assertFalse(os.path.exists(os.path.join(self.state, "stop", "s-inline")), hook)   # mid-sentence mention
            r = subprocess.run(hook, input="not json", capture_output=True, text=True, env=env)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            os.remove(os.path.join(self.state, "stop", "s-e"))   # next form must set it again by itself
        open(os.path.join(self.state, "stop", "s-e"), "a").close()
        self.post("s-e", 4, MAIN_TOOLS)        # flag already set -> never captured
        (e,) = self.events("real", sid="s-e")
        self.assertEqual((e["captured"], e["reason"]), (False, "flag"))
        self.assertNotIn("s-e", self.ka.sessions)
        self.advance(3300)
        self.ticks()
        self.assertEqual(self.fake.pings(), [])

    def test_e2_global_off_flag(self):
        self.post("s-e2", 4, MAIN_TOOLS)
        open(os.path.join(self.state, "off"), "w").close()
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="flag", sid="s-e2"))
        self.assertEqual(self.fake.pings(), [])

    def test_f_verify_fail_low_read_stops_no_retry(self):
        self.fake.ping_usage = {"input_tokens": 5, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 990}
        self.post("s-f", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        (e,) = self.events("ping")
        self.assertFalse(e["ok"])
        self.assertIsNone(e["avoided_rewarm_usd_if_returned"])
        self.advance(3300)
        self.ticks()
        self.assertEqual(len(self.fake.pings()), 1)

    def test_f2_http_401_stops_no_retry(self):
        self.fake.ping_status = 401
        self.post("s-f2", 4, MAIN_TOOLS)
        self.advance(3300)
        (d,) = self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        self.assertEqual(d["http_status"], 401)
        self.assertEqual(self.ka.sessions["s-f2"]["status"], "verify-fail")
        self.advance(3300)
        self.ticks()
        self.assertEqual(len(self.fake.pings()), 1)

    def test_g_cap_stops(self):
        self.ka.cfg["cap_opus"] = 1.5          # hours: hard ceiling (= KA_CAP_H_OPUS=1.5) below the dynamic 4h
        self.post("s-g", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("ping"))
        self.advance(3300)                     # idle 6600s > 5400s
        (d,) = self.wait_for(lambda: self.events("decision", reason="cap"))
        self.assertEqual((d["sid"], d["cap_h"]), (ka_proxy.log_sid("s-g"), 1.5))
        self.ticks()
        self.assertEqual(len(self.fake.pings()), 1)

    def test_h_expired_before_ping_no_backlog(self):
        self.post("s-h", 4, MAIN_TOOLS)
        self.advance(3600)                     # laptop slept past TTL-60
        self.wait_for(lambda: self.events("decision", reason="expired-before-ping"))
        self.advance(3300)
        self.ticks()
        self.assertEqual(self.fake.pings(), [])
        self.assertEqual(self.ka.sessions["s-h"]["status"], "expired")

    def test_h2_expired_after_one_ping(self):
        self.post("s-h2", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("ping"))
        self.advance(3545)                     # > TTL-60 since last refresh
        self.wait_for(lambda: self.events("decision", reason="expired-before-ping"))
        self.assertEqual(len(self.fake.pings()), 1)

    def test_i_log_has_no_secrets(self):
        self.post("s-i", 4, MAIN_TOOLS)
        self.post("s-i", 6, MAIN_TOOLS, compaction=False)
        self.advance(3300)
        self.wait_for(lambda: self.events("ping"))
        self.fake.ping_status = 401
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        log = self.logtext()
        self.assertIn('"event": "ping"', log)
        for s in SENTINELS + ("Bearer", "2023-06-01"):
            self.assertNotIn(s, log)

    def test_k_subagent_conversation_does_not_replace(self):
        self.post("s-k", 4, MAIN_TOOLS)
        self.post("s-k", 8, SUB_TOOLS, lineage="sub")   # subagent: same sid, own history, more msgs
        self.assertEqual([e["captured"] for e in self.events("real")], [True, False])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (MAIN_TOOLS, 4))

    def test_k2_subagent_first_then_main_with_more_tools(self):
        """Reviewer scenario: proxy starts mid-session and the first request seen is a subagent's."""
        self.post("s-k2", 2, SUB_TOOLS, lineage="sub")   # first tools-bearing -> provisional main
        self.post("s-k2", 2, MAIN_TOOLS)                  # different history, strictly more tools -> main
        self.post("s-k2", 4, SUB_TOOLS, lineage="sub")    # subagent continues: not an extension of main
        self.post("s-k2", 4, MAIN_TOOLS)                  # main extends itself -> main
        self.assertEqual([e["captured"] for e in self.events("real")], [True, True, False, True])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (MAIN_TOOLS, 4))

    def test_k3_tools_change_mid_session_and_cache_control_moves(self):
        more = MAIN_TOOLS + ["ToolSearchLoaded"]
        self.post("s-k3", 4, MAIN_TOOLS)
        self.post("s-k3", 6, more)             # ToolSearch added a tool; history extends -> still main
        self.post("s-k3", 2, more)             # same tools, fewer msgs, no marker (helper) -> not main
        self.post("s-k3", 8, more)             # extends again; cache_control moved every turn
        self.assertEqual([e["captured"] for e in self.events("real")], [True, True, False, True])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (more, 8))

    def test_k4_message_hash_ignores_cache_control_only(self):
        a = [{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}]
        b = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        c = [{"role": "user", "content": [{"type": "text", "text": "y"}]}]
        self.assertEqual(ka_proxy.message_hashes(a), ka_proxy.message_hashes(b))
        self.assertNotEqual(ka_proxy.message_hashes(b), ka_proxy.message_hashes(c))

    def test_k2b_subagent_first_ping_replays_main_tools(self):
        self.post("s-k2b", 2, SUB_TOOLS, lineage="sub")
        self.post("s-k2b", 2, MAIN_TOOLS)
        self.post("s-k2b", 4, SUB_TOOLS, lineage="sub")
        self.assertEqual([e["captured"] for e in self.events("real")], [True, True, False])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        self.assertEqual([t["name"] for t in self.fake.pings()[0]["json"]["tools"]], MAIN_TOOLS)

    def test_l_same_tools_fewer_msgs_no_marker_does_not_replace(self):
        self.post("s-l", 6, MAIN_TOOLS)
        self.post("s-l", 2, MAIN_TOOLS)
        self.assertEqual([e["captured"] for e in self.events("real")], [True, False])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        self.assertEqual(len(self.fake.pings()[0]["json"]["messages"]), 6)

    def test_m_compaction_marker_replaces(self):
        self.post("s-m", 6, MAIN_TOOLS)
        self.post("s-m", 2, MAIN_TOOLS, compaction=True)
        ev = self.events("real")
        self.assertEqual([(e["captured"], e["compaction_switch"]) for e in ev], [(True, False), (True, True)])
        self.post("s-m", 4, MAIN_TOOLS, compaction=True)   # post-compaction growth extends the summary
        self.assertEqual(self.events("real")[-1]["captured"], True)
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        self.assertEqual(len(self.fake.pings()[0]["json"]["messages"]), 4)

    def test_controller_error_keeps_passthrough(self):
        self.post("s-x", 4, MAIN_TOOLS)
        self.ka.sessions["s-x"]["body"] = b"\x00not json"
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="controller-error"))
        status, data, _ = self.post("s-x", 6, MAIN_TOOLS)
        self.assertEqual((status, data), (200, self.fake.last_sse))
        self.assertEqual(self.fake.pings(), [])

    def test_real_request_resets_idle_and_status(self):
        self.fake.ping_status = 401
        self.post("s-r", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        self.fake.ping_status = 200
        self.post("s-r", 6, MAIN_TOOLS)
        s = self.ka.sessions["s-r"]
        self.assertEqual((s["status"], s["pings"]), ("active", 0))
        self.advance(3300)
        self.wait_for(lambda: self.events("ping", ok=True))


class TestPingErrors(Base):
    def upstream_pings(self):
        return len(self.fake.pings())

    def test_s_429_large_retry_after_stops_without_retry(self):
        self.fake.ping_script = [(429, "rate_limit_error", {
            "retry-after": "3600", "anthropic-ratelimit-unified-reset": "1790000000",
            "anthropic-ratelimit-tokens-remaining": "0", "x-should-retry": "false",
            "anthropic-ratelimit-odd": "free text here"})]
        self.post("s-s", 4, MAIN_TOOLS)
        self.advance(3300)
        (d,) = self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        (p,) = self.events("ping")
        self.assertEqual((p["ok"], p["retry"], p["status"], p["error_type"], p["retry_after"]),
                         (False, False, 429, "rate_limit_error", "3600"))
        self.assertEqual(p["rl_headers"], {"anthropic-ratelimit-unified-reset": "1790000000",
                                           "anthropic-ratelimit-tokens-remaining": "0",
                                           "x-should-retry": "false", "anthropic-ratelimit-odd": None})
        self.assertEqual((d["http_status"], d["error_type"], d["retry_after"]), (429, "rate_limit_error", "3600"))
        self.assertEqual(self.events("decision", action="retry"), [])
        self.advance(3300)
        self.ticks()
        self.assertEqual(self.upstream_pings(), 1)

    def test_t_529_then_200_one_retry_succeeds(self):
        self.ka.cfg["retry_default"] = 0.2
        self.fake.ping_script = [(529, "overloaded_error", {})]
        self.post("s-t", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("ping", ok=True))
        first, second = self.events("ping")
        self.assertEqual((first["ok"], first["retry"], first["status"], first["error_type"]),
                         (False, False, 529, "overloaded_error"))
        self.assertEqual((second["ok"], second["retry"], second["pings"]), (True, True, 1))
        self.assertNotIn("error_type", second)
        (r,) = self.events("decision", action="retry")
        self.assertEqual((r["http_status"], r["wait_s"]), (529, 0.2))
        self.assertEqual(self.events("decision", action="stop"), [])
        s = self.ka.sessions["s-t"]
        self.assertEqual((s["status"], s["pings"]), ("active", 1))
        self.assertEqual(self.upstream_pings(), 2)

    def test_u_429_short_retry_after_retries_once_then_stops(self):
        self.fake.ping_script = [(429, "rate_limit_error", {"retry-after": "1"}),
                                 (429, "rate_limit_error", {"retry-after": "1"})]
        self.post("s-u", 4, MAIN_TOOLS)
        self.advance(3300)                     # window = min(120, 3600-30-3300) = 120 >= 1
        self.wait_for(lambda: self.events("decision", reason="verify-fail"), timeout=5)
        self.assertEqual([(p["retry"], p["ok"]) for p in self.events("ping")], [(False, False), (True, False)])
        self.assertEqual(len(self.events("decision", action="retry")), 1)
        self.advance(3300)
        self.ticks()
        self.assertEqual(self.upstream_pings(), 2)

    def test_u2_400_never_retried_even_if_overloaded_type(self):
        self.fake.ping_script = [(400, "overloaded_error", {"retry-after": "1"})]
        self.post("s-u2", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        self.ticks()
        self.assertEqual(self.events("decision", action="retry"), [])
        self.assertEqual(self.upstream_pings(), 1)

    def test_v_error_bodies_never_reach_log(self):
        self.ka.cfg["retry_default"] = 0.1
        self.fake.ping_script = [(529, "overloaded_error", {}), (429, "rate_limit_error", {"retry-after": "3600"})]
        self.post("s-v", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        log = self.logtext()
        self.assertIn('"error_type": "rate_limit_error"', log)
        for s in SENTINELS + ("bad ", "Bearer"):
            self.assertNotIn(s, log)


KACTL = os.path.join(HERE, "kactl")
CONTRACT_TOP = {"schema", "updated", "proxy_pid", "global_off", "sessions", "today"}
CONTRACT_SESSION = {"sid", "model", "prefix_tokens", "last_real_ts", "last_refresh_ts", "next_ping_ts", "pings",
                    "cap_h", "cap_end_ts", "state", "stop_reason", "est_ping_usd", "est_rewarm_usd"}
CONTRACT_STATES = {"active", "retry-wait", "stopped", "capped", "expired", "verify-fail", "over-max"}
SA, SB, SC = ("aaaa0001-1111-4111-8111-000000000001", "bbbb0002-2222-4222-8222-000000000002",
              "cccc0003-3333-4333-8333-000000000003")


class TestMultiSession(Base):
    def status(self):
        p = os.path.join(self.state, "status.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def row(self, sid):
        st = self.status() or {}
        return next((r for r in st.get("sessions", []) if r["sid"] == ka_proxy.log_sid(sid)), None)

    def kactl(self, *args, session=None):
        """session = CLAUDE_CODE_SESSION_ID for the call; never inherited from the test runner."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["KA_STATE_DIR"] = self.state
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run([PY, KACTL, *args], capture_output=True, text=True, env=env)

    def test_x_max_sessions_keeps_most_recent(self):
        self.ka.cfg["max_sessions"] = 1
        self.post("s-x1", 4, MAIN_TOOLS)
        self.advance(1)
        self.post("s-x2", 4, MAIN_TOOLS)
        (d,) = self.wait_for(lambda: self.events("decision", reason="over-max-sessions"))
        self.assertEqual((d["action"], d["sid"]), ("skip", ka_proxy.log_sid("s-x1")))
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings("s-x2"))
        self.ticks()
        self.assertEqual(self.fake.pings("s-x1"), [])
        self.assertEqual(len(self.events("decision", reason="over-max-sessions")), 1)   # once per dropped sid
        self.wait_for(lambda: (self.row("s-x1") or {}).get("state") == "over-max")
        self.assertIsNone(self.ka.sessions["s-x1"]["body"])

    def test_y_kactl_stop_resume_off_on(self):
        for sid in (SA, SB, SC):
            self.post(sid, 4, MAIN_TOOLS)
        self.wait_for(lambda: self.status() and len(self.status()["sessions"]) == 3)
        r = self.kactl("stop", "aaaa")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_for(lambda: self.events("decision", reason="flag", sid=SA))
        self.ticks()
        self.assertEqual([self.ka.sessions[s]["status"] for s in (SA, SB, SC)], ["stopped", "active", "active"])
        self.assertIsNone(self.ka.sessions[SA]["body"])            # dropped from memory within one tick
        self.assertIsNone(self.ka.sessions[SA]["headers"])
        out = self.kactl("status", "all").stdout
        self.assertIn("aaaa0001", out)
        self.assertIn("stopped (flag) [stop flag]", out)
        self.assertNotEqual(self.kactl("stop", "zzzz").returncode, 0)   # no match -> error
        self.assertEqual(self.kactl("stop", "all").returncode, 0)        # = global off
        self.wait_for(lambda: len(self.events("decision", reason="flag")) == 3)
        self.assertTrue(os.path.exists(os.path.join(self.state, "off")))
        self.assertEqual(self.kactl("resume", "all").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "off")))
        self.assertEqual(os.listdir(os.path.join(self.state, "stop")), [])
        self.post(SB, 6, MAIN_TOOLS)                                     # restarts on its next real request
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings(SB))
        self.assertEqual(self.fake.pings(SA), [])
        self.assertEqual(self.kactl("off").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.state, "off")))
        self.assertEqual(self.kactl("on").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "off")))
        self.assertEqual(self.kactl("stop", SC[:6]).returncode, 0)
        self.assertEqual(self.kactl("resume", SC[:6]).returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop", SC)))

    def test_z_status_json_contract_and_no_secrets(self):
        weird = f"weird sid {S_PROMPT}"                  # not uuid-like: must appear only as a hash tag
        hexy = "0123456789abcdef" * 2                 # token-like hex, not a canonical UUID -> hashed too
        self.post(SA, 4, MAIN_TOOLS)
        self.post(weird, 4, MAIN_TOOLS)
        self.post(hexy, 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: len(self.events("ping", ok=True)) == 3)
        self.wait_for(lambda: self.status() and self.status()["today"]["pings"] == 3)
        p = os.path.join(self.state, "status.json")
        self.assertTrue(mode_ok(p, 0o600))
        with open(p) as f:
            text = f.read()
        st = json.loads(text)
        self.assertEqual(set(st), CONTRACT_TOP)
        self.assertEqual((st["schema"], st["global_off"]), (1, False))
        for r in st["sessions"]:
            self.assertEqual(set(r), CONTRACT_SESSION)
            self.assertIn(r["state"], CONTRACT_STATES)
        a = self.row(SA)
        self.assertEqual((a["state"], a["pings"], a["prefix_tokens"], a["cap_h"]), ("active", 1, 250000, 4))
        self.assertAlmostEqual(a["est_rewarm_usd"], (250000 - 30000) * 7.8 / 1e6)
        self.assertAlmostEqual(a["est_ping_usd"], (250000 * 0.2 + 3845 * 4) / 1e6)
        self.assertAlmostEqual(a["cap_end_ts"] - a["last_real_ts"], 4 * 3600, places=0)
        self.assertEqual(self.row(weird)["sid"], ka_proxy.log_sid(weird))
        self.assertTrue(ka_proxy.log_sid(weird).startswith("h-"))
        self.assertEqual(set(st["today"]), {"pings", "ping_usd", "avoided_rewarm_usd", "stops"})
        out = self.kactl("status", "all").stdout
        for s in SENTINELS + ("Bearer", "weird sid"):
            self.assertNotIn(s, text)
            self.assertNotIn(s, out)
        self.assertNotIn("weird sid", self.logtext())                   # L9 in ka.log too
        for where in (text, self.logtext(), out):
            self.assertNotIn(hexy, where)
        self.assertEqual(self.row(SA)["sid"], SA)                       # canonical UUID stays readable

    def ext_path(self, sid):
        return os.path.join(self.state, "extend", sid)

    def test_ext1_extend_beyond_cap_pings_until_deadline_then_stops(self):
        self.ka.cfg["cap_opus"] = 1.5                                      # dynamic cap end = +5400 s
        self.post(SA, 4, MAIN_TOOLS)
        r = self.kactl("extend", "2", session=SA)                          # deadline = now + 7200 s
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(mode_ok(self.ext_path(SA), 0o600))
        row = self.wait_for(lambda: (self.row(SA) or {}).get("extend_until_ts") and self.row(SA))
        self.assertAlmostEqual(row["cap_end_ts"], row["extend_until_ts"], places=3)   # effective cap end
        self.assertGreater(row["cap_end_ts"] - row["last_real_ts"], 7000)
        self.assertLessEqual(set(row), CONTRACT_SESSION | {"extend_until_ts"})
        for n in (1, 2):                                                   # 3300 s, 6600 s (> dynamic 5400)
            self.advance(3300)
            self.wait_for(lambda: len(self.events("ping", ok=True, sid=SA)) == n)
        self.ticks()
        self.assertEqual(self.ka.sessions[SA]["status"], "active")
        self.advance(700)                                                  # 7300 s > extension deadline
        (d,) = self.wait_for(lambda: self.events("decision", reason="cap", sid=SA))
        self.wait_for(lambda: not os.path.exists(self.ext_path(SA)))       # expired file pruned
        self.wait_for(lambda: (self.row(SA) or {}).get("state") == "capped")
        self.assertNotIn("extend_until_ts", self.row(SA))
        with open(os.path.join(self.state, "status.json")) as f:
            text = f.read()
        for s in SENTINELS + ("Bearer",):
            self.assertNotIn(s, text)

    def test_ext2_extend_keeps_cap0_session(self):
        self.fake.stream_usage = {"input_tokens": 5, "cache_read_input_tokens": 38000,
                                  "cache_creation_input_tokens": 2000}   # dynamic cap 0
        r = self.kactl("extend", "1", session="s-ext2")                    # before the first message
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not tracked yet", r.stdout)
        self.post("s-ext2", 4, MAIN_TOOLS)
        self.ticks()
        self.assertEqual(self.ka.sessions["s-ext2"]["cap_h"], 0)
        self.assertEqual(self.ka.sessions["s-ext2"]["status"], "active")   # kept despite cap 0
        self.advance(3300)
        self.wait_for(lambda: self.events("ping", ok=True, sid="s-ext2"))
        self.advance(400)                                                  # past the 1 h extension
        self.wait_for(lambda: self.events("decision", reason="cap", sid="s-ext2"))

    def test_ext3_extend_bounded_by_max_hours(self):
        self.ka.cfg["cap_opus"] = 0.05                                     # dynamic cap end = +180 s
        self.ka.cfg["extend_max_h"] = 0.5
        self.post(SA, 4, MAIN_TOOLS)
        os.makedirs(os.path.join(self.state, "extend"), mode=0o700)
        with open(self.ext_path(SA), "w") as f:                            # hand-written 20 h deadline
            f.write(f"{self.clock() + 20 * 3600}\n")
        t_set = self.clock() - 1440                                         # written 24 min ago
        os.utime(self.ext_path(SA), (t_set, t_set))
        row = self.wait_for(lambda: (self.row(SA) or {}).get("extend_until_ts") and self.row(SA))
        self.assertAlmostEqual(row["extend_until_ts"], t_set + 1800, delta=1)   # set time + 0.5 h, not 20 h
        self.advance(400)
        self.wait_for(lambda: self.events("decision", reason="cap", sid=SA))
        self.wait_for(lambda: not os.path.exists(self.ext_path(SA)))
        for bad in ("25", "-1", "nan", "inf", "x"):                         # kactl's own bound
            r = self.kactl("extend", bad, session=SB)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertFalse(os.path.exists(self.ext_path(SB)), bad)

    def test_ext4_extend_zero_removes(self):
        self.post(SA, 4, MAIN_TOOLS)
        self.wait_for(lambda: self.row(SA))                                # prefix resolves via status.json
        self.assertEqual(self.kactl("extend", "aaaa", "3").returncode, 0)   # by prefix
        self.assertTrue(os.path.exists(self.ext_path(SA)))
        self.wait_for(lambda: (self.row(SA) or {}).get("extend_until_ts"))
        r = self.kactl("extend", "0", session=SA)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"extension removed for {SA[:12]}"))
        self.assertFalse(os.path.exists(self.ext_path(SA)))
        self.wait_for(lambda: self.row(SA) and "extend_until_ts" not in self.row(SA))

    def test_kactl_defaults_to_claude_code_session_id(self):
        for args in (["status"], ["stop"], ["resume"], ["start"], ["extend", "2"]):   # no env: clean refusal
            r = self.kactl(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(r.stderr.strip().count("\n"), 0, args)
            self.assertIn("CLAUDE_CODE_SESSION_ID is unset", r.stderr, args)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "extend")))
        r = self.kactl("status", session=SB)                                # not tracked yet
        self.assertEqual(r.returncode, 1)
        self.assertIn("not tracked yet — send one message through the proxy first", r.stdout)
        self.post(SA, 4, MAIN_TOOLS)
        self.post(SB, 4, MAIN_TOOLS)
        self.wait_for(lambda: self.row(SA) and self.row(SB))
        r = self.kactl("status", session=SA)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        for s in ("aaaa0001", "active", "prefix 250000", "idle", "next ping in", "cap end", "pings 0"):
            self.assertIn(s, r.stdout)
        self.assertNotIn("bbbb0002", r.stdout)
        self.assertIn("bbbb0002", self.kactl("status", "all").stdout)       # old all-sessions view
        self.assertEqual(self.kactl("stop", session=SA).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.state, "stop", SA)))
        self.wait_for(lambda: self.ka.sessions[SA]["status"] == "stopped")
        self.assertEqual(self.ka.sessions[SB]["status"], "active")
        self.assertIn("[stop flag]", self.kactl("status", session=SA).stdout)
        self.assertEqual(self.kactl("start", session=SA).returncode, 0)   # alias of resume
        self.assertFalse(os.path.exists(os.path.join(self.state, "stop", SA)))
        self.assertIn("bbbb0002", self.kactl("status", "bbbb").stdout)     # explicit prefix still works

    def test_ret1_capped_track_dropped_after_2h_active_kept(self):
        self.assertEqual(ka_proxy.load_config({})["inactive_retain_h"], 2)
        self.ka.cfg["cap_opus"] = 0.05                                     # dynamic cap end = +180 s
        self.post(SA, 4, MAIN_TOOLS)
        self.post(SB, 4, MAIN_TOOLS)
        self.assertEqual(self.kactl("extend", "3", session=SB).returncode, 0)   # SB stays active 3 h
        self.advance(200)
        self.wait_for(lambda: (self.row(SA) or {}).get("state") == "capped")
        for n, step in ((1, 3300), (2, 3300)):                             # t = 3500, 6800
            self.advance(step)
            self.wait_for(lambda: len(self.events("ping", ok=True, sid=SB)) == n)
        self.wait_for(lambda: self.status()["updated"] >= self.clock() - 1)
        self.assertEqual(self.row(SA)["state"], "capped")                  # 6600 s inactive: still listed
        self.advance(700)                                                  # 7300 s inactive > 2 h
        self.wait_for(lambda: SA not in self.ka.sessions and SA not in self.ka.meta and self.row(SA) is None)
        self.assertEqual((self.ka.sessions[SB]["status"], self.row(SB)["state"]), ("active", "active"))

    def test_ret2_dropped_stopped_track_reappears_after_flag_cleared(self):
        self.post(SC, 4, MAIN_TOOLS)
        self.assertEqual(self.kactl("stop", session=SC).returncode, 0)
        self.wait_for(lambda: self.ka.sessions[SC]["status"] == "stopped")
        self.advance(7300)
        self.wait_for(lambda: SC not in self.ka.sessions and self.row(SC) is None)
        self.assertTrue(os.path.exists(os.path.join(self.state, "stop", SC)))   # control file left alone
        self.post(SC, 6, MAIN_TOOLS)                                       # flag still set: not captured
        self.assertEqual(self.events("real", sid=SC)[-1]["reason"], "flag")
        self.assertNotIn(SC, self.ka.sessions)
        self.assertEqual(self.kactl("start", session=SC).returncode, 0)
        self.post(SC, 8, MAIN_TOOLS)
        self.assertTrue(self.events("real", sid=SC)[-1]["captured"])
        self.wait_for(lambda: (self.row(SC) or {}).get("state") == "active")

    def test_aa_pending_retry_does_not_delay_other_session(self):
        self.fake.ping_script_by_sid = {SA: [(429, "rate_limit_error", {"retry-after": "60"})]}   # A only
        self.post(SA, 4, MAIN_TOOLS)
        self.advance(1)
        self.post(SB, 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: self.events("ping", ok=True, sid=SB), timeout=3)   # B not held by A's 60 s wait
        self.assertEqual(len(self.fake.pings(SA)), 1)
        a = self.wait_for(lambda: (self.row(SA) or {}).get("state") == "retry-wait" and self.row(SA))
        self.assertGreater(a["next_ping_ts"] - self.clock(), 50)

    def test_bb_dynamic_cap_sanity_rows(self):
        cfg = ka_proxy.load_config({})
        for model, prefix, want in (("claude-fable-5-1", 100000, 4), ("claude-opus-5-5", 100000, 3),
                                    ("claude-fable-5-1", 250000, 5), ("claude-opus-5-5", 250000, 4),
                                    ("claude-fable-5-1", 500000, 9), ("claude-opus-5-5", 500000, 5)):
            got = ka_proxy.dynamic_cap_hours(cfg, model, prefix)
            self.assertLessEqual(abs(got - want), 1, (model, prefix, got, want))
        self.assertEqual(ka_proxy.dynamic_cap_hours(cfg, "some-other-model", 250000), 4)   # KA_CAP_H_DEFAULT
        self.assertEqual(ka_proxy.dynamic_cap_hours(dict(cfg, cap_fable=2), "claude-fable-5-1", 500000), 2)

    def test_cc_tiny_prefix_cap_zero_no_pings(self):
        cfg = ka_proxy.load_config({})
        for model in ("claude-fable-5-1", "claude-opus-5-5"):
            self.assertEqual(ka_proxy.dynamic_cap_hours(cfg, model, 40000), 0)
        self.fake.stream_usage = {"input_tokens": 5, "cache_read_input_tokens": 38000,
                                  "cache_creation_input_tokens": 2000}
        self.post("s-cc", 4, MAIN_TOOLS)
        (d,) = self.wait_for(lambda: self.events("decision", reason="cap", sid="s-cc"))
        self.assertEqual(d["cap_h"], 0)
        self.advance(3300)
        self.ticks()
        self.assertEqual(self.fake.pings(), [])
        self.wait_for(lambda: (self.row("s-cc") or {}).get("state") == "capped")

    def test_l12_ping_result_ignored_if_stopped_in_flight(self):
        self.fake.ping_delay = 0.5
        self.post("s-l12", 4, MAIN_TOOLS)
        t_start = self.ka.sessions["s-l12"]["last_refresh"]
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())                          # ping is in flight at the fake
        with self.ka.lock:                                                 # concurrent stop while in flight
            self.ka._stop("s-l12", self.ka.sessions["s-l12"], "flag")
        self.wait_for(lambda: self.events("ping", ok=True))
        self.ticks()
        s = self.ka.sessions["s-l12"]
        self.assertEqual((s["status"], s["pings"], s["last_refresh"]), ("stopped", 0, t_start))


class TestReview2(Base):
    def test_dd_eight_due_sessions_all_pinged_with_slow_upstream(self):
        """8 tracks due together, 2 s per ping upstream, window only 12 s of clock: a serial scheduler
        would need 16 s and expire the tail; 3-way concurrency finishes in ~6 s."""
        self.ka.cfg["ttl"] = 3300 + 60 + 12      # TTL - margin - ping_after = 12 s window
        self.fake.ping_delay = 2.0
        sids = [f"dd{i}" for i in range(8)]
        for sid in sids:
            self.post(sid, 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: len(self.events("ping", ok=True)) == 8, timeout=12)
        self.assertEqual(self.events("decision", reason="expired-before-ping"), [])
        self.assertLessEqual(self.fake.ping_max, 3)                       # KA_PING_CONCURRENCY
        per_tick = collections.Counter(e["tick"] for e in self.events("ping"))
        self.assertLessEqual(max(per_tick.values()), 3)                  # <= 3 new dispatches per tick
        self.assertTrue(all(self.ka.sessions[s]["pings"] == 1 for s in sids))

    def test_r3_5_ping_concurrency_clamped(self):
        for raw, want in (("8", 3), ("3", 3), ("2", 2), ("1", 1), ("0", 1), ("-4", 1)):
            self.assertEqual(ka_proxy.load_config({"KA_PING_CONCURRENCY": raw})["ping_concurrency"], want, raw)
        self.assertEqual(ka_proxy.load_config({})["ping_concurrency"], 3)

    def test_dd2_ping_rechecks_age_before_sending(self):
        self.post("s-dd2", 4, MAIN_TOOLS)
        s = self.ka.sessions["s-dd2"]
        snap = {k: s[k] for k in ("path", "headers", "body", "gen", "model", "expected_prefix",
                                  "last_refresh", "idle_start", "pings")}
        self.advance(3545)                                                 # past TTL - margin at send time
        self.ka._ping("s-dd2", snap)
        (d,) = self.events("decision", reason="expired-before-ping")
        self.assertEqual(d["at"], "send")
        self.assertEqual(self.fake.pings(), [])

    def test_ee_meta_pruned_with_sessions_and_orphans(self):
        self.post("s-ee", 4, MAIN_TOOLS)
        self.assertIn("s-ee", self.ka.meta)
        open(os.path.join(self.state, "off"), "w").close()
        self.wait_for(lambda: self.ka.sessions["s-ee"]["status"] == "stopped")
        with self.ka.lock:                                                 # never-captured orphan meta
            self.ka.meta["ghost"] = {"hashes": ["x"], "n_tools": 1, "n_msgs": 1, "last_seen": self.clock()}
        self.advance(86401)
        self.wait_for(lambda: "s-ee" not in self.ka.sessions and "s-ee" not in self.ka.meta
                      and "ghost" not in self.ka.meta)

    def test_ff_flagged_real_request_not_stored(self):
        self.post("s-ff", 4, MAIN_TOOLS)
        os.makedirs(os.path.join(self.state, "stop"), exist_ok=True)
        open(os.path.join(self.state, "stop", "s-ff"), "w").close()
        self.wait_for(lambda: self.ka.sessions["s-ff"]["status"] == "stopped")
        self.post("s-ff", 6, MAIN_TOOLS)                                  # real request after the stop
        e = self.events("real", sid="s-ff")[-1]
        self.assertEqual((e["captured"], e["reason"]), (False, "flag"))
        s = self.ka.sessions["s-ff"]
        self.assertEqual((s["status"], s["body"], s["headers"]), ("stopped", None, None))
        open(os.path.join(self.state, "off"), "w").close()                # global off as well
        self.post("s-ff2", 4, MAIN_TOOLS)
        self.assertEqual(self.events("real", sid="s-ff2")[-1]["captured"], False)
        self.assertNotIn("s-ff2", self.ka.sessions)

    def test_r3_4_flag_written_during_capture_drops_it(self):
        orig = self.ka._capture

        def racing_capture(*a, **kw):   # an external stop/off writer lands right between check and capture
            orig(*a, **kw)
            open(os.path.join(self.state, "off"), "w").close()

        self.ka._capture = racing_capture
        self.post("s-r34", 4, MAIN_TOOLS)
        (e,) = self.events("real", sid="s-r34")
        self.assertEqual((e["captured"], e["reason"]), (False, "flag"))
        (d,) = self.events("decision", reason="flag", sid="s-r34")
        self.assertEqual(d["at"], "capture")
        s = self.ka.sessions["s-r34"]
        self.assertEqual((s["status"], s["body"], s["headers"]), ("stopped", None, None))
        self.advance(3300)
        self.ticks()
        self.assertEqual(self.fake.pings(), [])

    def test_gg_status_json_0600_despite_leftover_0644_tmp(self):
        tmp = os.path.join(self.state, "status.json.tmp")
        with open(tmp, "w") as f:
            f.write("stale")
        os.chmod(tmp, 0o644)
        self.post("s-gg", 4, MAIN_TOOLS)
        p = os.path.join(self.state, "status.json")
        self.wait_for(lambda: os.path.exists(p) and not os.path.exists(tmp)
                      and "s-gg" in self.ka.sessions and mode_ok(p, 0o600))
        with open(p) as f:
            self.assertEqual(json.load(f)["schema"], 1)

    def test_hh_stale_main_switches_to_active_lineage(self):
        """Reviewer scenario: proxy starts mid-session; first capture is a subagent that has MORE tools
        than main. Main then keeps extending while the stored subagent goes stale -> main-switch."""
        big_sub = MAIN_TOOLS + ["Extra1"]                                   # 6 tools, subagent
        self.post("s-hh", 2, big_sub, lineage="sub")                        # captured first (wrongly)
        self.post("s-hh", 4, big_sub, lineage="sub")                        # sub extends -> confirmed
        self.advance(1200)
        self.post("s-hh", 20, MAIN_TOOLS)                                   # main: fewer tools, no ext
        for n in (22, 24, 26):                                              # 3 extensions, sub stale
            self.advance(190)
            self.post("s-hh", n, MAIN_TOOLS)
        self.assertEqual(self.events("decision", action="main-switch"), [])   # only ~1770 s since sub ext
        self.advance(40)
        self.post("s-hh", 28, MAIN_TOOLS)                                   # >= 1800 s stale, >= 3 exts
        (d,) = self.wait_for(lambda: self.events("decision", action="main-switch"))
        self.assertEqual(d["reason"], "stale-main")
        self.assertEqual([e["captured"] for e in self.events("real")], [True, True, False, False, False, False, True])
        sub4 = self.ka.meta["s-hh"]["previous_main"]
        self.assertEqual(len(sub4), 4)                                      # displaced lineage kept
        # accepted trade-off (orchestrator decision 2026-09-23): the mistaken subagent speaking again takes
        # the capture back once (switchback); main resuming after a long subagent is the common case
        self.post("s-hh", 6, big_sub, lineage="sub")
        (b,) = self.wait_for(lambda: self.events("decision", action="main-switchback"))
        self.assertIsNone(self.ka.meta["s-hh"]["previous_main"])
        self.post("s-hh", 30, MAIN_TOOLS)                                   # main is "other" again
        self.advance(1250)
        for n in (32, 34, 36):
            self.post("s-hh", n, MAIN_TOOLS)
            self.advance(190)
        self.post("s-hh", 38, MAIN_TOOLS)                                   # sub 1820 s stale, >= 3 exts
        self.assertEqual([e["captured"] for e in self.events("real")][-6:], [True, False, False, False, False, True])
        self.assertEqual(len(self.events("decision", action="main-switch")), 2)
        self.assertEqual(len(self.ka.meta["s-hh"]["previous_main"]), 6)   # subagent recorded again
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (MAIN_TOOLS, 38))

    def test_hh2_long_subagent_does_not_displace_long_main(self):
        self.post("s-hh2", 40, MAIN_TOOLS)
        for n in range(2, 20, 2):                                           # subagent runs ~29 min
            self.advance(195)
            self.post("s-hh2", n, MAIN_TOOLS[:3], lineage="sub")
        self.assertEqual(self.events("decision", action="main-switch"), [])
        self.assertEqual(len(self.ka.meta["s-hh2"]["hashes"]), 40)
        # a subagent still extending after main has been idle 30 min DOES displace main (no message-count
        # guard); main's next request takes it back at once via previous_main (test_r4_*)
        self.advance(60)
        self.post("s-hh2", 20, MAIN_TOOLS[:3], lineage="sub")
        self.wait_for(lambda: self.events("decision", action="main-switch"))
        self.assertEqual(len(self.ka.meta["s-hh2"]["hashes"]), 20)

    def takeover(self, sid):
        """Main (40 msgs) displaced by a subagent that keeps extending for > 30 min."""
        self.post(sid, 40, MAIN_TOOLS)
        for n in range(2, 22, 2):                                           # last one at ~1815 s
            self.advance(195 if n < 20 else 60)
            self.post(sid, n, MAIN_TOOLS[:3], lineage="sub")
        (d,) = self.wait_for(lambda: self.events("decision", action="main-switch", sid=sid))
        self.assertEqual(len(self.ka.meta[sid]["previous_main"]), 40)

    def test_r4_switchback_on_single_main_request(self):
        self.takeover("s-r41")
        self.post("s-r41", 42, MAIN_TOOLS)                                  # main resumes: one request only
        (b,) = self.wait_for(lambda: self.events("decision", action="main-switchback", sid="s-r41"))
        self.assertEqual((b["reason"], b["n_msgs"]), ("previous-main", 42))
        self.assertTrue(self.events("real", sid="s-r41")[-1]["captured"])
        m = self.ka.meta["s-r41"]
        self.assertEqual((len(m["hashes"]), m["previous_main"], m["other"]), (42, None, None))
        self.advance(3300)                                                  # main then idles
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (MAIN_TOOLS, 42))

    def test_r4_no_reflap_after_switchback(self):
        self.takeover("s-r42")
        self.post("s-r42", 42, MAIN_TOOLS)
        self.wait_for(lambda: self.events("decision", action="main-switchback"))
        for n in range(22, 40, 2):                                          # subagent alone, ~29 min
            self.advance(195)
            self.post("s-r42", n, MAIN_TOOLS[:3], lineage="sub")
        self.assertEqual(len(self.events("decision", action="main-switch")), 1)   # only the first takeover
        self.assertEqual({e["captured"] for e in self.events("real")[-9:]}, {False})
        self.assertEqual(len(self.ka.meta["s-r42"]["hashes"]), 42)
        self.advance(60)
        self.post("s-r42", 40, MAIN_TOOLS[:3], lineage="sub")               # full 30-min/3-ext rule met again
        self.wait_for(lambda: len(self.events("decision", action="main-switch")) == 2)
        self.assertEqual(len(self.ka.meta["s-r42"]["previous_main"]), 42)
        self.assertEqual(len(self.events("decision", action="main-switchback")), 1)

    def test_r5_compaction_clears_previous_main(self):
        self.takeover("s-r5")
        self.post("s-r5", 2, MAIN_TOOLS, compaction=True)                   # main compacts
        e = self.events("real", sid="s-r5")[-1]
        self.assertEqual((e["captured"], e["compaction_switch"]), (True, True))
        m = self.ka.meta["s-r5"]
        self.assertEqual((m["previous_main"], m["other"]), (None, None))
        self.post("s-r5", 42, MAIN_TOOLS)                                   # extends the pre-compaction main
        self.assertFalse(self.events("real", sid="s-r5")[-1]["captured"])
        self.assertEqual(self.events("decision", action="main-switchback"), [])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (MAIN_TOOLS, 2))
        self.assertIn(ka_proxy.COMPACTION_OPENER, p["messages"][0]["content"][0]["text"])

    def test_r4_previous_main_pruned(self):
        self.takeover("s-r43")
        self.assertIsNotNone(self.ka.meta["s-r43"]["previous_main"])
        open(os.path.join(self.state, "off"), "w").close()
        self.wait_for(lambda: self.ka.sessions["s-r43"]["status"] == "stopped")
        self.advance(86401)
        self.wait_for(lambda: "s-r43" not in self.ka.meta and "s-r43" not in self.ka.sessions)

    def test_r3_3_mistaken_long_first_capture_switches_to_shorter_main(self):
        """Reviewer round-3 case: first capture is a 30-message subagent with more tools; the real main
        never reaches 30 messages. It still takes over once the capture is 30 min stale."""
        big_sub = MAIN_TOOLS + ["Extra1"]
        self.post("s-r33", 30, big_sub, lineage="sub")                      # mistaken first capture
        self.advance(1500)
        self.post("s-r33", 10, MAIN_TOOLS)                                  # fewer tools: no more-tools path
        for n in (12, 14):
            self.advance(150)
            self.post("s-r33", n, MAIN_TOOLS)
        self.assertEqual(self.events("decision", action="main-switch"), [])   # 1800 s stale but only 2 exts
        self.advance(150)
        self.post("s-r33", 16, MAIN_TOOLS)                                  # 3rd extension in 10 min
        (d,) = self.wait_for(lambda: self.events("decision", action="main-switch"))
        self.assertEqual(d["reason"], "stale-main")
        self.assertEqual([e["captured"] for e in self.events("real")], [True, False, False, False, True])
        self.advance(3300)
        self.wait_for(lambda: self.fake.pings())
        p = self.fake.pings()[0]["json"]
        self.assertEqual(([t["name"] for t in p["tools"]], len(p["messages"])), (MAIN_TOOLS, 16))


class TestReportAndUninstall(Base):
    def test_j_report_and_uninstall_dry_run(self):
        # the timeline below spans ~4.1 h of fake clock and the report is per local day: start it at 06:00
        # local today so it never straddles midnight (it failed when the suite ran after ~20:00)
        now = time.time()
        lt = time.localtime(now)
        self.clock.offset = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 6, 0, 0, 0, 0, -1)) - now
        self.post("s-j", 4, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: len(self.events("ping")) == 1)
        self.advance(3300)
        self.wait_for(lambda: len(self.events("ping")) == 2)
        self.advance(1000)                     # return 4300s after the first ping -> counted
        self.post("s-j", 6, MAIN_TOOLS)
        self.advance(3300)
        self.wait_for(lambda: len(self.events("ping")) == 3)
        self.advance(60)                       # return soon after one ok ping -> also counted (item 7 fix)
        self.post("s-j", 8, MAIN_TOOLS)
        self.fake.ping_status = 401
        self.advance(3300)
        self.wait_for(lambda: self.events("decision", reason="verify-fail"))
        day = self.events("ping")[0]["ts"][:10]
        r = subprocess.run([PY, os.path.join(HERE, "ka_report.py"), "--day", day,
                            "--log", os.path.join(self.state, "ka.log")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pings   4 (ok 3)", r.stdout)
        self.assertIn("potential avoided rewarm: $3.9000 over 2 return(s)", r.stdout)
        self.assertIn("verify-fail x1", r.stdout)
        for s in SENTINELS:
            self.assertNotIn(s, r.stdout)
        # install.py --uninstall --dry-run against a throwaway HOME: prints plan, changes nothing
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".claude"))
        sp = os.path.join(home, ".claude", "settings.json")
        with open(os.path.join(self.state, "proxy.token")) as f:
            tok = f.read().strip()
        orig = json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787", "KEEP": "1",
                                   "HTTPS_PROXY": f"http://ka:{tok}@127.0.0.1:8787",
                                   "NO_PROXY": "localhost,127.0.0.1",
                                   "NODE_EXTRA_CA_CERTS": self.ca_pem,
                                   "CLAUDE_CODE_SHELL_PREFIX": os.path.join(self.state, "bin", "ka-shell-prefix.sh")},
                           "model": "opus"})
        with open(sp, "w") as f:
            f.write(orig)
        uninstall = [PY, os.path.join(PKG, "install.py"), "--uninstall", "--dry-run", "--no-services", "--home", home]
        uenv = dict({k: v for k, v in os.environ.items() if k != "KA_PORT"}, KA_STATE_DIR=self.state)
        os.makedirs(os.path.join(self.state, "bin"))
        r = subprocess.run(uninstall, capture_output=True, text=True, env=uenv)
        self.assertEqual(r.returncode, 0, r.stderr)
        for k in ("ANTHROPIC_BASE_URL", "HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "NO_PROXY", "CLAUDE_CODE_SHELL_PREFIX"):
            self.assertIn(f"would: remove env.{k} ", r.stdout)
        self.assertNotIn("env.KEEP", r.stdout)
        self.assertIn(f"would: remove directory {os.path.join(self.state, 'bin')}\n", r.stdout)
        self.assertIn(f"keep {self.state} ", r.stdout)                      # state kept without --purge
        r = subprocess.run(uninstall + ["--purge"], capture_output=True, text=True, env=uenv)
        self.assertIn(f"would: remove directory {self.state} (", r.stdout)
        with open(sp) as f:
            self.assertEqual(f.read(), orig)
        self.assertEqual(os.listdir(os.path.join(home, ".claude")), ["settings.json"])
        self.assertTrue(os.path.isdir(os.path.join(self.state, "ca")))
        # a user's own corporate proxy / CA are not ours: nothing to remove
        # M7: near-misses of our values (other port, trailing slash, other file in our ca dir, same
        # basename elsewhere) are NOT ours
        other = json.dumps({"env": {"HTTPS_PROXY": "http://127.0.0.1:8787/", "NO_PROXY": "localhost,127.0.0.1",
                                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
                                    "NODE_EXTRA_CA_CERTS": os.path.join(self.state, "ca", "other.pem"),
                                    "CLAUDE_CODE_SHELL_PREFIX": "/elsewhere/ka-shell-prefix.sh"}})
        with open(sp, "w") as f:
            f.write(other)
        r = subprocess.run(uninstall, capture_output=True, text=True, env=uenv)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("would: remove env.", r.stdout)
        self.assertIn("no ka proxy/CA env keys", r.stdout)
        with open(sp) as f:
            self.assertEqual(f.read(), other)
        # H1: the pre-token URL and a wrong token are near-misses too; the token never reaches stdout
        for near in ("http://127.0.0.1:8787", f"http://ka:{'0' * 32}@127.0.0.1:8787"):
            with open(sp, "w") as f:
                json.dump({"env": {"HTTPS_PROXY": near}}, f)
            r = subprocess.run(uninstall, capture_output=True, text=True, env=uenv)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no ka proxy/CA env keys", r.stdout, near)
            self.assertNotIn(tok, r.stdout + r.stderr)


def tls_probe(cert, key, sni, cafile):
    """Serve cert/key once on a free port; handshake as a client trusting ONLY cafile. Raises on failure."""
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert, key)
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)

    def srv():
        conn, _ = ls.accept()
        try:
            sctx.wrap_socket(conn, server_side=True).close()
        except (ssl.SSLError, OSError):
            conn.close()

    t = threading.Thread(target=srv, daemon=True)
    t.start()
    try:
        with socket.create_connection(ls.getsockname(), timeout=5) as s:
            with ssl.create_default_context(cafile=cafile).wrap_socket(s, server_hostname=sni):
                pass
    finally:
        t.join(5)
        ls.close()


def basic_auth(token, user="ka"):
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def raw_connect(port, target, auth=None):
    """Plain-socket CONNECT through the proxy (auth = Proxy-Authorization value); returns (socket, status line)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    pa = b"Proxy-Authorization: %s\r\n" % auth.encode() if auth else b""
    s.sendall(b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s\r\n" % (target.encode(), target.encode(), pa))
    head = b""
    while b"\r\n\r\n" not in head:
        d = s.recv(1)
        if not d:
            break
        head += d
    return s, head.split(b"\r\n", 1)[0].decode()


class TestTunnel(Base):
    def mitm_conn(self, cafile=None):
        ctx = ssl.create_default_context(cafile=cafile or self.ca_pem)   # trusts ONLY our CA
        c = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=10)
        c.set_tunnel("api.anthropic.com", 443, headers={"Proxy-Authorization": self.auth()})
        return c

    def post_mitm(self, c, sid, n_msgs, tools):
        body = json.dumps(make_body(sid, n_msgs, tools)).encode()
        n_real = len(self.events("real"))
        c.request("POST", "/v1/messages?beta=true", body=body, headers=headers_for(sid))
        r = c.getresponse()
        data = r.read()
        self.wait_for(lambda: len(self.events("real")) > n_real)
        return r.status, data, body

    def echo_server(self):
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)

        def srv():
            conn, _ = ls.accept()
            while True:
                d = conn.recv(4096)
                if not d:
                    break
                conn.sendall(d)
            conn.close()
            ls.close()

        threading.Thread(target=srv, daemon=True).start()
        return ls.getsockname()[1]

    def test_n_connect_mitm_captures_with_ca_only_trust(self):
        c = self.mitm_conn()
        for n in (4, 6):   # two requests on one keep-alive TLS tunnel
            status, data, sent = self.post_mitm(c, "s-n", n, MAIN_TOOLS)
            self.assertEqual((status, data), (200, self.fake.last_sse))
            self.assertEqual(self.fake.requests[-1]["raw"], sent)
            self.assertEqual(self.fake.requests[-1]["headers"]["authorization"], f"Bearer {S_AUTH}")
        c.close()
        self.assertEqual([e["captured"] for e in self.events("real")], [True, True])
        self.wait_for(lambda: self.events("tunnel", mode="mitm", ok=True))
        # a client with only the system trust store must reject the leaf
        bad = http.client.HTTPSConnection("127.0.0.1", self.port, context=ssl.create_default_context(), timeout=10)
        bad.set_tunnel("api.anthropic.com", 443, headers={"Proxy-Authorization": self.auth()})
        with self.assertRaises(ssl.SSLCertVerificationError):
            bad.request("GET", "/")
        bad.close()
        self.wait_for(lambda: self.events("tunnel", mode="mitm", ok=False))
        # the captured tunnel request drives the normal keepalive
        self.advance(3300)
        self.wait_for(lambda: self.events("ping", ok=True))
        self.assertEqual(len(self.fake.pings("s-n")[0]["json"]["messages"]), 6)

    def test_o_other_host_blind_tunnel(self):
        port = self.echo_server()
        s, line = raw_connect(self.port, f"127.0.0.1:{port}", self.auth())
        self.assertIn(" 200 ", line)
        payload = f"POST /v1/messages {S_AUTH} {S_BODY} {S_PROMPT}".encode() * 50
        s.sendall(payload)
        got = b""
        while len(got) < len(payload):
            got += s.recv(65536)
        s.shutdown(socket.SHUT_WR)
        while s.recv(65536):
            pass
        s.close()
        self.assertEqual(got, payload)
        (t,) = self.wait_for(lambda: self.events("tunnel", mode="blind"))
        self.assertEqual((t["host"], t["port"], t["bytes_up"], t["bytes_down"], t["ok"]),
                         ("127.0.0.1", port, len(payload), len(payload), True))
        self.assertEqual(sorted(t), sorted(["ts", "t", "event", "mode", "host", "port", "ok", "bytes_up",
                                            "bytes_down", "dur_s"]))
        self.assertEqual(self.events("real"), [])
        self.assertEqual(self.fake.requests, [])

    def test_o2_mitm_host_without_leaf_falls_back_to_blind(self):
        os.remove(os.path.join(self.state, "ca", "leaf.pem"))
        self.ka.cfg["mitm_hosts"] = {"api.anthropic.com", "localhost"}   # localhost:443 = no real network
        s, _ = raw_connect(self.port, "localhost:443", self.auth())
        s.close()
        self.wait_for(lambda: self.events("decision", reason="no-leaf-cert", host="localhost"))
        self.wait_for(lambda: self.events("tunnel", mode="blind", host="localhost"))
        self.assertEqual(self.events("tunnel", mode="mitm"), [])

    def test_p_name_constraints_block_other_hosts(self):
        ca_dir = os.path.join(self.state, "ca")
        text = subprocess.run([OPENSSL, "x509", "-noout", "-text", "-in", self.ca_pem],
                              capture_output=True, text=True).stdout
        if "Name Constraints" not in text:
            self.skipTest("LibreSSL did not emit nameConstraints; cannot prove the CA is host-limited")
        d = os.path.join(self.tmp, "evil")
        os.makedirs(d)
        with open(os.path.join(d, "x.cnf"), "w") as f:
            f.write("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=evil.example.com\n"
                    "[v3]\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n"
                    "subjectAltName=DNS:evil.example.com\n"
                    "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")   # well-formed except its name
        run = lambda *a: subprocess.run([OPENSSL, *a], cwd=d, capture_output=True, check=True)
        run("genrsa", "-out", "evil.key", "2048")
        run("req", "-new", "-key", "evil.key", "-config", "x.cnf", "-out", "evil.csr")
        run("x509", "-req", "-in", "evil.csr", "-CA", self.ca_pem, "-CAkey", os.path.join(ca_dir, "ca.key"),
            "-set_serial", "7", "-days", "30", "-extfile", "x.cnf", "-extensions", "v3", "-out", "evil.pem")
        # control: the real leaf verifies against the same CA
        tls_probe(os.path.join(ca_dir, "leaf.pem"), os.path.join(ca_dir, "leaf.key"), "api.anthropic.com",
                  self.ca_pem)
        with self.assertRaises(ssl.SSLCertVerificationError) as cm:
            tls_probe(os.path.join(d, "evil.pem"), os.path.join(d, "evil.key"), "evil.example.com", self.ca_pem)
        self.assertEqual(cm.exception.verify_code, 47, cm.exception)   # X509_V_ERR_PERMITTED_VIOLATION

    def test_q_chunked_body_in_tunnel_refused(self):
        c = self.mitm_conn()
        c.request("POST", "/v1/messages", body=iter([b'{"model":"x",', b'"messages":[]}']),
                  headers=headers_for("s-q"))   # iterable body -> http.client sends Transfer-Encoding: chunked
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 411)
        c.close()
        (d,) = self.wait_for(lambda: self.events("decision", reason="chunked-request-body"))
        self.assertEqual((d["action"], d["tunnel"], d["method"]), ("reject", True, "POST"))
        self.assertEqual(self.fake.requests, [])
        c = self.mitm_conn()                   # proxy still healthy
        status, data, _ = self.post_mitm(c, "s-q", 4, MAIN_TOOLS)
        c.close()
        self.assertEqual((status, data), (200, self.fake.last_sse))

    def test_r_no_secrets_in_log_with_tunnel_traffic(self):
        c = self.mitm_conn()
        self.post_mitm(c, "s-r", 4, MAIN_TOOLS)
        c.close()
        self.advance(3300)
        self.wait_for(lambda: self.events("ping", ok=True))
        port = self.echo_server()
        s, _ = raw_connect(self.port, f"127.0.0.1:{port}", self.auth())
        s.sendall(f"{S_AUTH} {S_BODY} {S_PROMPT}".encode())
        s.recv(4096)
        s.close()
        self.wait_for(lambda: self.events("tunnel", mode="blind"))
        log = self.logtext()
        for sent in SENTINELS + ("Bearer", "2023-06-01", "/v1/messages"):
            self.assertNotIn(sent, log)

    def test_no_body_statuses_keep_alive_intact(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        for method, path, want in (("GET", "/v1/204", (204, b"")), ("GET", "/v1/ok", (200, b"hello")),
                                   ("HEAD", "/v1/ok", (200, b"")), ("GET", "/v1/ok", (200, b"hello"))):
            c.request(method, path)
            r = c.getresponse()
            self.assertEqual((r.status, r.read()), want, (method, path))
        c.close()

    def test_plain_chunked_body_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/v1/messages", body=iter([b"{}"]), headers=headers_for("s-pc"))
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 411)
        self.wait_for(lambda: self.events("decision", reason="chunked-request-body", tunnel=False))


def raw_request(port, head):
    """Send raw request bytes, half-close, read the whole reply until the proxy closes. Returns (status, reply)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.sendall(head)
        s.shutdown(socket.SHUT_WR)
        out = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    finally:
        s.close()
    return int(out.split(b" ", 2)[1]), out


def dechunk(reply):
    """Body of a raw chunked reply -> (data, saw terminal chunk)."""
    rest, data = reply.split(b"\r\n\r\n", 1)[1], b""
    while rest:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        n = int(size_line, 16)
        if n == 0:
            return data, True
        data, rest = data + rest[:n], rest[n + 2:]
    return data, False


class TestHardening(Base):
    """Relay hardening: Content-Length, stream timeout, hop-by-hop headers, HEAD/304, usage tap, proxy auth."""

    def test_m2_bad_content_length_rejected_plain(self):
        for bad in (b"-1", b"abc", str(40 << 20).encode(), b"+5", b"5, 5"):
            status, reply = raw_request(self.port, b"POST /v1/messages HTTP/1.1\r\nHost: x\r\n"
                                        b"Content-Length: %s\r\n\r\n" % bad)
            self.assertEqual(status, 400, bad)
            self.assertIn(b"Connection: close", reply)
        self.assertEqual(self.fake.requests, [])
        evs = self.events("decision", reason="bad-content-length")
        self.assertEqual(len(evs), 5)
        self.assertEqual({(e["action"], e["tunnel"], e["method"]) for e in evs}, {("reject", False, "POST")})
        self.assertNotIn(str(40 << 20), self.logtext())

    def test_m2_valid_content_length_relayed(self):
        body = b'{"stream": false}'
        status, reply = raw_request(self.port, b"POST /v1/messages/count_tokens HTTP/1.1\r\nHost: x\r\n"
                                    b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.requests[-1]["raw"], body)
        self.assertEqual(self.events("decision", reason="bad-content-length"), [])

    def test_m2_bad_content_length_rejected_in_tunnel(self):
        c = TestTunnel.mitm_conn(self)
        c.putrequest("POST", "/v1/messages")
        c.putheader("Content-Length", "-1")
        c.endheaders()
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 400)
        (d,) = self.wait_for(lambda: self.events("decision", reason="bad-content-length"))
        self.assertEqual(d["tunnel"], True)
        self.assertEqual(self.fake.requests, [])

    def test_m8_read_timeout_config(self):
        self.assertEqual(ka_proxy.load_config({})["upstream_read_timeout"], 300)
        self.assertEqual(ka_proxy.load_config({"KA_UPSTREAM_READ_TIMEOUT_S": "5"})["upstream_read_timeout"], 5)

    def test_m8_stalled_stream_closes_client_cleanly(self):
        self.ka.cfg["upstream_read_timeout"] = 0.5
        t0 = time.monotonic()
        status, reply = raw_request(self.port, b"GET /v1/stall HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertLess(time.monotonic() - t0, 5)                         # closed, not hung
        self.assertEqual(status, 200)
        self.assertIn(b"6\r\nfirst!\r\n", reply)                          # bytes before the stall got through
        self.assertFalse(reply.endswith(b"0\r\n\r\n"))                    # truncation stays visible
        (d,) = self.wait_for(lambda: self.events("decision", reason="stream-timeout"))
        self.assertEqual((d["action"], d["tunnel"], d["method"]), ("abort", False, "GET"))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)   # proxy still serves
        c.request("GET", "/v1/ok")
        self.assertEqual(c.getresponse().read(), b"hello")
        c.close()

    def test_m8_upstream_cut_mid_stream_logged(self):
        status, reply = raw_request(self.port, b"GET /v1/cut HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertFalse(reply.endswith(b"0\r\n\r\n"))
        (d,) = self.wait_for(lambda: self.events("decision", reason="upstream-error"))
        self.assertEqual(d["action"], "abort")

    def test_r3_1_fixed_length_truncation_not_framed_complete(self):
        status, reply = raw_request(self.port, b"GET /v1/short HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(status, 200)
        data, complete = dechunk(reply)
        self.assertEqual((data, complete), (b"s" * 400, False))           # 400 bytes through, no terminal chunk
        (d,) = self.wait_for(lambda: self.events("decision", reason="upstream-truncated"))
        self.assertEqual((d["action"], d["tunnel"], d["method"]), ("abort", False, "GET"))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)   # complete fixed-length body: fine
        c.request("GET", "/v1/ok")
        self.assertEqual(c.getresponse().read(), b"hello")
        c.close()
        self.assertEqual(len(self.events("decision", action="abort")), 1)

    def test_r3_2_client_gone_mid_stream_stops_relay(self):
        self.ka.cfg["upstream_read_timeout"] = 60
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /v1/drip HTTP/1.1\r\nHost: x\r\n\r\n")
        got = b""
        while b"drip!" not in got:
            got += s.recv(4096)
        t0 = time.monotonic()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")   # RST on close
        s.close()
        (d,) = self.wait_for(lambda: self.events("decision", reason="client-gone"), timeout=5)
        self.assertLess(time.monotonic() - t0, 5)                         # far below the 60 s read timeout
        self.assertEqual((d["action"], d["method"]), ("abort", "GET"))
        self.assertTrue(self.fake.drip_broken.wait(5))                     # upstream side closed as well

    def test_l10_connection_nominated_request_headers_dropped(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/v1/ok", headers={"Connection": "X-Internal,  x-other", "X-Internal": "a",
                                            "X-Other": "b", "X-Keep": "c"})
        self.assertEqual(c.getresponse().read(), b"hello")
        c.close()
        got = {k.lower() for k in self.fake.requests[-1]["headers"]}
        self.assertIn("x-keep", got)
        self.assertEqual(got & {"x-internal", "x-other"}, set())

    def test_l10_connection_nominated_response_headers_dropped(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/v1/nominate")
        r = c.getresponse()
        self.assertEqual(r.read(), b"hello")
        c.close()
        got = {k.lower() for k, _ in r.getheaders()}
        self.assertIn("x-keep", got)
        self.assertNotIn("x-secret-hop", got)

    def test_l11_head_and_304_keep_content_length(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        for method, path, want_status, want_cl in (("HEAD", "/v1/ok", 200, "5"), ("GET", "/v1/304", 304, "123"),
                                                   ("GET", "/v1/ok", 200, None)):
            c.request(method, path)
            r = c.getresponse()
            body = r.read()
            self.assertEqual(r.status, want_status, path)
            if want_cl is None:                                            # normal body: chunked, as before
                self.assertEqual(body, b"hello")
                self.assertIsNone(r.getheader("Content-Length"))
            else:
                self.assertEqual((r.getheader("Content-Length"), body), (want_cl, b""), path)
        c.close()

    def test_h1_token_file(self):
        p = os.path.join(self.state, "proxy.token")
        self.assertTrue(mode_ok(p, 0o600))
        tok = self.ka.proxy_token
        self.assertRegex(tok, r"^[0-9a-f]{32}$")
        self.assertEqual(ka_proxy.load_or_create_token(self.state), tok)          # stable across restarts

    def test_h1_connect_requires_proxy_auth(self):
        tok = self.ka.proxy_token
        port = TestTunnel.echo_server(self)
        target = f"127.0.0.1:{port}".encode()
        bad = [None, basic_auth("0" * 32), basic_auth(tok, user="other"), basic_auth(tok[:-1]),
               "Bearer " + tok, "Basic !!notbase64", basic_auth(tok) + "\r\nProxy-Authorization: " + basic_auth(tok)]
        for auth in bad:
            pa = b"Proxy-Authorization: %s\r\n" % auth.encode() if auth else b""
            status, reply = raw_request(self.port, b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s\r\n" % (target, target, pa))
            self.assertEqual(status, 407, auth)
            self.assertIn(b'Proxy-Authenticate: Basic realm="ka"\r\n', reply)
            self.assertTrue(reply.endswith(b"\r\n\r\n"), auth)                      # closed, nothing tunnelled
        evs = self.events("decision", reason="proxy-auth-fail")
        self.assertEqual([e["had_header"] for e in evs], [False] + [True] * (len(bad) - 1))
        self.assertEqual(self.events("tunnel"), [])
        s, line = raw_connect(self.port, target.decode(), self.auth())               # right credentials
        self.assertIn(" 200 ", line)
        s.sendall(b"ping")
        self.assertEqual(s.recv(16), b"ping")
        s.close()
        self.wait_for(lambda: self.events("tunnel", mode="blind", ok=True))
        c = TestTunnel.mitm_conn(self)                                             # MITM door, right credentials
        c.request("GET", "/v1/ok")
        self.assertEqual(c.getresponse().read(), b"hello")
        c.close()
        st = os.path.join(self.state, "status.json")
        self.wait_for(lambda: os.path.exists(st))
        with open(st) as f:
            status_text = f.read()
        for where in (self.logtext(), status_text):
            self.assertNotIn(tok, where)
            self.assertNotIn(self.auth().split()[1], where)

    def test_r3_6_token_rotation_without_restart(self):
        closed = f"127.0.0.1:{free_port()}".encode()                       # auth ok -> 502 (refused), else 407

        def connect(tok):
            return raw_request(self.port, b"CONNECT %s HTTP/1.1\r\nHost: x\r\nProxy-Authorization: %s\r\n\r\n"
                               % (closed, basic_auth(tok).encode()))[0]

        old = self.ka.proxy_token
        self.assertEqual(connect(old), 502)
        os.remove(os.path.join(self.state, "proxy.token"))                 # reviewer's rotation: delete + ka_ca.py
        r = subprocess.run([PY, os.path.join(HERE, "ka_ca.py")], capture_output=True, text=True,
                           env=dict(os.environ, KA_STATE_DIR=self.state, KA_PORT=str(self.port)))
        self.assertEqual(r.returncode, 0, r.stderr)
        new = re.search(r"http://ka:([0-9a-f]{32})@127\.0\.0\.1:", r.stdout).group(1)
        self.assertNotEqual(new, old)
        self.assertEqual((connect(old), connect(new)), (407, 502))           # running proxy picked it up
        with open(os.path.join(self.state, "proxy.token"), "w") as f:       # malformed -> keep the last good
            f.write("garbage\n")
        self.assertEqual((connect(new), connect(new)), (502, 502))
        self.assertEqual(len(self.events("decision", reason="token-reload-error")), 1)   # once per change
        log = self.logtext()
        for t in (old, new):
            self.assertNotIn(t, log)

    def test_h1_plain_door_serves_v1_only(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/v1/ok")
        self.assertEqual(c.getresponse().read(), b"hello")
        c.close()
        n = len(self.fake.requests)
        for method, path, body in (("GET", "/ok", None), ("HEAD", "/", None), ("GET", "/v1", None),
                                   ("POST", "/api/x", b'{"a": 1}'), ("GET", "http://evil.example/v1/x", None)):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request(method, path, body=body)
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, 404, (method, path))
        self.assertEqual(len(self.fake.requests), n)                               # none relayed
        self.assertEqual(len(self.events("decision", reason="plain-path-not-api")), 5)
        self.assertNotIn("/api/x", self.logtext())


def sse_lines(*evs, eol=b"\n"):
    return b"".join(b"event: %s%sdata: %s%s%s" % (e["type"].encode(), eol, json.dumps(e).encode(), eol, eol)
                    for e in evs)


START = {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 1,
         "cache_read_input_tokens": 240000, "cache_creation_input_tokens": 10000,
         "cache_creation": {"ephemeral_1h_input_tokens": 10000}}}}
DELTA = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}


class TestUsageTap(unittest.TestCase):
    """M3: bounded usage tap gives the same usage as parsing the whole stream."""

    def feed(self, data, size):
        tap = ka_proxy.UsageTap(True)
        for i in range(0, len(data), size):
            tap.feed(data[i:i + size])
        return tap

    def test_m3_split_usage_events_parse_identically(self):
        text = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": S_OUT}}
        for eol in (b"\n", b"\r\n"):
            sse = sse_lines(START, text, DELTA, {"type": "message_stop"}, eol=eol)
            want = ka_proxy.sse_usage(sse)
            self.assertEqual(want["output_tokens"], 7)
            for size in (1, 2, 7, 97, 4096):                                   # events split across chunks
                tap = self.feed(sse, size)
                self.assertEqual(ka_proxy.sse_usage(tap.result()), want, (eol, size))
            tap = self.feed(sse.rstrip(), 50)                                  # no trailing newline
            self.assertEqual(ka_proxy.sse_usage(tap.result()), want)

    def test_m3_50mib_stream_bounded(self):
        filler = sse_lines(*[{"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": "x" * 150 + ' "message_delta" '}}] * 3000)
        huge = sse_lines({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "y" * (200 << 10)}})   # > 64 KiB line
        decoy = b'data: {"type": "content_block_delta", "note": "message_start"}\n'  # substring, wrong type
        block = filler + huge + decoy * 2000
        tap = ka_proxy.UsageTap(True)
        tap.feed(sse_lines(START)[:40])                                       # message_start split too
        tap.feed(sse_lines(START)[40:])
        total = 0
        while total < 50 << 20:
            for i in range(0, len(block), 65536):
                tap.feed(block[i:i + 65536])
            total += len(block)
        tap.feed(sse_lines(DELTA, {"type": "message_stop"}))
        self.assertLessEqual(tap.peak, 256 << 10)
        self.assertLessEqual(tap.retained, 256 << 10)
        self.assertEqual(len(tap.kept), 2)                                     # start + delta; no filler/decoys
        got = ka_proxy.sse_usage(tap.result())
        self.assertEqual(got, ka_proxy.sse_usage(sse_lines(START, DELTA)))
        self.assertEqual((got["output_tokens"], got["cache_read_input_tokens"]), (7, 240000))

    def test_r3_8_malformed_sse_json_never_raises(self):
        bad = (b'data: ["message_start"]\n'                                    # valid JSON, not an object
               b'data: {"type": "message_delta", "usage": \n'                  # truncated JSON
               b'data: ' + b'[' * 30000 + b'"message_delta"' + b']' * 30000 + b'\n')   # deep list, < 64 KiB
        for size in (7, 4096, 65536):
            tap = ka_proxy.UsageTap(True)
            data = sse_lines(START)[:30] + sse_lines(START)[30:] + bad + sse_lines(DELTA)
            for i in range(0, len(data), size):
                tap.feed(data[i:i + size])                                    # must not raise
            self.assertEqual(tap.malformed, 3, size)
            self.assertEqual(ka_proxy.sse_usage(tap.result()), ka_proxy.sse_usage(sse_lines(START, DELTA)))
        # RecursionError needs ~1M nesting (> PARTIAL_MAX, so feed() drops such a line unparsed); the
        # parser-level catch is still exercised directly
        tap = ka_proxy.UsageTap(True)
        tap._line(b'data: ' + b'[' * 1000000 + b'"message_delta"' + b']' * 1000000)
        self.assertEqual(tap.malformed, 1)
        tap._line = lambda line: 1 / 0                                         # any scanner fault
        tap.feed(b"data: x\n")
        self.assertEqual(tap.malformed, 2)
        del tap._line                                                           # fault gone
        tap.feed(b"\n" + sse_lines(DELTA))                                      # recovers at the next line
        self.assertEqual(tap.malformed, 2)
        self.assertEqual(ka_proxy.sse_usage(tap.result()), {"output_tokens": 7})

    def test_m3_non_stream_body_kept_up_to_cap(self):
        tap = ka_proxy.UsageTap(False)
        body = json.dumps({"usage": {"input_tokens": 3}}).encode()
        tap.feed(body[:5])
        tap.feed(body[5:])
        self.assertEqual(tap.result(), body)
        big = ka_proxy.UsageTap(False)
        big.feed(b" " * ka_proxy.MAX_BODY)
        big.feed(b"x")
        self.assertEqual((big.result(), big.overflow, big.retained), (b"", True, 0))


class TestScripts(unittest.TestCase):
    @unittest.skipUnless(BASH, "bash script test (POSIX; on Windows the prefix runs under Git Bash, untested here)")
    def test_shell_prefix_strips_only_our_proxy_env(self):
        sp = os.path.join(HERE, "ka-shell-prefix.sh")
        show = ('echo "P=${HTTPS_PROXY-unset} p=${https_proxy-unset} H=${HTTP_PROXY-unset} '
                'C=${NODE_EXTRA_CA_CERTS-unset} N=${NO_PROXY-unset} n=${no_proxy-unset}"')
        cmd = show + "; echo 'a b' | tr a-z A-Z; cat; echo err >&2; exit 7"
        tmp = tempfile.mkdtemp(prefix="ka-sp-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "ca"))
        our_ca = os.path.join(tmp, "ca", "ca.pem")
        base = {k: v for k, v in os.environ.items() if k != "KA_PORT"}
        base["KA_STATE_DIR"] = tmp
        # H1: no token file yet -> no proxy URL is ours; the unauthenticated form is left alone, stderr quiet
        r = subprocess.run([BASH, sp, show], env=dict(base, HTTPS_PROXY="http://127.0.0.1:8787"),
                           capture_output=True, text=True)
        self.assertEqual((r.stdout.split()[0], r.stderr), ("P=http://127.0.0.1:8787", ""))
        tok = ka_proxy.load_or_create_token(tmp)
        url = f"http://ka:{tok}@127.0.0.1:8787"
        ours = dict(base, HTTPS_PROXY=url, https_proxy=url,
                    HTTP_PROXY=url, NODE_EXTRA_CA_CERTS=our_ca,
                    NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
        r = subprocess.run([BASH, sp, cmd], input="stdin-data\n", env=ours, capture_output=True, text=True)
        self.assertEqual(r.returncode, 7)
        self.assertEqual(r.stdout, "P=unset p=unset H=unset C=unset N=unset n=unset\nA B\nstdin-data\n")
        self.assertEqual(r.stderr, "err\n")
        # M6: each variable is judged on its own; only exact matches of our values are removed
        mixed = dict(base, HTTPS_PROXY=url, https_proxy="http://proxy.corp:3128",
                     HTTP_PROXY="http://127.0.0.1:8787", NODE_EXTRA_CA_CERTS="/corp.pem",
                     NO_PROXY="localhost,127.0.0.1,.corp", no_proxy="localhost,127.0.0.1")
        r = subprocess.run([BASH, sp, show], env=mixed, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "P=unset p=http://proxy.corp:3128 H=http://127.0.0.1:8787 C=/corp.pem "
                                   "N=localhost,127.0.0.1,.corp n=unset\n")
        other_port = dict(ours, KA_PORT="9999", HTTPS_PROXY=f"http://ka:{tok}@127.0.0.1:9999")
        r = subprocess.run([BASH, sp, show], env=other_port, capture_output=True, text=True)
        self.assertTrue(r.stdout.startswith(f"P=unset p={url} "), r.stdout)

    def test_ca_script(self):
        tmp = tempfile.mkdtemp(prefix="ka-cas-")
        try:
            state = os.path.join(tmp, "st")
            env = dict({k: v for k, v in os.environ.items() if k != "https_proxy"}, KA_STATE_DIR=state,
                       KA_PORT="8799")
            r = subprocess.run([PY, os.path.join(HERE, "ka_ca.py")], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            ca_dir = os.path.realpath(os.path.join(state, "ca"))
            tok_path = os.path.join(state, "proxy.token")
            self.assertTrue(mode_ok(tok_path, 0o600))
            tok = ka_proxy.load_or_create_token(state)                         # proxy reads the same token
            self.assertRegex(tok, r"^[0-9a-f]{32}$")
            for line in (f'"HTTPS_PROXY": "http://ka:{tok}@127.0.0.1:8799"',
                         f'"NODE_EXTRA_CA_CERTS": "{ka_ca.env_path(os.path.join(ca_dir, "ca.pem"))}"',
                         '"NO_PROXY": "localhost,127.0.0.1"',
                         f'"CLAUDE_CODE_SHELL_PREFIX": '
                         f'"{ka_ca.env_path(os.path.join(os.path.realpath(HERE), "ka-shell-prefix.sh"))}"'):
                self.assertIn(line, r.stdout)
            self.assertTrue(mode_ok(ca_dir, 0o700))
            for fn in ("ca.key", "leaf.key", "ca.pem", "leaf.pem"):
                self.assertTrue(mode_ok(os.path.join(ca_dir, fn), 0o600), fn)
            txt = lambda fn: subprocess.run([OPENSSL, "x509", "-noout", "-text", "-in", os.path.join(ca_dir, fn)],
                                            capture_output=True, text=True).stdout
            ca, leaf = txt("ca.pem"), txt("leaf.pem")
            for s in ("X509v3 Name Constraints: critical", "DNS:api.anthropic.com", "CA:TRUE, pathlen:0"):
                self.assertIn(s, ca)
            for s in ("Subject: CN=api.anthropic.com", "DNS:api.anthropic.com", "TLS Web Server Authentication",
                      "CA:FALSE"):
                self.assertIn(s, leaf)
            with open(os.path.join(ca_dir, "leaf.pem"), "rb") as f:
                first = f.read()
            r = subprocess.run([PY, os.path.join(HERE, "ka_ca.py")], env=env, capture_output=True, text=True)
            self.assertIn("already present", r.stdout)
            self.assertIn(f"http://ka:{tok}@127.0.0.1:8799", r.stdout)            # token reused, not rotated
            self.assertNotIn("lowercase https_proxy", r.stderr)
            if POSIX:   # Windows environment names are case-insensitive: no separate lowercase variable
                secret = "http://user:SECRETPW@corp:3128"                          # lowercase set -> warn only
                r = subprocess.run([PY, os.path.join(HERE, "ka_ca.py")], env=dict(env, https_proxy=secret),
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("warning: lowercase https_proxy is set", r.stderr)
                self.assertNotIn("SECRETPW", r.stdout + r.stderr)
            with open(os.path.join(ca_dir, "leaf.pem"), "rb") as f:
                self.assertEqual(f.read(), first)
            r = subprocess.run([PY, os.path.join(HERE, "ka_ca.py"), "--force"], env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(ca_dir, "leaf.pem"), "rb") as f:
                self.assertNotEqual(f.read(), first)
            self.assertEqual([n for n in os.listdir(ca_dir) if n.startswith(".gen")], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSubprocessMain(unittest.TestCase):
    """End-to-end through main(): env config, real scheduler thread, tiny real-time timings."""

    def test_env_config_and_real_time_ping(self):
        tmp = tempfile.mkdtemp(prefix="ka-sub-")
        fake = FakeUpstream()
        port = free_port()
        self.assertNotEqual(port, 8787)
        env = dict(os.environ, KA_UPSTREAM=f"http://127.0.0.1:{fake.port}", KA_PORT=str(port), KA_STATE_DIR=tmp,
                   KA_PING_AFTER_S="2", KA_TTL_S="6", KA_EXPIRE_MARGIN_S="1", KA_TICK_S="0.2", KA_CAP_H_OPUS="0.01")
        proc = subprocess.Popen([PY, os.path.join(HERE, "ka_proxy.py")], env=env)
        try:
            end = time.time() + 5
            while time.time() < end:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
            body = json.dumps(make_body("s-sub", 4, MAIN_TOOLS)).encode()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/v1/messages", body=body, headers=headers_for("s-sub"))
            self.assertEqual(c.getresponse().read(), fake.last_sse)
            c.close()
            end = time.time() + 6
            while time.time() < end and not fake.pings():
                time.sleep(0.1)
            self.assertEqual(len(fake.pings()), 1)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/_ka/replay")
            r = c.getresponse()
            r.read()
            # M5: no local replay endpoint; H1: a non-/v1/ path on the plain door is 404, never relayed
            self.assertEqual(r.status, 404)
            self.assertNotEqual(fake.requests[-1]["path"], "/_ka/replay")
            c.close()
            with open(os.path.join(tmp, "ka.log")) as f:
                log = f.read()
            evs = [json.loads(l)["event"] for l in log.splitlines()]
            self.assertEqual(evs[:3], ["start", "real", "ping"])
            self.assertTrue(mode_ok(os.path.join(tmp, "ka.log"), 0o600))
            for s in SENTINELS:
                self.assertNotIn(s, log)
        finally:
            proc.terminate()
            proc.wait(5)
            fake.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
