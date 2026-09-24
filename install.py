#!/usr/bin/env python3
"""ka-keepalive installer (stdlib only; macOS, Linux, Windows).

  python3 install.py                   install: bin -> ~/.claude/ka/bin, keepwarm skill, local CA,
                                       auto-start services (proxy + dashboard); prints the settings env block
  python3 install.py --apply-settings  ... and merge that env block into ~/.claude/settings.json (backup first)
  python3 install.py --cli-only        no CA, no HTTPS_PROXY: terminal sessions opt in with ANTHROPIC_BASE_URL
  python3 install.py --no-dashboard    do not register the dashboard service
  python3 install.py --no-services     copy files only; start ka_proxy.py / kadash.py yourself
  python3 install.py --uninstall [--purge]   stop + unregister services, remove our env keys (exact match),
                                       remove bin + skill; --purge also removes ~/.claude/ka (logs, CA, tokens)
  python3 install.py --status          what is installed and running
  add --dry-run to print every action without changing anything

Windows: run it with `py -3 install.py ...` (or the python.exe you want the services to use).
Requires Python 3.10+ (the installer looks for one if started with an older interpreter).
"""
import argparse, json, os, platform, re, shlex, shutil, socket, subprocess, sys, time

MIN_PY = (3, 10)
PKG = os.path.dirname(os.path.abspath(__file__))
PKG_BIN = os.path.join(PKG, "bin")
PKG_SKILL = os.path.join(PKG, "skill", "keepwarm", "SKILL.md")
SKILL_MARKER = "<!-- ka-keepalive: installed by install.py"
DEFAULT_PORT, DEFAULT_DASH_PORT = 8787, 8788
SCRIPTS = {"proxy": "ka_proxy.py", "dash": "kadash.py"}
LABELS = {"proxy": "com.ka-keepalive.proxy", "dash": "com.ka-keepalive.dash"}          # macOS launchd
UNITS = {"proxy": "ka-keepalive-proxy.service", "dash": "ka-keepalive-dash.service"}  # Linux systemd --user
TASKS = {"proxy": "ka-keepalive-proxy", "dash": "ka-keepalive-dash"}                  # Windows Task Scheduler
NO_PROXY = "localhost,127.0.0.1"
OLD_TOKEN_URL = re.compile(r"http://ka:[0-9a-f]{32}@127\.0\.0\.1:\d{1,5}")


