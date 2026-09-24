#!/usr/bin/env python3
"""kadash: local web dashboard for the ka prompt-cache keepalive proxy (stdlib; Python 3.10+).

Reads STATE_DIR/status.json (written by ka_proxy.py, see STATUS-CONTRACT.md) and
controls the proxy ONLY through files: STATE_DIR/stop/<sid> and STATE_DIR/off.
Never talks to the proxy over HTTP.

Env: KA_STATE_DIR (default ~/.claude/ka), KA_DASH_PORT (default 8788),
     KA_PROJECTS_DIR (default ~/.claude/projects; read-only, session titles).
Session titles: the last {"type":"custom-title"} line of <projects>/*/<sid>.jsonl,
read backwards, cached per sid; display-only (status endpoint `_titles`), never
written to status.json, logs or stdout.
Usage: kadash.py          serve on 127.0.0.1:<port>, print the URL once
       kadash.py --url    print the URL (creates the token if needed) and exit
Security: random token in STATE_DIR/dash.token (0600), every path lives under
/<token>/; Host header must be 127.0.0.1:<port> or localhost:<port>; actions are
POST + header X-KA-Dash: 1; sid must be exactly one listed in status.json.
"""
import glob
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = "127.0.0.1"          # loopback only; deliberately not configurable
DEFAULT_PORT = 8788
MAX_BODY = 4096
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TITLE_CHUNK = 64 * 1024
TITLE_SCAN_MAX = 8 * 1024 * 1024
TITLE_MAX_CHARS = 200
NO_TITLE = "—"          # em dash: no transcript found
# macOS/Linux project dirs look like "-Users-alice-...", Windows ones like "C--Users-alice-..."
_PROJ_PREFIX_RE = re.compile(r"^(?:[A-Za-z]-)?-(?:Users|home)-[^-]+-(?:Workspace-)?")
_PROJ_HOME_RE = re.compile(r"^(?:[A-Za-z]-)?-(?:Users|home)-[^-]+$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def state_dir_from_env():
    return os.path.expanduser(os.environ.get("KA_STATE_DIR", "~/.claude/ka"))


def projects_dir_from_env():
    return os.path.expanduser(os.environ.get("KA_PROJECTS_DIR", "~/.claude/projects"))


def project_label(dirname):
    """'-Users-alice-Workspace-my-project' (or 'C--Users-alice-...') -> 'my-project'; bare home dir -> '~'."""
    trimmed = _PROJ_PREFIX_RE.sub("", dirname, count=1)
    if trimmed and trimmed != dirname:
        return trimmed
    if _PROJ_HOME_RE.match(dirname):
        return "~"
    return dirname


def _parse_title(line, sid):
    """Title string if `line` is a custom-title record for `sid` ('' = cleared), else None."""
    if b'"custom-title"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") != "custom-title":
        return None
    title = rec.get("customTitle")
    if not isinstance(title, str):
        return None
    if rec.get("sessionId") not in (None, sid):
        return None
    return _CTRL_RE.sub(" ", title).strip()[:TITLE_MAX_CHARS]


def scan_title_back(f, lo, hi, sid):
    """Scan complete lines of [lo, hi) newest-first, at most TITLE_SCAN_MAX bytes.
    Returns (title_or_None, tail_end) where tail_end = offset just past the last
    newline seen (lo if none) -- where the next incremental scan may start."""
    pos, carry, scanned, tail_end = hi, b"", 0, None
    while pos > lo and scanned < TITLE_SCAN_MAX:
        n = min(TITLE_CHUNK, pos - lo, TITLE_SCAN_MAX - scanned)
        pos -= n
        f.seek(pos)
        raw = f.read(n)
        scanned += n
        if tail_end is None and b"\n" in raw:
            tail_end = pos + raw.rfind(b"\n") + 1
        lines = (raw + carry).split(b"\n")
        carry = lines[0]                       # partial unless pos == lo
        for line in reversed(lines[1:]):
            t = _parse_title(line, sid)
            if t is not None:
                return t, tail_end if tail_end is not None else lo
    if pos == lo and carry:
        t = _parse_title(carry, sid)
        if t is not None:
            return t, tail_end if tail_end is not None else lo
    return None, tail_end if tail_end is not None else lo


class TitleCache:
    """sid -> display title from Claude Code transcripts, cached by (path, ino, mtime, size).
    An appended-to file is scanned only over its new tail."""

    def __init__(self, projects_dir):
        self.projects_dir = projects_dir
        self.reads = 0                          # transcript opens (tests count these)
        self._cache = {}                        # sid -> dict(key, path, tail_end, custom, result)
        self._lock = threading.Lock()

    def _find(self, sid):
        pat = os.path.join(glob.escape(self.projects_dir), "*", sid + ".jsonl")
        best = None
        for p in glob.glob(pat):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if best is None or st.st_mtime_ns > best[1].st_mtime_ns:
                best = (p, st)
        return best

    def _lookup_one(self, sid):
        found = self._find(sid)
        if found is None:
            self._cache.pop(sid, None)
            return {"title": NO_TITLE, "project": "", "custom": False}
        path, st = found
        key = (path, st.st_ino, st.st_mtime_ns, st.st_size)
        ent = self._cache.get(sid)
        if ent and ent["key"] == key:
            return ent["result"]
        project = project_label(os.path.basename(os.path.dirname(path)))
        appended = bool(ent and ent["key"][:2] == key[:2] and st.st_size >= ent["key"][3])
        lo = ent["tail_end"] if appended else 0
        try:
            self.reads += 1
            with open(path, "rb") as f:
                title, tail_end = scan_title_back(f, lo, st.st_size, sid)
        except OSError:
            return {"title": project or NO_TITLE, "project": project, "custom": False}
        if title is None and appended:
            title = ent["custom"]                # nothing newer in the tail: keep the old answer
        custom = title or None                   # '' (cleared) counts as no title
        result = {"title": custom or project or NO_TITLE, "project": project, "custom": bool(custom)}
        self._cache[sid] = {"key": key, "tail_end": tail_end, "custom": title, "result": result}
        return result

    def lookup(self, sids):
        """{sid: {title, project, custom}} for canonical-UUID sids; prunes other cache entries."""
        want = [s for s in sids if isinstance(s, str) and UUID_RE.match(s)]
        with self._lock:
            out = {s: self._lookup_one(s) for s in want}
            for s in list(self._cache):
                if s not in out:
                    del self._cache[s]
        return out


def load_or_create_token(state_dir):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    path = os.path.join(state_dir, "dash.token")
    try:
        with open(path) as f:
            tok = f.read().strip()
        if TOKEN_RE.match(tok):
            os.chmod(path, 0o600)
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(24)
    tmp = path + ".tmp.%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    for i in range(20):   # Windows: a concurrent reader may hold the file open for a moment
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if os.name != "nt" or i == 19:
                raise
            time.sleep(0.05)
    return tok


def read_status(state_dir):
    """Parsed status.json dict, or None if missing/unreadable/not the contract shape."""
    try:
        with open(os.path.join(state_dir, "status.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or not isinstance(st.get("sessions"), list):
        return None
    return st


def listed_sids(st):
    if not st:
        return set()
    return {s["sid"] for s in st["sessions"] if isinstance(s, dict) and isinstance(s.get("sid"), str)}


def control_state(state_dir):
    stop_dir = os.path.join(state_dir, "stop")
    stops = []
    try:
        with os.scandir(stop_dir) as it:
            stops = sorted(e.name for e in it if e.is_file(follow_symlinks=False))
    except OSError:
        pass
    return {"off_file": os.path.exists(os.path.join(state_dir, "off")),
            "stop_files": stops, "server_now": time.time()}


def _touch(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("kadash %d\n" % int(time.time()))


def _rm(path):
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def do_action(state_dir, action, sid):
    """Apply one control action. Returns (http_code, result_dict)."""
    stop_dir = os.path.join(state_dir, "stop")
    if action in ("stop", "resume"):
        # sid must be a plain file name AND exactly one of the listed sessions
        if not isinstance(sid, str) or not SID_RE.match(sid) or sid not in listed_sids(read_status(state_dir)):
            return 400, {"ok": False, "error": "unknown sid"}
        path = os.path.join(stop_dir, sid)
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(stop_dir):
            return 400, {"ok": False, "error": "bad sid"}
        if action == "stop":
            os.makedirs(stop_dir, mode=0o700, exist_ok=True)
            _touch(path)
        else:
            _rm(path)
        return 200, {"ok": True, "action": action, "sid": sid}
    if action == "stop_all":
        _touch(os.path.join(state_dir, "off"))
        return 200, {"ok": True, "action": action}
    if action == "resume_all":
        _rm(os.path.join(state_dir, "off"))
        removed = 0
        try:
            with os.scandir(stop_dir) as it:
                for e in it:
                    if e.is_file(follow_symlinks=False) or e.is_symlink():
                        removed += _rm(os.path.join(stop_dir, e.name))
        except FileNotFoundError:
            pass
        return 200, {"ok": True, "action": action, "stop_files_removed": removed}
    return 400, {"ok": False, "error": "unknown action"}


class Handler(BaseHTTPRequestHandler):
    server_version = "kadash"
    sys_version = ""

    # -- helpers ---------------------------------------------------------
    def log_message(self, fmt, *args):
        msg = fmt % args
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                      msg.replace(self.server.token, "<token>")))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _allowed_origins(self):
        port = self.server.server_address[1]
        return {"%s:%d" % (h, port) for h in ("127.0.0.1", "localhost")}

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self._allowed_origins()

    def _route(self):
        """Returns sub-path after /<token> (e.g. '/', '/status.json'), or None."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        parts = path.split("/", 2)          # ['', token, rest]
        if len(parts) < 2 or not hmac.compare_digest(parts[1].encode(), self.server.token.encode()):
            return None
        return "/" + parts[2] if len(parts) == 3 else ""

    def _gate(self):
        if not self._host_ok():
            self._send(403, {"error": "forbidden host"})
            return None
        sub = self._route()
        if sub is None:
            self._send(404, {"error": "not found"})
            return None
        return sub

    # -- methods ---------------------------------------------------------
    def do_GET(self):
        sub = self._gate()
        if sub is None:
            return
        sd = self.server.state_dir
        if sub == "":
            self._send(301, "", "text/plain", {"Location": "/%s/" % self.server.token})
        elif sub == "/":
            st = read_status(sd)
            self._send(200, render_page(st is not None), "text/html; charset=utf-8")
        elif sub == "/status.json":
            st = read_status(sd)
            out = dict(st) if st is not None else {"error": "proxy not reporting"}
            out["_dash"] = control_state(sd)
            if st is not None:              # display-only; never persisted or logged
                out["_titles"] = self.server.titles.lookup(sorted(listed_sids(st)))
            self._send(200, out)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        sub = self._gate()
        if sub is None:
            return
        if sub != "/action":
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("X-KA-Dash") != "1":
            self._send(403, {"error": "missing X-KA-Dash header"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower().removeprefix("http://") not in self._allowed_origins():
            self._send(403, {"error": "forbidden origin"})
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._send(413 if n > MAX_BODY else 400, {"ok": False, "error": "bad body size"})
            return
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        code, res = do_action(self.server.state_dir, req.get("action"), req.get("sid"))
        self._send(code, res)

    def _no(self):
        self._send(405, {"error": "method not allowed"}, extra={"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


class LoopbackServer(ThreadingHTTPServer):
    """On Windows: exclusive bind (SO_REUSEADDR there would let a second process share the port)."""
    if os.name == "nt":
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def make_server(state_dir, port, projects_dir=None):
    """Bound (not yet serving) server on 127.0.0.1:<port>; port 0 = pick a free one."""
    srv = LoopbackServer((BIND, port), Handler)
    srv.daemon_threads = True
    srv.state_dir = state_dir
    srv.titles = TitleCache(projects_dir or projects_dir_from_env())
    srv.token = load_or_create_token(state_dir)
    return srv


def dash_url(port, token):
    return "http://%s:%d/%s/" % (BIND, port, token)


# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>ka dash</title>
<style>
:root{--bg:#0c0c0c;--fg:#c8c8c8;--dim:#6c6c6c;--green:#5fd75f;--yellow:#d7d75f;--red:#ff5f5f;
--cyan:#5fd7d7;--mag:#d787d7;--blue:#5f87d7;--line:#1f1f1f}
html,body{background:var(--bg);color:var(--fg);margin:0}
body{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:12px 16px 24px}
.hdr{white-space:pre-wrap;margin-bottom:8px}
.dim{color:var(--dim)}.g{color:var(--green)}.y{color:var(--yellow)}.r{color:var(--red)}
.c{color:var(--cyan)}.m{color:var(--mag)}.b{color:var(--blue)}
.wrap{padding-bottom:16px}          /* no inner scroll box: the page scrolls horizontally */
.tt{display:block;width:24ch;max-width:24ch;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
table{border-collapse:collapse;white-space:nowrap}
th{color:var(--dim);font-weight:normal;text-align:left;padding:0 14px 2px 0;border-bottom:1px solid var(--line)}
td{padding:1px 14px 1px 0}
td.n,th.n{text-align:right}
tr:hover td{background:#141414}
button{font:inherit;background:transparent;color:var(--fg);border:1px solid #3a3a3a;padding:0 6px;cursor:pointer}
button:hover{border-color:var(--fg)}
button:disabled{color:var(--dim);border-color:#222;cursor:default}
button.stop{color:var(--red)}button.res{color:var(--green)}
#msg{margin-top:6px;min-height:1.4em}
</style></head><body>
<div class="hdr" id="hdr">ka-proxy <span class="r">%%INITIAL%%</span></div>
<div class="hdr"><button class="stop" id="stopall">stop all</button> <button class="res" id="resall">resume all</button> <span class="dim" id="tick"></span></div>
<div class="wrap"><table><thead><tr>
<th>SID</th><th>TITLE</th><th>MODEL</th><th class="n">PREFIX</th><th>STATE</th><th class="n">IDLE</th><th class="n">NEXT PING</th>
<th class="n">CAP LEFT</th><th class="n">PINGS</th><th class="n">EST PING$</th><th class="n">REWARM$</th><th></th>
</tr></thead><tbody id="rows"></tbody></table></div>
<div id="msg"></div>
<script>
"use strict";
const STALE_S = 90, PENDING_MAX_S = 180;
const COLOR = {"active":"g","retry-wait":"y","stopped":"m","capped":"c","expired":"dim",
               "verify-fail":"r","over-max":"r"};
let last = null, pending = {}, gpending = null;   // pending[sid] = {kind, t}; gpending = {kind, t}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function num(x){return typeof x==="number"&&isFinite(x);}
function dur(s){ if(!num(s)) return "-"; if(s<0) return "now"; s=Math.round(s);
  if(s<60) return s+"s"; if(s<3600) return Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s";
  return Math.floor(s/3600)+"h"+String(Math.floor(s%3600/60)).padStart(2,"0")+"m"; }
function hm(ts){ return num(ts)? new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}) : "-"; }
function hms(ts){ return num(ts)? new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}) : "-"; }
function usd(x){ return num(x)? "$"+x.toFixed(2) : "-"; }
function kt(x){ return num(x)? Math.round(x/1000)+"k" : "-"; }
function titleCell(t){
  // fixed ~24ch cell, CSS ellipsis; full text in the tooltip; fallback (project / dash) muted
  const txt = t ? t.title : "\u2014", cls = t && t.custom ? "tt" : "tt dim";
  return '<div class="'+cls+'" title="'+esc(txt)+'">'+esc(txt)+'</div>';
}
function span(cls, txt){ return '<span class="'+cls+'">'+esc(txt)+'</span>'; }
function now(){ return Date.now()/1000; }

function confirmPending(st){
  const t = now(), bySid = {};
  for (const s of (st && st.sessions) || []) bySid[s.sid] = s;
  for (const sid of Object.keys(pending)){
    const p = pending[sid], s = bySid[sid];
    const done = !s || (p.kind==="stop" ? !["active","retry-wait"].includes(s.state) : s.state!=="stopped");
    if (done || t - p.t > PENDING_MAX_S) delete pending[sid];
  }
  if (gpending && st && !st.error){
    const anyStopped = (st.sessions||[]).some(s=>s.state==="stopped");
    const done = gpending.kind==="off" ? st.global_off===true : (st.global_off===false && !anyStopped);
    if (done || t - gpending.t > PENDING_MAX_S) gpending = null;
  }
}

function render(st){
  const hdr = document.getElementById("hdr"), rows = document.getElementById("rows");
  const t = now(), ctl = (st && st._dash) || {stop_files:[], off_file:false};
  if (!st || st.error || !Array.isArray(st.sessions)){
    hdr.innerHTML = "ka-proxy " + span("r","proxy not reporting") +
      span("dim","  (no valid status.json)") + (ctl.off_file? "  "+span("m","OFF flag set") : "");
    rows.innerHTML = ""; return;
  }
  const age = num(st.updated)? t - st.updated : Infinity;
  let state;
  if (age > STALE_S) state = span("y","stale")+span("dim"," (updated "+dur(age)+" ago)");
  else if (st.global_off) state = span("m","global OFF");
  else state = span("g","running");
  if (age > STALE_S && st.global_off) state += " " + span("m","global OFF");
  if (gpending) state += " " + span("y", gpending.kind==="off" ? "stopping all…" : "resuming all…");
  const td = st.today || {}, pu = num(td.ping_usd)? td.ping_usd:0, av = num(td.avoided_rewarm_usd)? td.avoided_rewarm_usd:0;
  const net = av - pu, stops = td.stops || {};
  const stopTxt = Object.keys(stops).map(k=>k+":"+stops[k]).join(" ");
  hdr.innerHTML = "ka-proxy " + state + span("dim","  pid "+(st.proxy_pid??"-")+"  updated "+hms(st.updated)) +
    "\ntoday  pings " + span("c", td.pings??0) + "  ping cost " + span("y",usd(pu)) +
    "  avoided rewarm " + span("g",usd(av)) + "  net " + span(net>=0?"g":"r",(net>=0?"+":"-")+usd(Math.abs(net))) +
    (stopTxt? span("dim","  stops "+stopTxt) : "");
  const stopSet = new Set(ctl.stop_files || []), titles = st._titles || {};
  let html = "";
  for (const s of st.sessions){
    if (!s || typeof s.sid!=="string") continue;
    const live = s.state==="active" || s.state==="retry-wait";
    const p = pending[s.sid];
    let btn;
    if (p) btn = '<button disabled>'+(p.kind==="stop"?"stopping…":"resuming…")+'</button>';
    else if (s.state==="stopped" || stopSet.has(s.sid)) btn = '<button class="res" data-a="resume" data-sid="'+esc(s.sid)+'">resume</button>';
    else btn = '<button class="stop" data-a="stop" data-sid="'+esc(s.sid)+'">stop</button>';
    let stTxt = span(COLOR[s.state]||"", s.state||"?");
    if (s.stop_reason && !live) stTxt += span("dim"," "+s.stop_reason);
    const next = live && num(s.next_ping_ts) ? dur(s.next_ping_ts - t)+span("dim"," "+hm(s.next_ping_ts)) : "-";
    const cap = num(s.cap_end_ts) ? dur(Math.max(0, s.cap_end_ts - t)) : "-";
    html += "<tr><td>"+span("b",s.sid.slice(0,8))+"</td><td>"+titleCell(titles[s.sid])+"</td><td>"+esc(s.model||"-")+'</td><td class="n">'+kt(s.prefix_tokens)+
      "</td><td>"+stTxt+'</td><td class="n">'+dur(num(s.last_real_ts)? t - s.last_real_ts : NaN)+
      '</td><td class="n">'+next+'</td><td class="n">'+(live?cap:span("dim",cap))+'</td><td class="n">'+esc(s.pings??0)+
      '</td><td class="n">'+usd(s.est_ping_usd)+'</td><td class="n">'+usd(s.est_rewarm_usd)+"</td><td>"+btn+"</td></tr>";
  }
  rows.innerHTML = html || '<tr><td colspan="12" class="dim">no tracked sessions</td></tr>';
}

async function refresh(){
  try {
    const r = await fetch("status.json", {cache:"no-store"});
    if (!r.ok) throw new Error("HTTP "+r.status);
    last = await r.json();
    document.getElementById("tick").textContent = "refreshed " + hms(now());
  } catch(e){
    document.getElementById("tick").innerHTML = span("r","dash unreachable: "+e.message);
  }
  confirmPending(last); render(last);
}

async function act(action, sid){
  const msg = document.getElementById("msg");
  try {
    const r = await fetch("action", {method:"POST", headers:{"X-KA-Dash":"1","Content-Type":"application/json"},
                                      body: JSON.stringify({action, sid})});
    const j = await r.json().catch(()=>({}));
    if (!r.ok || !j.ok) throw new Error(j.error || ("HTTP "+r.status));
    if (sid) pending[sid] = {kind: action, t: now()};
    else gpending = {kind: action==="stop_all" ? "off" : "on", t: now()};
    msg.innerHTML = span("dim", hms(now())+" "+action+(sid?" "+sid.slice(0,8):"")+" requested");
  } catch(e){ msg.innerHTML = span("r", action+" failed: "+e.message); }
  render(last); refresh();
}

document.getElementById("rows").addEventListener("click", ev=>{
  const b = ev.target.closest("button[data-a]"); if (b) act(b.dataset.a, b.dataset.sid);
});
document.getElementById("stopall").onclick = ()=>act("stop_all");
document.getElementById("resall").onclick = ()=>act("resume_all");
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""


def render_page(reporting):
    return PAGE.replace("%%INITIAL%%", "loading…" if reporting else "proxy not reporting")


def main(argv):
    sd = state_dir_from_env()
    try:
        port = int(os.environ.get("KA_DASH_PORT", DEFAULT_PORT))
    except ValueError:
        sys.exit("kadash: bad KA_DASH_PORT")
    if "--url" in argv:
        print(dash_url(port, load_or_create_token(sd)))
        return 0
    try:
        srv = make_server(sd, port)
    except OSError as e:
        sys.exit("kadash: cannot bind %s:%d: %s" % (BIND, port, e))
    print("kadash serving " + dash_url(srv.server_address[1], srv.token), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
