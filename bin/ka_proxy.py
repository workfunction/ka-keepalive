#!/usr/bin/env python3
"""ka_proxy v1: pass-through proxy Claude Code -> Anthropic API + prompt-cache keepalive controller.

Keeps each session's last main-loop POST /v1/messages request IN MEMORY only. Every tracked
session (up to KA_MAX_SESSIONS, most recently active first) that has been idle for KA_PING_AFTER_S
gets that request replayed with max_tokens=0, stream=false (no output, no transcript turn) so its
1h prompt cache stays warm; at most one ping per tick, most urgent first. Each ping is verified
(HTTP 200 + cache read ~= expected prefix); a transient failure gets one scheduled retry, anything
else stops that session. Manual control: kactl (status/stop/resume/off/on) via STATE_DIR files.

Two front doors, one relay path:
  - plain HTTP (CLI: ANTHROPIC_BASE_URL=http://127.0.0.1:<port>); /v1/ paths only, others 404
  - HTTPS_PROXY (desktop): every CONNECT needs Proxy-Authorization Basic ka:<STATE_DIR/proxy.token>
    (else 407). CONNECT api.anthropic.com:443 (KA_MITM_HOSTS) is TLS-terminated with the
    leaf from STATE_DIR/ca (made by ka_ca.py; CA trusted only via NODE_EXTRA_CA_CERTS) and served
    through the same relay; every other CONNECT target is a blind TCP tunnel, never decrypted.

Log (STATE_DIR/ka.log, JSONL) = METADATA ONLY: never bodies, prompts, outputs, header values
or tokens. Binds 127.0.0.1 only. Stdlib only; Python 3.10+ (macOS, Linux, Windows).
"""
import base64, collections, email.utils, hashlib, hmac, http.client, http.server, itertools, json, os, re, secrets, \
    selectors, signal, socket, ssl, threading, time, urllib.parse

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
MAX_BODY = 32 << 20   # largest request body the proxy reads (bytes)
# $/MTok list prices; model id matched by prefix after stripping "claude-", longest key first.
PRICES = {"fable": {"write1h": 20, "read": 0.25, "input": 10},
          "opus-5-5": {"write1h": 8, "read": 0.2, "input": 4},
          "opus-5": {"write1h": 10, "read": 0.5, "input": 5}}
COMPACTION_OPENER = "This session is being continued from a previous conversation"
# Fraction of idle stretches (idle >= 1h) that returned within h hours, h = 1..48. Source: return_cdf.json
# (n=471, idle_events.json 2026-08-19..09-22); embedded here, never read at runtime.
RETURN_CDF = [0.0, 0.1783, 0.2314, 0.2696, 0.293, 0.3057, 0.3227, 0.3355, 0.3482, 0.3524, 0.3588, 0.3652,
              0.3673, 0.3758, 0.38, 0.3822, 0.3928, 0.4013, 0.4119, 0.4204, 0.4331, 0.4395, 0.4416, 0.448,
              0.4522, 0.4544, 0.4544, 0.4565, 0.4565, 0.4671, 0.4671, 0.4671, 0.4671, 0.4692, 0.4713, 0.4713,
              0.4713, 0.4713, 0.4756, 0.4777, 0.4798, 0.482, 0.482, 0.482, 0.4841, 0.4841, 0.4862, 0.4904]
PING_UNCACHED_INPUT = 3845   # typical uncached tail tokens per keepalive ping
_SID_OK = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")   # canonical UUID only
_STOP_NAME_OK = re.compile(r"[A-Za-z0-9._-]{1,128}")
O_BINARY = getattr(os, "O_BINARY", 0)   # Windows: no CRLF translation on os.write


def replace_retry(src, dst, tries=20, delay=0.05):
    """os.replace; on Windows a reader holding dst open (kadash, kactl) makes it fail with
    PermissionError for a moment, so retry briefly before giving up. POSIX: first try succeeds."""
    for i in range(tries):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if os.name != "nt" or i == tries - 1:
                raise
            time.sleep(delay)


def set_mode(fd_or_path, mode):
    """Best-effort POSIX permission bits (no-op where unsupported, e.g. os.fchmod on Windows)."""
    try:
        if isinstance(fd_or_path, int):
            if hasattr(os, "fchmod"):
                os.fchmod(fd_or_path, mode)
        else:
            os.chmod(fd_or_path, mode)
    except OSError:
        if os.name != "nt":
            raise


class LoopbackServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer; on Windows exclusive bind (SO_REUSEADDR there would let a second
    process share the port silently)."""
    if os.name == "nt":
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def log_sid(sid):
    """Session id as it may appear in logs/status: as-is only if a canonical lowercase UUID, else h-<sha256[:12]>."""
    if sid is None:
        return None
    s = str(sid)
    return s if _SID_OK.fullmatch(s) else "h-" + hashlib.sha256(s.encode()).hexdigest()[:12]


def load_config(env=None):
    env = os.environ if env is None else env
    f = lambda k, d: float(env.get(k, d))
    return {"upstream": env.get("KA_UPSTREAM", "https://api.anthropic.com"),
            "port": int(env.get("KA_PORT", "8787")),
            "state_dir": os.path.expanduser(env.get("KA_STATE_DIR", "~/.claude/ka")),
            "ping_after": f("KA_PING_AFTER_S", 3300), "ttl": f("KA_TTL_S", 3600),
            "margin": f("KA_EXPIRE_MARGIN_S", 60), "tick": f("KA_TICK_S", 30),
            "cap_fable": f("KA_CAP_H_FABLE", 12), "cap_opus": f("KA_CAP_H_OPUS", 12),   # hard ceilings
            "cap_default": f("KA_CAP_H_DEFAULT", 4),                                  # unknown price
            "inactive_retain_h": f("KA_INACTIVE_RETAIN_H", 2),   # non-active track dropped this long after
            "extend_max_h": f("KA_EXTEND_MAX_H", 24),   # extend/<sid> deadline <= its write time + this
            "shared_prefix": f("KA_SHARED_PREFIX", 30000), "min_read_frac": f("KA_MIN_READ_FRAC", 0.95),
            "mitm_hosts": {h.strip().lower() for h in env.get("KA_MITM_HOSTS", "api.anthropic.com").split(",")
                           if h.strip()},
            "tunnel_connect_timeout": f("KA_TUNNEL_CONNECT_TIMEOUT_S", 10),
            # relay: max upstream silence per read; 300 s = Claude Code's own event-level stream watchdog
            "upstream_read_timeout": f("KA_UPSTREAM_READ_TIMEOUT_S", 300),
            "retry_default": f("KA_RETRY_DEFAULT_S", 5),
            "max_sessions": int(env.get("KA_MAX_SESSIONS", "8")),
            "ping_concurrency": min(3, max(1, int(env.get("KA_PING_CONCURRENCY", "3"))))}   # 529/overloaded retry wait when no retry-after


def price_for(model):
    if not model:
        return None
    m = model.lower()
    m = m[7:] if m.startswith("claude-") else m
    for k in sorted(PRICES, key=len, reverse=True):
        if m.startswith(k):
            return PRICES[k]
    return None


def est_cost(model, u):
    p = price_for(model)
    if p is None:
        return None
    return round((u["create"] * p["write1h"] + u["read"] * p["read"] + u["in"] * p["input"]) / 1e6, 6)


def est_ping_usd(model, prefix):
    p = price_for(model)
    return None if p is None else round((prefix * p["read"] + PING_UNCACHED_INPUT * p["input"]) / 1e6, 6)


def est_rewarm_usd(model, prefix, shared):
    p = price_for(model)
    return None if p is None else round(max(prefix - shared, 0) * (p["write1h"] - p["read"]) / 1e6, 6)


def dynamic_cap_hours(cfg, model, prefix):
    """argmax over H in 1..12 of expected net $ per idle stretch; 0 if never positive (don't keep).

    EV(H) = sum_{h<=H} P(return in hour h) * (save - (h-1)*ping) - P(no return by H) * H * ping
    with save = est_rewarm_usd, ping = est_ping_usd, one ping per elapsed idle hour.
    KA_CAP_H_FABLE / KA_CAP_H_OPUS are hard ceilings; unknown price -> KA_CAP_H_DEFAULT.
    """
    if price_for(model) is None:
        return cfg["cap_default"]
    save = est_rewarm_usd(model, prefix, cfg["shared_prefix"])
    ping = est_ping_usd(model, prefix)
    # Hourly-bin approximation (a return in hour h is charged h-1 pings although pings run every 55 min):
    # accepted: the hourly CDF cannot resolve sub-hour timing anyway.
    best_h, best_ev = 0, 0.0
    for H in range(1, 13):
        ev, prev = 0.0, 0.0
        for h in range(1, H + 1):
            c = RETURN_CDF[h - 1]
            ev += (c - prev) * (save - (h - 1) * ping)
            prev = c
        ev -= (1 - prev) * H * ping
        if ev > best_ev + 1e-12:
            best_h, best_ev = H, ev
    m = (model or "").lower()
    ceiling = cfg["cap_fable"] if "fable" in m else cfg["cap_opus"] if "opus" in m else 12
    return min(best_h, ceiling)


def usage_fields(u):
    cc = u.get("cache_creation") or {}
    return {"read": u.get("cache_read_input_tokens") or 0, "create": u.get("cache_creation_input_tokens") or 0,
            "c5m": cc.get("ephemeral_5m_input_tokens") or 0, "c1h": cc.get("ephemeral_1h_input_tokens") or 0,
            "in": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0}


def sse_usage(raw):
    """Pull usage from message_start / message_delta events of an SSE stream."""
    u = {}
    for line in raw.split(b"\n"):
        if not line.startswith(b"data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            u.update(ev["message"].get("usage") or {})
        elif ev.get("type") == "message_delta" and ev.get("usage"):
            u.update({k: v for k, v in ev["usage"].items() if v is not None})
    return u


_TOKEN_OK = re.compile(r"[0-9a-f]{32}")


def load_or_create_token(state_dir):
    """STATE_DIR/proxy.token: 32 random hex chars, file 0600, created once (O_EXCL, shared with ka_ca.py).
    Clients of the CONNECT front door authenticate as user "ka" with this token as the password."""
    path = os.path.join(state_dir, "proxy.token")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:   # LF only: ka-shell-prefix.sh reads it with `read -r`
            f.write((secrets.token_hex(16) + "\n").encode())
    set_mode(path, 0o600)
    with open(path) as f:
        tok = f.read().strip()
    if not _TOKEN_OK.fullmatch(tok):
        raise ValueError(f"{path}: not 32 lowercase hex chars (delete it to regenerate)")
    return tok


def proxy_auth_ok(values, token):
    """True iff exactly one Proxy-Authorization header carries Basic base64("ka:" + token)."""
    if not values or len(values) != 1:
        return False
    scheme, _, cred = values[0].strip().partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        got = base64.b64decode(cred.strip(), validate=True)
    except ValueError:
        return False
    return hmac.compare_digest(got, b"ka:" + token.encode())


def strip_hop(pairs):
    """Header (name, value) pairs minus hop-by-hop ones: the fixed HOP set plus every name the
    Connection header nominates (comma-separated, case-insensitive)."""
    drop = set(HOP)
    for k, v in pairs:
        if k.lower() == "connection":
            drop |= {t.strip().lower() for t in v.split(",") if t.strip()}
    return [(k, v) for k, v in pairs if k.lower() not in drop]


def content_length(values):
    """Request body length from the Content-Length value(s): 0 if absent; None (reject) unless every value
    is the same plain non-negative decimal integer <= MAX_BODY."""
    if not values:
        return 0
    vals = {v.strip() for v in values}
    if len(vals) != 1:
        return None
    v = vals.pop()
    if not (v.isascii() and v.isdigit()) or len(v) > 9 or int(v) > MAX_BODY:
        return None
    return int(v)


class UsageTap:
    """Bounded copy of a relayed response, just enough for usage extraction (on_real).

    Stream: incremental line scanner that keeps only complete `data:` lines whose event type is
    message_start / message_delta (the lines sse_usage reads), carries at most one partial line across
    chunks (a line longer than PARTIAL_MAX is dropped) and never retains more than KEEP_MAX bytes in total.
    Non-stream: the JSON body, dropped entirely if it grows past MAX_BODY."""
    PARTIAL_MAX, KEEP_MAX = 64 << 10, 256 << 10
    _WANT = ("message_start", "message_delta")

    def __init__(self, stream):
        self.stream = stream
        self.partial, self.skipping = b"", False   # skipping: inside an over-long line, until next newline
        self.kept, self.kept_n = [], 0
        self.body, self.overflow = bytearray(), False
        self.peak = 0
        self.malformed = 0   # candidate lines skipped as bad JSON (or a scanner fault); never raised

    @property
    def retained(self):
        return len(self.partial) + self.kept_n + len(self.body)

    def feed(self, chunk):
        """Never raises: the relay loop calls this between upstream read and client write."""
        if self.stream:
            try:
                self._scan(chunk)
            except Exception:   # defensive: skip the line being assembled, keep relaying
                self.malformed += 1
                self.partial, self.skipping = b"", True
        elif not self.overflow:
            if len(self.body) + len(chunk) > MAX_BODY:
                self.body, self.overflow = bytearray(), True
            else:
                self.body += chunk
        self.peak = max(self.peak, self.retained)

    def _scan(self, chunk):
        start = 0
        while (nl := chunk.find(b"\n", start)) >= 0:
            if not self.skipping and len(self.partial) + nl - start <= self.PARTIAL_MAX:
                self._line(self.partial + chunk[start:nl])
            self.partial, self.skipping, start = b"", False, nl + 1
        if not self.skipping:
            if len(self.partial) + len(chunk) - start > self.PARTIAL_MAX:
                self.partial, self.skipping = b"", True
            else:
                self.partial += chunk[start:]

    def _line(self, line):
        if not line.startswith(b"data: ") or not any(b'"%s"' % w.encode() in line for w in self._WANT):
            return
        try:
            ev = json.loads(line[6:])
        except (ValueError, RecursionError):   # RecursionError: deeply nested JSON is not a ValueError
            self.malformed += 1
            return
        if not isinstance(ev, dict):
            self.malformed += 1
            return
        room = self.KEEP_MAX - self.PARTIAL_MAX - self.kept_n   # kept lines + one partial <= KEEP_MAX
        if ev.get("type") in self._WANT and len(line) <= room:
            self.kept.append(line)
            self.kept_n += len(line)

    def result(self):
        """Bytes for on_real: the kept SSE lines (sse_usage input) or the JSON body."""
        if not self.stream:
            return bytes(self.body)
        if self.partial and not self.skipping:   # last line without a trailing newline
            self._line(self.partial)
            self.partial = b""
        return b"\n".join(self.kept)


def session_id(headers, body):
    for k in ("x-claude-code-session-id", "x-session-id"):
        if headers.get(k):
            return headers[k]
    uid = (body.get("metadata") or {}).get("user_id", "")
    return uid.rsplit("session_", 1)[-1] if "session_" in uid else (uid or "unknown")


_NUMERIC_OR_TS = re.compile(r"[0-9][0-9:.+\-TZ]{0,39}")                      # 42, 1.5, 2026-09-23T17:00:00Z
_HTTP_DATE = re.compile(r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT")


def safe_header_value(v):
    """Rate-limit header values are numbers, timestamps or booleans; anything else is dropped (None)."""
    v = (v or "").strip()
    if v.lower() in ("true", "false"):
        return v.lower()
    return v if (_NUMERIC_OR_TS.fullmatch(v) or _HTTP_DATE.fullmatch(v)) else None


def retry_after_seconds(v):
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _strip_cache_control(x):
    if isinstance(x, dict):
        return {k: _strip_cache_control(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [_strip_cache_control(v) for v in x]
    return x


def message_hashes(messages):
    """Per-message sha256 of canonical JSON, cache_control removed (Claude Code moves the cache
    breakpoint every turn, so the same message otherwise hashes differently across requests)."""
    return [hashlib.sha256(json.dumps(_strip_cache_control(m), sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False).encode()).hexdigest() for m in messages]


def is_compaction(body):
    """True if the first user message opens with Claude Code's compaction summary. Local only, never logged."""
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = next((b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"), "")
        return isinstance(c, str) and c.lstrip().startswith(COMPACTION_OPENER)
    return False


def is_main(rec, prev):
    """Is this request the session's main-loop request? prev = stored main meta {hashes, n_tools} or None.

    Only tools-bearing requests qualify. The first one seen is main. A later one is main iff its
    messages START WITH the stored main's messages (per-message hash prefix: the main loop only
    appends, and a helper that extends it carries the whole main prefix, so capturing it still warms
    the main cache; this survives tools being added mid-session), or it carries the compaction
    marker. Recovery for a proxy started mid-session whose first capture was a subagent: a request
    with strictly more tools than the stored one also qualifies (subagents get a subset of tools).
    """
    # TODO: compaction lowers n_msgs — revisit after A5b (marker rule added 09-23; confirm on a real /compact)
    if not rec["n_tools"]:
        return False
    if prev is None or rec["compaction"] or extends(rec["hashes"], prev["hashes"]):
        return True
    # more-tools recovery only while the stored main is unconfirmed (never extended itself); after that,
    # a wrong first capture is corrected by the stale-lineage switch in Proxy.on_real instead
    return not prev.get("confirmed") and rec["n_tools"] > prev["n_tools"]


def extends(hashes, base):
    """hashes starts with base (equal counts as extending)."""
    return hashes[:len(base)] == base


def pump(cli, up):
    """Blind bidirectional copy until both directions hit EOF. Returns (bytes_up, bytes_down)."""
    sel = selectors.DefaultSelector()
    sel.register(cli, selectors.EVENT_READ, up)
    sel.register(up, selectors.EVENT_READ, cli)
    n = {cli: 0, up: 0}
    live = 2
    try:
        while live:
            for key, _ in sel.select():
                src, dst = key.fileobj, key.data
                try:
                    data = src.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    sel.unregister(src)
                    live -= 1
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    dst.sendall(data)
                except OSError:
                    return n[cli], n[up]
                n[src] += len(data)
    finally:
        sel.close()
    return n[cli], n[up]


class Today:
    """Running totals for the local day, fed every logged event (same rules as ka_report.py):
    pings, ping_usd, avoided_rewarm_usd (a captured real request after >=1 ok ping in its idle
    stretch counts that stretch's last ok avoided_rewarm_usd_if_returned), stop reasons."""

    def __init__(self):
        self.day, self.stretch = None, {}
        self._reset(None)

    def _reset(self, day):
        self.day, self.pings, self.ping_usd, self.avoided = day, 0, 0.0, 0.0
        self.stops = collections.Counter()

    def feed(self, e):
        t = e.get("t")
        if not isinstance(t, (int, float)):
            return
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if day != self.day:
            self._reset(day)
        ev, sid = e.get("event"), e.get("sid")
        if ev == "ping":
            self.pings += 1
            self.ping_usd += e.get("est_cost_usd") or 0.0
            if e.get("ok") and e.get("avoided_rewarm_usd_if_returned") is not None:
                self.stretch[sid] = e["avoided_rewarm_usd_if_returned"]
        elif ev == "real" and e.get("captured"):
            v = self.stretch.pop(sid, None)
            if v:
                self.avoided += v
        elif ev == "decision" and e.get("action") == "stop":
            self.stops[e.get("reason")] += 1

    def as_dict(self, now):
        if self.day != time.strftime("%Y-%m-%d", time.localtime(now)):
            self._reset(None)
        stops = {"cap": 0, "flag": 0, "verify-fail": 0, **{k: v for k, v in self.stops.items() if k}}
        return {"pings": self.pings, "ping_usd": round(self.ping_usd, 4),
                "avoided_rewarm_usd": round(self.avoided, 4), "stops": stops}


# internal stop reason -> STATUS-CONTRACT state
STOP_STATE = {"flag": "stopped", "cap": "capped", "expired-before-ping": "expired", "verify-fail": "verify-fail",
              "controller-error": "verify-fail", "over-max-sessions": "over-max"}


class Proxy:
    PER_TICK = 3   # max new ping dispatches per tick

    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        up = urllib.parse.urlsplit(cfg["upstream"])
        if up.scheme not in ("https", "http") or not up.hostname:
            raise ValueError("KA_UPSTREAM must be http(s)://host[:port]")
        if up.scheme == "http" and up.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("plain-http upstream allowed only on loopback")
        self.up = up
        self.ctx = ssl.create_default_context()
        self.sessions = {}   # sid -> main-loop capture + controller state (body/headers in memory only)
        self.meta = {}       # sid -> stored main {"hashes", "n_tools", "n_msgs"}; in memory only, never logged
        self.tick_no = 0
        self.inflight = set()   # sids with a ping worker running (<= KA_PING_CONCURRENCY)
        self.gens = itertools.count(1)   # capture generation, global so a dropped+recaptured sid never collides
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.httpd = None
        os.makedirs(cfg["state_dir"], mode=0o700, exist_ok=True)
        self.log_path = os.path.join(cfg["state_dir"], "ka.log")
        self.proxy_token = load_or_create_token(cfg["state_dir"])   # memory only; never logged
        self._token_stamp, self._token_lock = self._token_file_stamp(), threading.Lock()
        self.today = Today()
        try:   # rebuild today's totals after a restart (metadata lines only)
            with open(self.log_path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        self.today.feed(e)
        except OSError:
            pass
        self.ca_dir = os.path.join(cfg["state_dir"], "ca")
        self._mitm = (None, None)   # (leaf/key mtimes, server SSLContext)
        self._mitm_lock = threading.Lock()

    def _token_file_stamp(self):
        try:
            st = os.stat(os.path.join(self.cfg["state_dir"], "proxy.token"))
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_ino, st.st_size)

    def current_token(self):
        """The CONNECT credential, re-read when proxy.token changes (ka_ca.py rotation needs no restart).
        A missing file is recreated; an unreadable/malformed one keeps the previous token."""
        stamp = self._token_file_stamp()
        if stamp != self._token_stamp:
            with self._token_lock:
                try:
                    self.proxy_token = load_or_create_token(self.cfg["state_dir"])
                    self._token_stamp = self._token_file_stamp()
                except (OSError, ValueError) as e:
                    self._token_stamp = stamp   # log once per change, not per CONNECT
                    self.log(event="decision", action="skip", reason="token-reload-error", err=type(e).__name__)
        return self.proxy_token

    def mitm_context(self):
        """Server TLS context for the MITM hosts; reloaded when leaf.pem/leaf.key change. None if absent."""
        leaf, key = os.path.join(self.ca_dir, "leaf.pem"), os.path.join(self.ca_dir, "leaf.key")
        try:
            stamp = (os.stat(leaf).st_mtime_ns, os.stat(key).st_mtime_ns)
        except OSError:
            return None
        with self._mitm_lock:
            if self._mitm[0] != stamp:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(leaf, key)
                ctx.set_alpn_protocols(["http/1.1"])
                self._mitm = (stamp, ctx)
            return self._mitm[1]

    # ---- logging (metadata only) ----
    def log(self, **kw):
        t = self.clock()
        if "sid" in kw:
            kw["sid"] = log_sid(kw["sid"])
        kw = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)), "t": round(t, 3), **kw}
        line = (json.dumps(kw) + "\n").encode()
        with self.log_lock:
            self.today.feed(kw)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | O_BINARY, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)

    def upstream_conn(self, timeout):
        u = self.up
        if u.scheme == "https":
            return http.client.HTTPSConnection(u.hostname, u.port or 443, context=self.ctx, timeout=timeout)
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)

    # ---- capture ----
    def classify(self, hdrs, body):
        """Parse a /v1/messages request into capture metadata (hashes stay in memory). Returns rec or None."""
        try:
            j = json.loads(body)
        except ValueError:
            return None
        if not isinstance(j, dict) or not isinstance(j.get("messages"), list):
            return None
        tools = j.get("tools") or []
        return {"sid": session_id({k.lower(): v for k, v in hdrs.items()}, j), "model": j.get("model"),
                "n_msgs": len(j["messages"]), "n_tools": len(tools), "stream": bool(j.get("stream")),
                "hashes": message_hashes(j["messages"]), "compaction": is_compaction(j)}

    def on_real(self, rec, path, hdrs, body, t_start, status, tail):
        if rec["stream"]:
            u = sse_usage(tail)
        else:
            try:
                rj = json.loads(tail or b"{}")
            except ValueError:
                rj = {}
            u = (rj.get("usage") if isinstance(rj, dict) else None) or {}
        uf = usage_fields(u)
        sid, captured, switch, main_switch, reason = rec["sid"], False, False, False, None
        switchback = False
        with self.lock:
            m = self.meta.get(sid)
            if m is not None:
                m["last_seen"] = t_start
            if status == 200 and rec["n_tools"]:
                if self._flagged(sid):
                    reason = "flag"   # stopped/off: never put body/credentials back in memory
                elif is_main(rec, m):
                    switch = bool(m is not None and rec["compaction"] and rec["n_msgs"] < m["n_msgs"])
                    confirmed = m is not None and (rec["compaction"] or extends(rec["hashes"], m["hashes"]))
                    # compaction rewrote the history: no older lineage (previous_main, other) can be
                    # extended legitimately any more, so neither is carried forward
                    self._capture(sid, rec, path, hdrs, body, t_start, uf, confirmed,
                                  None if rec["compaction"] else m)
                    captured = True
                elif m.get("previous_main") and extends(rec["hashes"], m["previous_main"]):
                    # the lineage displaced by the last takeover is back: return to it at once, no timer;
                    # prev_meta None clears previous_main and the tracked other lineage
                    self._capture(sid, rec, path, hdrs, body, t_start, uf, True, None)
                    captured = switchback = True
                else:
                    main_switch = self._track_other(sid, rec, m, t_start)
                    if main_switch:
                        self._capture(sid, rec, path, hdrs, body, t_start, uf, True, None)
                        self.meta[sid]["previous_main"] = m["hashes"]   # one slot: the displaced lineage
                        captured = True
                if captured and self._flagged(sid):   # stop/off written between the check and the capture
                    self._stop(sid, self.sessions[sid], "flag", at="capture")
                    captured, reason, main_switch, switchback = False, "flag", False, False
        if main_switch:
            self.log(event="decision", action="main-switch", reason="stale-main", sid=sid, model=rec["model"],
                     n_msgs=rec["n_msgs"], n_tools=rec["n_tools"])
        if switchback:
            self.log(event="decision", action="main-switchback", reason="previous-main", sid=sid,
                     model=rec["model"], n_msgs=rec["n_msgs"], n_tools=rec["n_tools"])
        extra = {"reason": reason} if reason else {}
        self.log(event="real", sid=sid, model=rec["model"], status=status, n_msgs=rec["n_msgs"],
                 n_tools=rec["n_tools"], stream=rec["stream"], captured=captured, compaction_switch=switch,
                 **extra, **uf, est_cost_usd=est_cost(rec["model"], uf))

    def _capture(self, sid, rec, path, hdrs, body, t_start, uf, confirmed, prev_meta):
        """Store rec as the session's main (caller holds self.lock). prev_meta carries the tracked other
        lineage and previous_main (hash list of the lineage a stale-main takeover displaced) forward."""
        self.meta[sid] = {"hashes": rec["hashes"], "n_tools": rec["n_tools"], "n_msgs": rec["n_msgs"],
                          "confirmed": bool(confirmed), "last_ext": t_start, "last_seen": t_start,
                          "other": (prev_meta or {}).get("other"),
                          "previous_main": (prev_meta or {}).get("previous_main")}
        prefix = uf["read"] + uf["create"]
        self.sessions[sid] = {
            "path": path, "headers": hdrs, "body": body, "t_start": t_start, "model": rec["model"],
            "n_msgs": rec["n_msgs"], "expected_prefix": prefix,
            "cap_h": dynamic_cap_hours(self.cfg, rec["model"], prefix),
            "last_refresh": t_start, "idle_start": t_start, "pings": 0, "status": "active",
            "stop_reason": None, "retry_at": None, "gen": next(self.gens)}

    def _track_other(self, sid, rec, m, t):
        """Follow the most recent non-main tools-bearing lineage (caller holds self.lock). Returns True when
        main should switch to it: the stored main has not been extended for 30 min and this lineage was
        extended >= 3 times in the last 10 min. No message-count condition (a mistaken first capture may be
        longer than the real main ever gets). A subagent that keeps extending for > 30 min while main waits
        on it does displace main, but the displaced lineage is kept as previous_main and its next request
        switches back at once (on_real); verify-fail remains the safety net."""
        o = m.get("other")
        if o and len(rec["hashes"]) > len(o["hashes"]) and extends(rec["hashes"], o["hashes"]):
            o["hashes"], o["n_msgs"] = rec["hashes"], rec["n_msgs"]
            o["exts"].append(t)
        elif not (o and rec["hashes"] == o["hashes"]):
            m["other"] = o = {"hashes": rec["hashes"], "n_msgs": rec["n_msgs"], "exts": []}
        o["exts"] = [x for x in o["exts"] if t - x <= 600]
        return t - m["last_ext"] >= 1800 and len(o["exts"]) >= 3

    # ---- controller ----
    def _stop(self, sid, s, reason, action="stop", **extra):
        s["status"] = STOP_STATE.get(reason, "verify-fail")
        s.setdefault("inactive_since", self.clock())   # internal: retention clock (KA_INACTIVE_RETAIN_H)
        s["stop_reason"] = reason
        s["body"] = s["headers"] = None   # nothing left to replay; drop credentials early
        s["retry_at"] = None
        self.log(event="decision", action=action, reason=reason, sid=sid, model=s["model"],
                 pings=s["pings"], **extra)

    def _flagged(self, sid):
        """Global off, or a stop file named by the raw sid (hook) or its logged form (kactl / kadash)."""
        sd = self.cfg["state_dir"]
        if os.path.exists(os.path.join(sd, "off")):
            return True
        names = {log_sid(sid)} | ({sid} if _STOP_NAME_OK.fullmatch(sid) else set())
        return any(os.path.exists(os.path.join(sd, "stop", n)) for n in names)

    def _extend_until(self, sid, now):
        """User extension for sid: the deadline in STATE_DIR/extend/<sid> (raw or logged name; absolute
        epoch seconds), bounded by KA_EXTEND_MAX_H from when the file was written (and from now, for a
        future mtime) -- a now-only bound would slide forever. None if absent, unreadable or past."""
        d = os.path.join(self.cfg["state_dir"], "extend")
        span = self.cfg["extend_max_h"] * 3600
        best = None
        for n in {log_sid(sid)} | ({sid} if _STOP_NAME_OK.fullmatch(sid) else set()):
            p = os.path.join(d, n)
            try:
                with open(p) as f:
                    v = float(f.read().strip())
                v = min(v, min(os.stat(p).st_mtime, now) + span)
            except (OSError, ValueError):
                continue
            if v == v and v > now:   # v == v: not NaN
                best = v if best is None else max(best, v)
        return best

    def _prune_extends(self, now):
        """Remove extend files whose (bounded) deadline has passed; unparseable ones are left alone."""
        d = os.path.join(self.cfg["state_dir"], "extend")
        span = self.cfg["extend_max_h"] * 3600
        try:
            names = os.listdir(d)
        except OSError:
            return
        for n in names:
            p = os.path.join(d, n)
            try:
                with open(p) as f:
                    v = float(f.read().strip())
                if min(v, min(os.stat(p).st_mtime, now) + span) <= now:
                    os.remove(p)
            except (OSError, ValueError):
                pass

    def tick(self):
        """One controller pass over every tracked session. Due pings are dispatched to worker threads,
        most urgent (oldest refresh) first: at most PER_TICK new ones per tick and at most
        KA_PING_CONCURRENCY in flight, so a slow upstream cannot serialize pings past the TTL."""
        self.tick_no += 1
        try:
            with self.lock:
                now = self.clock()
                # forget finished tracks and their message-hash metadata KA_INACTIVE_RETAIN_H after they went
                # non-active (stop/extend files stay; the sid reappears on its next real request)
                retain = self.cfg["inactive_retain_h"] * 3600
                for k in [k for k, s in self.sessions.items() if s["status"] != "active"
                          and now - s.get("inactive_since", s["t_start"]) > retain]:
                    del self.sessions[k]
                    self.meta.pop(k, None)
                for k in [k for k, m in self.meta.items()
                          if k not in self.sessions and now - m.get("last_seen", 0) > 86400]:
                    del self.meta[k]
                self._prune_extends(now)
                active = [k for k, s in self.sessions.items() if s["status"] == "active"]
                if len(active) > self.cfg["max_sessions"]:
                    active.sort(key=lambda k: self.sessions[k]["t_start"], reverse=True)
                    for k in active[self.cfg["max_sessions"]:]:   # listed as over-max; logged once
                        self._stop(k, self.sessions[k], "over-max-sessions", action="skip")
                    active = active[:self.cfg["max_sessions"]]
                due = []
                for k in active:
                    s = self.sessions[k]
                    if k in self.inflight:            # its worker re-checks and applies the result
                        continue
                    if self._flagged(k):             # manual stop: effective within one tick, idle or not
                        self._stop(k, s, "flag")
                        continue
                    # cap 0 = not worth keeping: first tick -- unless a user extension (kactl extend) is set
                    ext = s["extend_until"] = self._extend_until(k, now)
                    if now >= max(s["idle_start"] + s["cap_h"] * 3600, ext or 0):
                        self._stop(k, s, "cap", idle_s=round(now - s["idle_start"]), cap_h=s["cap_h"])
                        continue
                    since = now - s["last_refresh"]
                    if s.get("retry_at") is not None:   # scheduled single retry (window checked at schedule)
                        if now < s["retry_at"]:
                            continue
                        if since > self.cfg["ttl"] - 30:
                            self._stop(k, s, "expired-before-ping", since_refresh_s=round(since))
                        else:
                            due.append(k)
                        continue
                    if since < self.cfg["ping_after"]:
                        continue
                    if since > self.cfg["ttl"] - self.cfg["margin"]:
                        self._stop(k, s, "expired-before-ping", since_refresh_s=round(since))
                    else:
                        due.append(k)
                due.sort(key=lambda k: self.sessions[k]["last_refresh"])   # closest to expiry first
                slots = min(self.PER_TICK, self.cfg["ping_concurrency"] - len(self.inflight))
                for sid in due[:max(0, slots)]:
                    s = self.sessions[sid]
                    snap = {k: s[k] for k in ("path", "headers", "body", "gen", "model", "expected_prefix",
                                              "last_refresh", "idle_start", "pings")}
                    snap["tick"] = self.tick_no
                    self.inflight.add(sid)
                    threading.Thread(target=self._ping_worker, args=(sid, snap, s.get("retry_at") is not None),
                                     daemon=True).start()
        except Exception as e:  # never let the controller take down pass-through
            self.log(event="decision", action="skip", reason="controller-error", err=type(e).__name__)
        finally:
            try:
                self.write_status()
            except Exception as e:
                self.log(event="decision", action="skip", reason="status-write-error", err=type(e).__name__)

    def write_status(self):
        """STATE_DIR/status.json per STATUS-CONTRACT.md (schema 1, 0600, atomic replace, metadata only)."""
        now = self.clock()
        with self.lock:
            rows = []
            for k, s in self.sessions.items():
                active = s["status"] == "active"
                retry = active and s.get("retry_at") is not None
                p = s["expected_prefix"]
                ext = s.get("extend_until") if active else None
                cap_end = max(s["idle_start"] + s["cap_h"] * 3600, ext or 0)   # effective, incl. extension
                rows.append({
                    "sid": log_sid(k), "model": s["model"], "prefix_tokens": p,
                    "last_real_ts": round(s["t_start"], 3), "last_refresh_ts": round(s["last_refresh"], 3),
                    "next_ping_ts": (round(s["retry_at"], 3) if retry else
                                     round(s["last_refresh"] + self.cfg["ping_after"], 3) if active else None),
                    "pings": s["pings"], "cap_h": s["cap_h"],
                    "cap_end_ts": round(cap_end, 3),
                    **({"extend_until_ts": round(ext, 3)} if ext else {}),
                    "state": "retry-wait" if retry else s["status"], "stop_reason": s.get("stop_reason"),
                    "est_ping_usd": est_ping_usd(s["model"], p),
                    "est_rewarm_usd": est_rewarm_usd(s["model"], p, self.cfg["shared_prefix"])})
        with self.log_lock:
            today = self.today.as_dict(now)
        doc = {"schema": 1, "updated": round(now, 3), "proxy_pid": os.getpid(),
               "global_off": os.path.exists(os.path.join(self.cfg["state_dir"], "off")),
               "sessions": rows, "today": today}
        path = os.path.join(self.cfg["state_dir"], "status.json")
        tmp = path + ".tmp"
        try:   # a leftover tmp may carry looser permissions; never inherit them
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
        set_mode(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        replace_retry(tmp, path)

    def replay(self, path, headers, body):
        """Send body with max_tokens=0, stream=false.

        Returns {status|None, usage, err (exception type)|None, error_type, retry_after, retry_after_s, rl}.
        Error metadata only: error.type (never the message text) and whitelisted-shape header values.
        """
        j = json.loads(body)
        j["max_tokens"] = 0
        j["stream"] = False
        data = json.dumps(j, ensure_ascii=False).encode()
        out = {"status": None, "usage": usage_fields({}), "err": None, "error_type": None,
               "retry_after": None, "retry_after_s": None, "rl": None}
        conn = self.upstream_conn(10)          # connect (+TLS handshake) bound: 10 s
        try:
            conn.connect()
            conn.sock.settimeout(60)           # response bound: 60 s (max_tokens=0 replies are one small read)
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except OSError as e:
            out["err"] = type(e).__name__
            return out
        finally:
            conn.close()
        try:
            rj = json.loads(raw)
        except ValueError:
            rj = {}
        rj = rj if isinstance(rj, dict) else {}
        out["status"] = resp.status
        out["usage"] = usage_fields(rj.get("usage") or {})
        if resp.status != 200:
            et = (rj.get("error") or {}).get("type") if isinstance(rj.get("error"), dict) else None
            out["error_type"] = et if isinstance(et, str) and re.fullmatch(r"[a-z_]{1,64}", et) else None
            rl = {}
            for k, v in resp.getheaders():
                kl = k.lower()
                if kl == "retry-after" or kl == "x-should-retry" or kl.startswith("anthropic-ratelimit-"):
                    rl[kl] = safe_header_value(v)
            ra = rl.pop("retry-after", None)
            out["retry_after"], out["retry_after_s"] = ra, retry_after_seconds(ra)
            out["rl"] = rl or None
        return out

    def _attempt(self, sid, snap, retry):
        t0 = self.clock()
        r = self.replay(snap["path"], snap["headers"], snap["body"])
        uf, exp = r["usage"], snap["expected_prefix"]
        ok = (r["status"] == 200 and exp > 0 and uf["read"] >= self.cfg["min_read_frac"] * exp
              and uf["create"] <= 0.05 * exp)
        p = price_for(snap["model"])
        avoided = round(exp * (p["write1h"] - p["read"]) / 1e6, 6) if (p and ok) else None
        extra = {}
        if r["status"] != 200:
            extra = {"error_type": r["error_type"], "retry_after": r["retry_after"], "rl_headers": r["rl"]}
        self.log(event="ping", sid=sid, model=snap["model"], status=r["status"], ok=ok, err=r["err"],
                 retry=retry, tick=snap.get("tick", self.tick_no), expected_prefix=exp, age_s=round(t0 - snap["last_refresh"]),
                 idle_s=round(t0 - snap["idle_start"]), pings=snap["pings"] + (1 if ok else 0), **extra,
                 **uf, est_cost_usd=est_cost(snap["model"], uf), avoided_rewarm_usd_if_returned=avoided)
        return t0, ok, r

    def _retry_wait(self, snap, r):
        """Seconds to wait before the single retry, or None if the failure is not transient."""
        st = r["status"]
        if st is None or st == 200 or st in (400, 401, 403):
            return None
        ra = r["retry_after_s"]
        if st == 529 or r["error_type"] == "overloaded_error":
            wait = ra if ra is not None else self.cfg["retry_default"]
        elif st == 429 and ra is not None:
            wait = ra
        else:
            return None
        window = min(120.0, snap["last_refresh"] + self.cfg["ttl"] - 30 - self.clock())
        return wait if 0 <= wait <= window else None

    def _ping_worker(self, sid, snap, retry):
        try:
            self._ping(sid, snap, retry)
        except Exception as e:  # a broken capture stops only its own session
            with self.lock:
                s = self.sessions.get(sid)
                if s is not None and s["status"] == "active" and s["gen"] == snap["gen"]:
                    self._stop(sid, s, "controller-error", err=type(e).__name__)
                else:
                    self.log(event="decision", action="stop", reason="controller-error", sid=sid,
                             err=type(e).__name__)
        finally:
            with self.lock:
                self.inflight.discard(sid)

    def _ping(self, sid, snap, retry=False):
        """One attempt. A transient first failure schedules ONE retry (s["retry_at"]) that a later tick
        fires, so no session ever waits on another session's retry."""
        with self.lock:   # re-check right before sending: never let a ping land after the cache expired
            s = self.sessions.get(sid)
            if s is None or s["gen"] != snap["gen"] or s["status"] != "active":
                return
            age = self.clock() - s["last_refresh"]
            if age > self.cfg["ttl"] - (30 if retry else self.cfg["margin"]):
                self._stop(sid, s, "expired-before-ping", since_refresh_s=round(age), at="send")
                return
        t0, ok, r = self._attempt(sid, snap, retry)
        wait = None if (ok or retry) else self._retry_wait(snap, r)
        with self.lock:
            s = self.sessions.get(sid)
            if s is None or s["gen"] != snap["gen"] or s["status"] != "active":
                return   # superseded by a real request, or stopped (flag/cap/over-max) while in flight
            s["retry_at"] = None
            if ok:
                s["last_refresh"] = t0
                s["pings"] += 1
            elif wait is not None:
                s["retry_at"] = self.clock() + wait
                self.log(event="decision", action="retry", reason="transient", sid=sid, model=snap["model"],
                         http_status=r["status"], error_type=r["error_type"], wait_s=wait)
            else:
                self._stop(sid, s, "verify-fail", http_status=r["status"], error_type=r["error_type"],
                           retry_after=r["retry_after"])

    def _loop(self):
        while not self.stop_evt.wait(self.cfg["tick"]):
            self.tick()

    # ---- lifecycle ----
    def start(self):
        """Bind 127.0.0.1, start server + scheduler threads. Returns the bound port."""
        self.httpd = LoopbackServer(("127.0.0.1", self.cfg["port"]), Handler)
        self.httpd.daemon_threads = True
        self.httpd.ka = self
        port = self.httpd.server_address[1]
        self.log(event="start", port=port, upstream_host=self.up.hostname,
                 ping_after_s=self.cfg["ping_after"], ttl_s=self.cfg["ttl"], tick_s=self.cfg["tick"],
                 mitm_hosts=sorted(self.cfg["mitm_hosts"]),
                 leaf_present=os.path.exists(os.path.join(self.ca_dir, "leaf.pem")))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        return port

    def shutdown(self):
        self.stop_evt.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    in_tunnel = False

    def log_message(self, *a):
        pass

    def _relay(self, method, body):
        ka = self.server.ka
        hdrs = dict(strip_hop(self.headers.items()))
        path, t_start, rec = self.path, ka.clock(), None
        if method == "POST" and path.startswith("/v1/messages") and "count_tokens" not in path:
            try:
                rec = ka.classify(hdrs, body)
            except Exception as e:
                ka.log(event="decision", action="skip", reason="controller-error", err=type(e).__name__)
        conn = ka.upstream_conn(ka.cfg["upstream_read_timeout"])
        abort = None   # stream-timeout / upstream-error: upstream stream broke mid-body
        try:
            try:
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
            except OSError:
                self.send_error(502, "upstream unreachable")
                return
            self.send_response_only(resp.status)   # upstream's own Date/Server pass through, no duplicates
            for k, v in strip_hop(resp.getheaders()):
                self.send_header(k, v)
            tap = UsageTap(rec["stream"]) if rec else None   # bounded: never the whole response
            if method == "HEAD" or resp.status in (204, 304) or resp.status < 200:
                cl = resp.getheader("content-length")
                if cl is not None and (method == "HEAD" or resp.status == 304):
                    self.send_header("Content-Length", cl)   # metadata only: the length of the GET body
                self.end_headers()   # no message body allowed; a chunk terminator would desync keep-alive
                resp.read()
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                declared = None if resp.chunked else resp.length   # upstream Content-Length, if fixed-length
                received = 0
                while True:
                    try:
                        chunk = resp.read1(65536)
                    except (OSError, http.client.HTTPException) as e:
                        abort = ("stream-timeout" if isinstance(e, TimeoutError) else "upstream-error",
                                 type(e).__name__)
                        break
                    if not chunk:   # read1 returns b"" on EOF even when short of Content-Length
                        if declared is not None and received < declared:
                            abort = ("upstream-truncated", None)
                        break
                    received += len(chunk)
                    if tap:
                        tap.feed(chunk)
                    try:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    except OSError as e:   # client gone: stop reading upstream; finally closes it
                        abort = ("client-gone", type(e).__name__)
                        break
                if abort:   # no terminator: the client must see a truncated body, then the close
                    self.close_connection = True
                else:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        finally:
            conn.close()
        if abort:
            ka.log(event="decision", action="abort", reason=abort[0], tunnel=self.in_tunnel, method=method,
                   **({"err": abort[1]} if abort[1] else {}), **({"sid": rec["sid"]} if rec else {}))
            return
        if rec:
            try:
                ka.on_real(rec, path, hdrs, body, t_start, resp.status, tap.result())
            except Exception as e:
                ka.log(event="decision", action="skip", reason="controller-error", sid=rec["sid"],
                       err=type(e).__name__)

    def _path_refused(self):
        """Plain front door serves the API only: paths outside /v1/ get 404 (inside an authenticated
        MITM tunnel every path is relayed). Returns True if the request was refused."""
        if self.in_tunnel or self.path.startswith("/v1/"):
            return False
        self.server.ka.log(event="decision", action="reject", reason="plain-path-not-api", method=self.command)
        self.send_error(404)   # also sets close_connection
        return True

    def do_GET(self):
        if not self._path_refused():
            self._relay("GET", None)

    def do_HEAD(self):
        if not self._path_refused():
            self._relay("HEAD", None)

    def _with_body(self):
        if self._path_refused():
            self._drain()
            return
        if self.headers.get("transfer-encoding"):
            self.server.ka.log(event="decision", action="reject", reason="chunked-request-body",
                               tunnel=self.in_tunnel, method=self.command)
            self.send_error(411, "chunked request bodies not supported")   # also sets close_connection
            self._drain()
            return
        n = content_length(self.headers.get_all("content-length"))
        if n is None:   # never log the header value itself
            self.server.ka.log(event="decision", action="reject", reason="bad-content-length",
                               tunnel=self.in_tunnel, method=self.command)
            self.send_error(400, "bad Content-Length")
            self.close_connection = True
            self._drain()
            return
        self._relay(self.command, self.rfile.read(n) if n else b"")

    do_POST = do_PUT = do_PATCH = do_DELETE = _with_body

    def _drain(self, limit=1 << 20, secs=2.0):
        """Swallow the unread request body before closing, so the close is not a TCP RST that
        destroys the error response still in flight to the client (lingering close)."""
        try:
            self.connection.settimeout(secs)
            n = 0
            while n < limit:
                d = self.rfile.read1(65536)
                if not d:
                    break
                n += len(d)
        except (OSError, ValueError):
            pass

    # ---- HTTPS_PROXY front door ----
    def do_CONNECT(self):
        ka = self.server.ka
        if self.in_tunnel:
            return self.send_error(400, "nested CONNECT")
        auth = self.headers.get_all("proxy-authorization")
        if not proxy_auth_ok(auth, ka.current_token()):   # never log the header or the target
            ka.log(event="decision", action="reject", reason="proxy-auth-fail", had_header=bool(auth))
            self.close_connection = True
            self.send_response_only(407, "Proxy Authentication Required")
            self.send_header("Proxy-Authenticate", 'Basic realm="ka"')
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        host, sep, port = self.path.rpartition(":")
        if not sep or not port.isdigit():
            return self.send_error(400, "bad CONNECT target")
        host, port = host.strip("[]").lower(), int(port)
        if host in ka.cfg["mitm_hosts"] and port == 443:
            try:
                ctx, err = ka.mitm_context(), "missing"
            except Exception as e:
                ctx, err = None, type(e).__name__
            if ctx:
                return self._mitm(ctx, host, port)
            ka.log(event="decision", action="skip", reason="no-leaf-cert", host=host, err=err)
        self._blind(host, port)

    def _mitm(self, ctx, host, port):
        ka, t0 = self.server.ka, time.monotonic()
        self.close_connection = True
        self.send_response_only(200, "Connection Established")
        self.end_headers()
        self.connection.settimeout(30)   # handshake bound
        try:
            tls = ctx.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError) as e:
            ka.log(event="tunnel", mode="mitm", host=host, port=port, ok=False, err=type(e).__name__)
            return
        tls.settimeout(None)
        err = None
        try:
            TunnelHandler(tls, self.client_address, self.server)   # serves keep-alive requests until close
        except Exception as e:
            err = type(e).__name__
        finally:
            try:
                tls.close()
            except OSError:
                pass
        ka.log(event="tunnel", mode="mitm", host=host, port=port, ok=True, err=err,
               dur_s=round(time.monotonic() - t0, 3))

    def _blind(self, host, port):
        ka, t0 = self.server.ka, time.monotonic()
        self.close_connection = True
        try:
            up = socket.create_connection((host, port), timeout=ka.cfg["tunnel_connect_timeout"])
        except OSError as e:
            ka.log(event="tunnel", mode="blind", host=host, port=port, ok=False, err=type(e).__name__)
            return self.send_error(502, "tunnel connect failed")
        try:
            up.settimeout(None)
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            self.connection.settimeout(None)
            n_up, n_down = pump(self.connection, up)
        finally:
            up.close()
        ka.log(event="tunnel", mode="blind", host=host, port=port, ok=True, bytes_up=n_up,
               bytes_down=n_down, dur_s=round(time.monotonic() - t0, 3))

class TunnelHandler(Handler):
    """HTTP/1.1 requests arriving inside a TLS-terminated CONNECT; same relay path as plain requests."""
    in_tunnel = True


def main():
    ka = Proxy(load_config())
    ka.start()
    if hasattr(signal, "SIGTERM"):   # systemd/launchd stop: shut down cleanly
        signal.signal(signal.SIGTERM, lambda *a: ka.stop_evt.set())
    try:
        while not ka.stop_evt.wait(1.0):   # timed wait: Ctrl+C is not delivered during an untimed wait on Windows
            pass
    except KeyboardInterrupt:
        pass
    finally:
        ka.shutdown()


if __name__ == "__main__":
    main()