# ---------------------------------------------------------------- python / OS detection
def py_version(exe):
    try:
        r = subprocess.run([exe, "-c", "import sys; print(sys.executable); print('%d %d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=20)
        path, ver = r.stdout.strip().splitlines()[-2:]
        return path, tuple(int(x) for x in ver.split())
    except Exception:
        return None, None


def find_python():
    """(path, version) of a Python >= MIN_PY other than the running one, or (None, None)."""
    names = ["python3.%d" % m for m in range(20, MIN_PY[1] - 1, -1)] + ["python3", "python"]
    cands = [shutil.which(n) for n in names]
    if os.name == "nt" and shutil.which("py"):
        cands.insert(0, "py -3")
    for c in cands:
        if not c:
            continue
        path, ver = _py_launcher() if c == "py -3" else py_version(c)
        if ver and ver >= MIN_PY:
            return path, ver
    return None, None


def _py_launcher():
    try:
        r = subprocess.run(["py", "-3", "-c", "import sys; print(sys.executable); print('%d %d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=20)
        path, ver = r.stdout.strip().splitlines()[-2:]
        return path, tuple(int(x) for x in ver.split())
    except Exception:
        return None, None


def service_python(override=None):
    """Interpreter the services, skill and hook will use: --python, else this one (a venv's base interpreter,
    since everything here is stdlib and a venv may be deleted later)."""
    if override:
        return os.path.abspath(override)
    exe = sys.executable
    if sys.prefix != sys.base_prefix:
        exe = getattr(sys, "_base_executable", None) or exe
    return exe


def os_kind():
    s = platform.system()
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(s, s.lower())


# ---------------------------------------------------------------- context + action helpers
class Ctx:
    def __init__(self, a):
        self.dry = a.dry_run
        self.kind = os_kind()
        self.home = os.path.abspath(a.home) if a.home else os.path.expanduser("~")
        self.claude = os.path.join(self.home, ".claude")
        self.default_state = os.path.join(self.claude, "ka")
        env_sd = os.environ.get("KA_STATE_DIR")
        self.state = os.path.abspath(os.path.expanduser(env_sd)) if env_sd else self.default_state
        self.bin = os.path.join(self.state, "bin")
        self.skill_dir = os.path.join(self.claude, "skills", "keepwarm")
        self.settings = os.path.join(self.claude, "settings.json")
        self.python = service_python(a.python)
        self.pythonw = self.python
        if self.kind == "windows":
            w = os.path.join(os.path.dirname(self.python), "pythonw.exe")
            self.pythonw = w if os.path.isfile(w) else self.python
        port_env = os.environ.get("KA_PORT")
        self.port = a.port or (int(port_env) if port_env and port_env.isdigit() else DEFAULT_PORT)
        dp_env = os.environ.get("KA_DASH_PORT")
        self.dash_port = a.dash_port or (int(dp_env) if dp_env and dp_env.isdigit() else DEFAULT_DASH_PORT)
        self.ts = time.strftime("%Y%m%d-%H%M%S")

    def custom_state(self):
        return os.path.normcase(os.path.normpath(self.state)) != os.path.normcase(os.path.normpath(self.default_state))

    def service_env(self, name):
        """KA_* settings a service needs when they differ from the built-in defaults."""
        env = {}
        if self.custom_state():
            env["KA_STATE_DIR"] = self.state
        if name == "proxy" and self.port != DEFAULT_PORT:
            env["KA_PORT"] = str(self.port)
        if name == "dash" and self.dash_port != DEFAULT_DASH_PORT:
            env["KA_DASH_PORT"] = str(self.dash_port)
        return env


def act(ctx, desc, fn=None, *args, **kw):
    """Run fn(*args) unless dry-run; always print the step."""
    if ctx.dry:
        print("[dry-run] would: " + desc)
        return None
    print("+ " + desc)
    return fn(*args, **kw) if fn else None


def run(cmd, check=False, quiet=False):
    """subprocess.run with captured output; returns CompletedProcess (rc 127 if the program is missing)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "not found: " + cmd[0])
    if check and r.returncode != 0:
        raise RuntimeError("%s failed (rc %d): %s" % (" ".join(cmd), r.returncode, (r.stderr or r.stdout).strip()[:300]))
    if not quiet and r.returncode != 0 and r.stderr.strip():
        print("  ! " + r.stderr.strip().splitlines()[-1][:300])
    return r


def port_in_use(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def env_path(p):
    """Windows: drive-letter path with forward slashes (Node and Git Bash both accept it)."""
    return p.replace("\\", "/") if os.name == "nt" else p


def cmd_quote(p):
    """One path as it is typed into a shell: POSIX shlex quoting; Windows double quotes when needed."""
    if os.name == "nt":
        p = env_path(p)
        return '"%s"' % p if (" " in p or "(" in p) else p
    return shlex.quote(p)


def load_pkg_module(name):
    sys.dont_write_bytecode = True   # never leave __pycache__ in the package
    if PKG_BIN not in sys.path:
        sys.path.insert(0, PKG_BIN)
    return __import__(name)


# ---------------------------------------------------------------- settings.json
def read_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError(path + ": top level is not a JSON object")
    return d


def write_settings(path, d):
    tmp = path + ".ka-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_token(ctx):
    """proxy.token content (never printed), or '' if absent/malformed."""
    try:
        with open(os.path.join(ctx.state, "proxy.token")) as f:
            tok = f.read().strip()
    except OSError:
        return ""
    return tok if re.fullmatch(r"[0-9a-f]{32}", tok) else ""


def path_forms(p):
    """Every spelling of path p we may have written into settings (as given, physical, Windows forms)."""
    out = {p, os.path.realpath(p)}
    return out | {env_path(x) for x in out} | {x.replace("/", "\\") for x in out if os.name == "nt"}


def env_block(ctx, token, cli_only):
    """Env values for ~/.claude/settings.json (full mode) or the ANTHROPIC_BASE_URL line (CLI mode)."""
    if cli_only:
        return {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % ctx.port}
    ca = os.path.join(os.path.realpath(os.path.join(ctx.state, "ca")) if os.path.isdir(ctx.state)
                      else os.path.join(ctx.state, "ca"), "ca.pem")
    binp = os.path.realpath(ctx.bin) if os.path.isdir(ctx.bin) else ctx.bin
    env = {"HTTPS_PROXY": "http://ka:%s@127.0.0.1:%d" % (token or "<token>", ctx.port),
           "NODE_EXTRA_CA_CERTS": env_path(ca),
           "NO_PROXY": NO_PROXY,
           "CLAUDE_CODE_SHELL_PREFIX": env_path(os.path.join(binp, "ka-shell-prefix.sh"))}
    if ctx.custom_state():   # the shell prefix, kactl and the hook must find the same STATE_DIR
        env["KA_STATE_DIR"] = env_path(ctx.state)
    if ctx.port != DEFAULT_PORT:   # the shell prefix rebuilds the proxy URL from KA_PORT
        env["KA_PORT"] = str(ctx.port)
    return env


def our_env_keys(env, ctx, token, port):
    """Keys of settings env whose value is EXACTLY what this installer (or ka_ca.py) writes."""
    ours = []
    if env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:%d" % port:
        ours.append("ANTHROPIC_BASE_URL")
    if token and env.get("HTTPS_PROXY") == "http://ka:%s@127.0.0.1:%d" % (token, port):
        ours.append("HTTPS_PROXY")
    if env.get("NODE_EXTRA_CA_CERTS") in path_forms(os.path.join(ctx.state, "ca", "ca.pem")):
        ours.append("NODE_EXTRA_CA_CERTS")
    if env.get("CLAUDE_CODE_SHELL_PREFIX") in path_forms(os.path.join(ctx.bin, "ka-shell-prefix.sh")):
        ours.append("CLAUDE_CODE_SHELL_PREFIX")
    if "HTTPS_PROXY" in ours and env.get("NO_PROXY") == NO_PROXY:
        ours.append("NO_PROXY")
    if env.get("KA_STATE_DIR") in path_forms(ctx.state):
        ours.append("KA_STATE_DIR")
    if env.get("KA_PORT") == str(port) and ("HTTPS_PROXY" in ours or "ANTHROPIC_BASE_URL" in ours):
        ours.append("KA_PORT")
    return ours


def apply_settings(ctx, block):
    """Merge block into settings.json env; refuses (writes nothing) if a key holds someone else's value."""
    d = read_settings(ctx.settings)
    env = d.get("env") if isinstance(d.get("env"), dict) else None
    if d.get("env") is not None and env is None:
        raise RuntimeError(ctx.settings + ': "env" is not an object; edit it by hand')
    env = env or {}
    ours_now = set(our_env_keys(env, ctx, read_token(ctx), ctx.port))
    conflicts = [k for k, v in block.items() if k in env and env[k] != v and k not in ours_now
                 and not (k == "HTTPS_PROXY" and OLD_TOKEN_URL.fullmatch(str(env[k])))]
    if conflicts:
        raise RuntimeError("settings.json already sets %s to other values (a corporate proxy/CA?). Not changed; "
                           "merge by hand." % ", ".join(conflicts))
    for low in ("https_proxy", "http_proxy"):
        if low in env:
            print("  ! settings env has lowercase %s: Claude Code reads it before HTTPS_PROXY" % low)
    if os.path.exists(ctx.settings):
        shutil.copy2(ctx.settings, ctx.settings + ".bak-ka-" + ctx.ts)
        print("  backup: " + ctx.settings + ".bak-ka-" + ctx.ts)
    else:
        os.makedirs(ctx.claude, exist_ok=True)
    env.update(block)
    d["env"] = env
    write_settings(ctx.settings, d)


# ---------------------------------------------------------------- services
def svc_paths(ctx, name):
    return {"script": os.path.join(ctx.bin, SCRIPTS[name]),
            "out": os.path.join(ctx.state, name + ".out"), "err": os.path.join(ctx.state, name + ".err")}


# macOS launchd
def plist_path(ctx, name):
    return os.path.join(ctx.home, "Library", "LaunchAgents", LABELS[name] + ".plist")


def plist_bytes(ctx, name):
    import plistlib
    p = svc_paths(ctx, name)
    d = {"Label": LABELS[name], "ProgramArguments": [ctx.python, p["script"]], "RunAtLoad": True,
         "KeepAlive": True, "StandardOutPath": p["out"], "StandardErrorPath": p["err"]}
    if ctx.service_env(name):
        d["EnvironmentVariables"] = ctx.service_env(name)
    return plistlib.dumps(d)


def launchd_loaded(label):
    return run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], quiet=True).returncode == 0


def launchd_install(ctx, name):
    label, path = LABELS[name], plist_path(ctx, name)
    if launchd_loaded(label):
        act(ctx, "launchctl bootout gui/<uid>/%s (reload)" % label, run,
            ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)])
    act(ctx, "write %s" % path, _write_bytes, path, plist_bytes(ctx, name), 0o644)

    def boot():
        for _ in range(10):   # bootout finishes asynchronously; bootstrap can fail for a moment
            if run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), path], quiet=True).returncode == 0:
                return
            time.sleep(0.5)
        run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), path], check=True)
    act(ctx, "launchctl bootstrap gui/<uid> %s" % path, boot)


def launchd_uninstall(ctx, name):
    label, path = LABELS[name], plist_path(ctx, name)
    if launchd_loaded(label):
        act(ctx, "launchctl bootout gui/<uid>/%s" % label, run, ["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)])
    else:
        print("  launchd: %s not loaded" % label)
    if os.path.exists(path):
        act(ctx, "remove %s" % path, os.remove, path)


def launchd_status(ctx, name):
    r = run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABELS[name])], quiet=True)
    if r.returncode != 0:
        return "not loaded" + (" (plist present)" if os.path.exists(plist_path(ctx, name)) else "")
    state = re.search(r"^\s*state = (\S+)", r.stdout, re.M)
    pid = re.search(r"^\s*pid = (\d+)", r.stdout, re.M)
    return "loaded, state %s%s" % (state.group(1) if state else "?", ", pid " + pid.group(1) if pid else "")


# Linux systemd --user
def unit_dir(ctx):
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if (xdg and not ctx.home_overridden) else os.path.join(ctx.home, ".config")
    return os.path.join(base, "systemd", "user")


def sd_quote(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def unit_text(ctx, name):
    p = svc_paths(ctx, name)
    lines = ["[Unit]", "Description=ka-keepalive %s (Claude Code prompt-cache keepalive)" % name, "",
             "[Service]", "ExecStart=%s %s" % (sd_quote(ctx.python), sd_quote(p["script"])),
             "Restart=always", "RestartSec=5"]
    for k, v in ctx.service_env(name).items():
        lines.append("Environment=%s" % sd_quote("%s=%s" % (k, v)))
    lines += ["StandardOutput=append:%s" % p["out"].replace("%", "%%"),
              "StandardError=append:%s" % p["err"].replace("%", "%%"), "",
              "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def systemd_ok():
    return shutil.which("systemctl") and run(["systemctl", "--user", "show-environment"], quiet=True).returncode == 0


def systemd_install(ctx, name):
    path = os.path.join(unit_dir(ctx), UNITS[name])
    act(ctx, "write %s" % path, _write_bytes, path, unit_text(ctx, name).encode(), 0o644)
    act(ctx, "systemctl --user daemon-reload", run, ["systemctl", "--user", "daemon-reload"], True)
    act(ctx, "systemctl --user enable %s" % UNITS[name], run, ["systemctl", "--user", "enable", UNITS[name]], True)
    act(ctx, "systemctl --user restart %s" % UNITS[name], run, ["systemctl", "--user", "restart", UNITS[name]], True)


def systemd_uninstall(ctx, name):
    path = os.path.join(unit_dir(ctx), UNITS[name])
    if shutil.which("systemctl"):
        act(ctx, "systemctl --user disable --now %s" % UNITS[name], run,
            ["systemctl", "--user", "disable", "--now", UNITS[name]])
    if os.path.exists(path):
        act(ctx, "remove %s" % path, os.remove, path)
        act(ctx, "systemctl --user daemon-reload", run, ["systemctl", "--user", "daemon-reload"])
    else:
        print("  systemd: %s absent" % path)


def systemd_status(ctx, name):
    if not shutil.which("systemctl"):
        return "systemctl not found"
    r = run(["systemctl", "--user", "is-active", UNITS[name]], quiet=True)
    return (r.stdout.strip() or "unknown") + ("" if os.path.exists(os.path.join(unit_dir(ctx), UNITS[name]))
                                              else " (no unit file)")


# Windows Task Scheduler
def task_xml(ctx, name):
    from xml.sax.saxutils import escape
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    args = '"%s" %s' % (os.path.join(ctx.bin, "ka_service.py"), name)
    for k, v in ctx.service_env(name).items():
        args += ' "%s=%s"' % (k, v)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>ka-keepalive %(name)s (Claude Code prompt-cache keepalive)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>%(user)s</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>%(user)s</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>%(cmd)s</Command><Arguments>%(args)s</Arguments><WorkingDirectory>%(wd)s</WorkingDirectory></Exec></Actions>
</Task>
""" % {"name": name, "user": escape(user), "cmd": escape(ctx.pythonw), "args": escape(args), "wd": escape(ctx.bin)}


def schtasks_install(ctx, name):
    tn, xml = TASKS[name], os.path.join(ctx.state, TASKS[name] + ".xml")
    if run(["schtasks", "/Query", "/TN", tn], quiet=True).returncode == 0:
        act(ctx, "schtasks /End /TN %s (reload)" % tn, run, ["schtasks", "/End", "/TN", tn])
    act(ctx, "write %s (task definition, UTF-16)" % xml, _write_bytes, xml, task_xml(ctx, name).encode("utf-16"), 0o600)
    act(ctx, "schtasks /Create /TN %s /XML %s /F  (at logon, %s, no console)" % (tn, xml, os.path.basename(ctx.pythonw)),
        run, ["schtasks", "/Create", "/TN", tn, "/XML", xml, "/F"], True)
    act(ctx, "schtasks /Run /TN %s" % tn, run, ["schtasks", "/Run", "/TN", tn], True)


def schtasks_uninstall(ctx, name):
    tn = TASKS[name]
    if run(["schtasks", "/Query", "/TN", tn], quiet=True).returncode == 0:
        act(ctx, "schtasks /End /TN %s" % tn, run, ["schtasks", "/End", "/TN", tn])
        act(ctx, "schtasks /Delete /TN %s /F" % tn, run, ["schtasks", "/Delete", "/TN", tn, "/F"])
    else:
        print("  Task Scheduler: %s not registered" % tn)
    xml = os.path.join(ctx.state, tn + ".xml")
    if os.path.exists(xml):
        act(ctx, "remove %s" % xml, os.remove, xml)


def schtasks_status(ctx, name):
    r = run(["schtasks", "/Query", "/TN", TASKS[name], "/FO", "LIST"], quiet=True)
    if r.returncode != 0:
        return "not registered"
    st = re.search(r"^Status:\s*(.+)$", r.stdout, re.M)
    return "registered" + (", " + st.group(1).strip() if st else "")


SERVICE_OPS = {"macos": (launchd_install, launchd_uninstall, launchd_status),
               "linux": (systemd_install, systemd_uninstall, systemd_status),
               "windows": (schtasks_install, schtasks_uninstall, schtasks_status)}


# ---------------------------------------------------------------- file helpers
def _write_bytes(path, data, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def copy_bin(ctx):
    os.makedirs(ctx.bin, mode=0o700, exist_ok=True)
    for n in sorted(os.listdir(PKG_BIN)):
        src = os.path.join(PKG_BIN, n)
        if not os.path.isfile(src) or n.endswith(".pyc"):
            continue
        with open(src, "rb") as f:
            data = f.read()
        if os.name != "nt" and data.startswith(b"#!/usr/bin/env python3\n") and not re.search(r"\s", ctx.python):
            data = b"#!" + ctx.python.encode() + data[len(b"#!/usr/bin/env python3"):]   # pin the interpreter
        _write_bytes(os.path.join(ctx.bin, n), data, 0o755 if data.startswith(b"#!") else 0o644)
    if ctx.kind == "windows":   # kactl.cmd: `kactl.cmd status` from cmd/PowerShell
        shim = '@echo off\r\n"%s" "%%~dp0kactl" %%*\r\n' % ctx.python
        _write_bytes(os.path.join(ctx.bin, "kactl.cmd"), shim.encode(), 0o755)


def kactl_command(ctx):
    return "%s %s" % (cmd_quote(ctx.python), cmd_quote(os.path.join(ctx.bin, "kactl")))


def render_skill(ctx):
    with open(PKG_SKILL, encoding="utf-8") as f:
        s = f.read()
    return s.replace("{{KACTL}}", kactl_command(ctx)).replace("{{STATE_DIR}}", env_path(ctx.state))


def install_skill(ctx, text):
    path = os.path.join(ctx.skill_dir, "SKILL.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cur = f.read()
        if cur == text:
            print("  skill: %s already current" % path)
            return
        # back up OUTSIDE ~/.claude/skills: a copy there would load as a second "keepwarm" skill
        bak = os.path.join(ctx.state, "backup", "keepwarm-skill-" + ctx.ts)
        act(ctx, "back up existing skill %s -> %s" % (ctx.skill_dir, bak), _move_dir, ctx.skill_dir, bak)
    act(ctx, "write %s" % path, _write_bytes, path, text.encode("utf-8"), 0o644)


def _move_dir(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def safe_to_remove_state(ctx):
    bad = {os.path.normpath(p) for p in (os.path.abspath(os.sep), ctx.home, ctx.claude)}
    return os.path.normpath(ctx.state) not in bad and len(os.path.normpath(ctx.state)) > 3


def hook_snippet(ctx):
    cmd = "%s %s" % (cmd_quote(ctx.python), cmd_quote(os.path.join(ctx.bin, "ka_off_hook.py")))
    return json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]}}, indent=2)


def dash_url(ctx):
    try:
        with open(os.path.join(ctx.state, "dash.out"), encoding="utf-8", errors="replace") as f:
            lines = [l for l in f.read().splitlines() if l.startswith("kadash serving ")]
        return lines[-1].split(" ", 2)[2] if lines else None
    except OSError:
        return None


# ---------------------------------------------------------------- commands
def cmd_install(ctx, a):
    mode = "cli-only" if a.cli_only else "full (HTTPS_PROXY + local CA)"
    print("ka-keepalive install  os=%s  python=%s (%s)  mode=%s%s" % (
        ctx.kind, ctx.python, "%d.%d" % sys.version_info[:2], mode, "  DRY-RUN" if ctx.dry else ""))
    print("  state dir %s   bin %s   skill %s" % (ctx.state, ctx.bin, ctx.skill_dir))
    if ctx.kind not in SERVICE_OPS and not a.no_services:
        raise RuntimeError("unsupported OS %r for auto-start; rerun with --no-services" % ctx.kind)
    if a.python:
        _, ver = py_version(ctx.python)
        if not ver or ver < MIN_PY:
            raise RuntimeError("--python %s is not a Python %d.%d+ interpreter" % ((ctx.python,) + MIN_PY))
    ka_ca = load_pkg_module("ka_ca")
    if not a.cli_only:
        ossl = ka_ca.find_openssl()
        if not ossl:
            raise RuntimeError("openssl not found (needed for the local CA). macOS/Linux: install it with your "
                               "package manager; Windows: use the one in Git for Windows (set KA_OPENSSL to its "
                               "path) -- or install with --cli-only")
        print("  openssl %s" % ossl)
    for port, what in ((ctx.port, "proxy"), (ctx.dash_port, "dashboard")):
        if port_in_use(port):
            print("  ! port %d (%s) is already in use: an earlier ka install, or another program. If the service "
                  "fails to start, free the port or rerun with --%s <other>" % (port, what,
                                                                              "port" if what == "proxy" else "dash-port"))

    print("\n1. files")
    if not os.path.isdir(ctx.state):
        act(ctx, "create %s (0700)" % ctx.state, os.makedirs, ctx.state, 0o700)
    act(ctx, "copy %s -> %s (interpreter pinned to %s)" % (PKG_BIN, ctx.bin, ctx.python), copy_bin, ctx)
    install_skill(ctx, render_skill(ctx))

    token = None
    if not a.cli_only:
        print("\n2. local CA (name-constrained to api.anthropic.com) + proxy token")
        if ctx.dry:
            print("[dry-run] would: create %s/proxy.token (if absent) and %s/ca/{ca,leaf}.{pem,key} (if absent or "
                  "expiring)" % (ctx.state, ctx.state))
        else:
            token, _ = ka_ca.ensure_ca(ctx.state, log=lambda m: print("+ " + m))
    else:
        print("\n2. local CA: skipped (--cli-only)")

    print("\n3. auto-start")
    names = ["proxy"] + ([] if a.no_dashboard else ["dash"])
    if a.no_services:
        print("  skipped (--no-services). Start manually:")
        for n in names:
            print("    %s %s" % (cmd_quote(ctx.python), cmd_quote(svc_paths(ctx, n)["script"])))
    elif ctx.kind == "linux" and not systemd_ok():
        print("  ! no systemd user session (systemctl --user). Start manually, e.g. from your login profile:")
        for n in names:
            p = svc_paths(ctx, n)
            print("    nohup %s %s >> %s 2>> %s &" % (cmd_quote(ctx.python), cmd_quote(p["script"]),
                                                     cmd_quote(p["out"]), cmd_quote(p["err"])))
    else:
        for n in names:
            SERVICE_OPS[ctx.kind][0](ctx, n)
        if a.no_dashboard:
            SERVICE_OPS[ctx.kind][1](ctx, "dash")   # drop a dashboard from an earlier install

    print("\n4. Claude Code settings")
    block = env_block(ctx, token, a.cli_only)
    if a.cli_only:
        print("  CLI only: nothing goes into settings.json. Start a terminal session through the proxy with:")
        if ctx.kind == "windows":
            print("    PowerShell:  $env:ANTHROPIC_BASE_URL='%s'; claude" % block["ANTHROPIC_BASE_URL"])
            print("    Git Bash:    ANTHROPIC_BASE_URL=%s claude" % block["ANTHROPIC_BASE_URL"])
        else:
            print("    ANTHROPIC_BASE_URL=%s claude" % block["ANTHROPIC_BASE_URL"])
        if a.apply_settings:
            print("  (--apply-settings ignored with --cli-only)")
    else:
        print('  env block for %s ("env" object):' % ctx.settings)
        for k, v in block.items():
            print("    %s: %s," % (json.dumps(k), json.dumps(v)))
        if a.apply_settings:
            if ctx.dry:
                print("[dry-run] would: back up %s and merge the keys above into its env (other keys kept)" % ctx.settings)
            else:
                print("+ merge into %s" % ctx.settings)
                apply_settings(ctx, block)
        else:
            print("  Not written (rerun with --apply-settings, or paste it yourself).")
        print("  WARNING: added env reaches RUNNING sessions mid-session, but removing it does NOT revert them:\n"
              "  a session keeps using the proxy until it restarts. Do the smoke test below before adding it.")
    print("\n  Optional #ka-off hook (merge into settings.json by hand):")
    print("    " + hook_snippet(ctx).replace("\n", "\n    "))

    print("\n5. next")
    print("  smoke test: open ONE terminal and run a CLI session with the env set for that process only, e.g.")
    if a.cli_only:
        print("    ANTHROPIC_BASE_URL=http://127.0.0.1:%d claude -p 'say ok'" % ctx.port)
    else:
        print("    HTTPS_PROXY=... NODE_EXTRA_CA_CERTS=... NO_PROXY=%s claude -p 'say ok'   (values above)" % NO_PROXY)
    print("  then: %s status all     (expect the session listed; no 407 in %s)" % (
        kactl_command(ctx), os.path.join(ctx.state, "ka.log")))
    if not ctx.dry and not a.no_services and "dash" in names:
        for _ in range(20):
            if dash_url(ctx):
                break
            time.sleep(0.25)
        u = dash_url(ctx)
        print("  dashboard: " + (u or "not up yet -- `install.py --status` shows the URL once it is"))
    return 0


def cmd_uninstall(ctx, a):
    print("ka-keepalive uninstall  os=%s  state dir %s%s" % (ctx.kind, ctx.state, "  DRY-RUN" if ctx.dry else ""))
    print("\n1. services")
    if a.no_services:
        print("  skipped (--no-services)")
    elif ctx.kind in SERVICE_OPS:
        for n in ("proxy", "dash"):
            SERVICE_OPS[ctx.kind][1](ctx, n)
    else:
        print("  unsupported OS %r: stop ka_proxy.py / kadash.py yourself" % ctx.kind)

    print("\n2. settings.json env (only values that exactly match ours)")
    removed_any = False
    if os.path.isfile(ctx.settings):
        d = read_settings(ctx.settings)
        env = d.get("env") if isinstance(d.get("env"), dict) else {}
        pe = os.environ.get("KA_PORT") or str(env.get("KA_PORT") or "")
        port = a.port or (int(pe) if pe.isdigit() else DEFAULT_PORT)
        keys = our_env_keys(env, ctx, read_token(ctx), port)
        if keys:
            removed_any = True
            bak = ctx.settings + ".bak-ka-" + ctx.ts
            act(ctx, "cp -p %s %s" % (ctx.settings, bak), shutil.copy2, ctx.settings, bak)
            for k in keys:
                act(ctx, "remove env.%s from %s (rest preserved)" % (k, ctx.settings))
            if not ctx.dry:
                for k in keys:
                    env.pop(k, None)
                write_settings(ctx.settings, d)
        else:
            print("  settings: no ka proxy/CA env keys in %s" % ctx.settings)
        if re.search(r"ka_off_hook|ka-off-hook", json.dumps(d.get("hooks") or {})):
            print("  ! settings.json hooks still reference the #ka-off hook: remove that entry by hand")
    else:
        print("  settings: %s absent" % ctx.settings)

    print("\n3. files")
    if os.path.isdir(ctx.bin):
        act(ctx, "remove directory %s" % ctx.bin, shutil.rmtree, ctx.bin)
    else:
        print("  bin: %s absent" % ctx.bin)
    sk = os.path.join(ctx.skill_dir, "SKILL.md")
    if os.path.isfile(sk):
        with open(sk, encoding="utf-8") as f:
            ours = SKILL_MARKER in f.read()
        if ours:
            act(ctx, "remove directory %s" % ctx.skill_dir, shutil.rmtree, ctx.skill_dir)
        else:
            print("  skill: %s was not installed by ka-keepalive; left in place" % ctx.skill_dir)
    else:
        print("  skill: %s absent" % ctx.skill_dir)
    if a.purge:
        if not safe_to_remove_state(ctx):
            raise RuntimeError("refusing to remove STATE_DIR %r" % ctx.state)
        if os.path.isdir(ctx.state):
            act(ctx, "remove directory %s (logs, CA, tokens, flags)" % ctx.state, shutil.rmtree, ctx.state)
        else:
            print("  state: %s absent" % ctx.state)
    else:
        print("  keep %s (ka.log, CA, tokens, skill backups); --purge removes it" % ctx.state)
    if removed_any:
        print("\nRestart Claude Code (every CLI session and the desktop app): a session started while the env was "
              "set keeps routing through the proxy, which is now stopped, until it restarts.")
    return 0


def cmd_status(ctx, a):
    print("ka-keepalive status  os=%s  python=%s (%d.%d)" % ((ctx.kind, ctx.python) + sys.version_info[:2]))
    print("  bin:     %s" % (ctx.bin if os.path.isfile(os.path.join(ctx.bin, "ka_proxy.py")) else "not installed"))
    sk = os.path.join(ctx.skill_dir, "SKILL.md")
    if os.path.isfile(sk):
        with open(sk, encoding="utf-8") as f:
            print("  skill:   %s%s" % (sk, "" if SKILL_MARKER in f.read() else " (not ours)"))
    else:
        print("  skill:   not installed")
    if ctx.kind in SERVICE_OPS:
        for n in ("proxy", "dash"):
            print("  service %-5s %s" % (n, SERVICE_OPS[ctx.kind][2](ctx, n)))
    print("  port %d (proxy): %s   port %d (dashboard): %s" % (
        ctx.port, "listening" if port_in_use(ctx.port) else "closed",
        ctx.dash_port, "listening" if port_in_use(ctx.dash_port) else "closed"))
    try:
        with open(os.path.join(ctx.state, "status.json")) as f:
            st = json.load(f)
        age = time.time() - float(st.get("updated") or 0)
        print("  status.json: updated %.0fs ago%s, %d session(s), global %s" % (
            age, " (STALE)" if age > 120 else "", len(st.get("sessions") or []),
            "OFF" if st.get("global_off") else "on"))
    except (OSError, ValueError):
        print("  status.json: none (proxy never ran with this STATE_DIR)")
    try:
        env = read_settings(ctx.settings).get("env") or {}
    except (OSError, ValueError) as e:
        env = {}
        print("  settings: unreadable (%s)" % type(e).__name__)
    ours = set(our_env_keys(env, ctx, read_token(ctx), ctx.port))
    keys = ["HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "NO_PROXY", "CLAUDE_CODE_SHELL_PREFIX", "ANTHROPIC_BASE_URL"]
    print("  settings env: " + ", ".join("%s=%s" % (k, "ours" if k in ours else "OTHER" if k in env else "-")
                                         for k in keys))
    u = dash_url(ctx)
    print("  dashboard: %s" % (u or "no URL yet (dash.out has no 'kadash serving' line)"))
    return 0


def parse(argv):
    ap = argparse.ArgumentParser(description="ka-keepalive installer", epilog="See README.md.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    ap.add_argument("--apply-settings", action="store_true", help="merge the env block into ~/.claude/settings.json")
    ap.add_argument("--cli-only", action="store_true", help="no CA / HTTPS_PROXY; use ANTHROPIC_BASE_URL per shell")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--no-services", action="store_true", help="do not register auto-start")
    ap.add_argument("--purge", action="store_true", help="with --uninstall: also remove the state dir")
    ap.add_argument("--python", help="interpreter for services/skill/hook (default: this one)")
    ap.add_argument("--port", type=int, help="proxy port (default 8787)")
    ap.add_argument("--dash-port", type=int, help="dashboard port (default 8788)")
    ap.add_argument("--home", help=argparse.SUPPRESS)   # tests: act on a throwaway home dir
    return ap.parse_args(argv)


def main(argv):
    a = parse(argv)
    if sys.version_info < MIN_PY:
        path, ver = find_python()
        if not path:
            print("install.py: needs Python %d.%d+ (this is %d.%d) and none was found on PATH. Install a current "
                  "Python (python.org, Homebrew, your package manager) and rerun with it." % (MIN_PY + sys.version_info[:2]),
                  file=sys.stderr)
            return 1
        print("install.py: Python %d.%d is too old; re-running with %s (%d.%d)" % (sys.version_info[:2] + (path,) + ver), flush=True)
        return subprocess.call([path, os.path.abspath(__file__)] + argv)
    if a.purge and not a.uninstall:
        print("install.py: --purge only goes with --uninstall", file=sys.stderr)
        return 2
    ctx = Ctx(a)
    ctx.home_overridden = bool(a.home)
    try:
        if a.status:
            return cmd_status(ctx, a)
        if a.uninstall:
            return cmd_uninstall(ctx, a)
        return cmd_install(ctx, a)
    except (RuntimeError, ValueError, OSError) as e:
        print("install.py: error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
