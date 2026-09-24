#!/usr/bin/env python3
"""ka_ca.py [--force]: make the local CA + api.anthropic.com leaf for ka_proxy's CONNECT front door.

The CA is name-constrained (critical) to DNS:api.anthropic.com with pathlen:0, so it cannot vouch for any
other host. Trust it ONLY via Claude Code's NODE_EXTRA_CA_CERTS; never add it to the system trust store.
Idempotent: existing, not-expiring material is kept; --force regenerates all. Does NOT edit
~/.claude/settings.json -- prints the env lines to add.

openssl: $KA_OPENSSL, else `openssl` on PATH, else the copy bundled with Git for Windows.
Env: KA_STATE_DIR (default ~/.claude/ka), KA_PORT (default 8787). Stdlib only; Python 3.10+.
"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ka_proxy  # noqa: E402  (shared token format + permission helpers)

HOST = "api.anthropic.com"
NO_PROXY = "localhost,127.0.0.1"
CA_FILES = ("ca.key", "ca.pem", "leaf.key", "leaf.pem")

CNF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = ka-proxy local CA (api.anthropic.com only)
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
nameConstraints = critical, permitted;DNS:{host}
subjectKeyIdentifier = hash
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:{host}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""
# separate CSR config: LibreSSL ignores -subj under prompt=no, and a leaf whose subject equals the CA's
# would look self-issued
LEAF_CNF = "[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = {host}\n"


def git_openssl_candidates():
    """openssl.exe shipped with Git for Windows (Claude Code on Windows requires Git for Windows)."""
    roots = []
    git = shutil.which("git")
    if git:   # <root>\cmd\git.exe or <root>\bin\git.exe or <root>\mingw64\bin\git.exe
        d = os.path.dirname(os.path.realpath(git))
        roots += [os.path.dirname(d), os.path.dirname(os.path.dirname(d))]
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(var):
            roots.append(os.path.join(os.environ[var], "Git"))
    if os.environ.get("LOCALAPPDATA"):
        roots.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Git"))
    out = []
    for r in roots:
        for sub in (("mingw64", "bin"), ("usr", "bin"), ("mingw32", "bin")):
            out.append(os.path.join(r, *sub, "openssl.exe"))
    return out


def find_openssl():
    """Path of a usable openssl binary, or None."""
    cand = [os.environ.get("KA_OPENSSL") or None, shutil.which("openssl")]
    if os.name == "nt":
        cand += git_openssl_candidates()
    else:
        cand += ["/usr/bin/openssl"]
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def state_dir_from_env():
    return os.path.expanduser(os.environ.get("KA_STATE_DIR", "~/.claude/ka"))


def env_path(p):
    """A path in the form Claude Code's env values need: on Windows forward slashes (drive-letter form, C:/...), which
    both Node (NODE_EXTRA_CA_CERTS) and Git Bash (CLAUDE_CODE_SHELL_PREFIX) accept; elsewhere unchanged."""
    return p.replace("\\", "/") if os.name == "nt" else p


def settings_env(state_dir, port, bin_dir, token):
    """The four env values for ~/.claude/settings.json (HTTPS_PROXY front door), as a dict."""
    ca_dir = os.path.realpath(os.path.join(state_dir, "ca"))
    return {"HTTPS_PROXY": f"http://ka:{token}@127.0.0.1:{port}",
            "NODE_EXTRA_CA_CERTS": env_path(os.path.join(ca_dir, "ca.pem")),
            "NO_PROXY": NO_PROXY,
            "CLAUDE_CODE_SHELL_PREFIX": env_path(os.path.join(os.path.realpath(bin_dir), "ka-shell-prefix.sh"))}


def _run(openssl, *args, cwd):
    r = subprocess.run([openssl, *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed (rc {r.returncode}): {r.stderr.strip()[:400]}")
    return r


def leaf_ok(openssl, ca_dir):
    """True if all CA files exist and leaf.pem is valid for at least 30 more days."""
    if not all(os.path.isfile(os.path.join(ca_dir, f)) and os.path.getsize(os.path.join(ca_dir, f)) > 0
               for f in CA_FILES if f != "ca.key"):
        return False
    r = subprocess.run([openssl, "x509", "-checkend", "2592000", "-noout", "-in", os.path.join(ca_dir, "leaf.pem")],
                       capture_output=True)
    return r.returncode == 0


def generate(openssl, ca_dir):
    """Fresh CA + leaf into ca_dir (built in a temp dir inside it, then moved in)."""
    tmp = tempfile.mkdtemp(prefix=".gen.", dir=ca_dir)
    try:
        with open(os.path.join(tmp, "x.cnf"), "w") as f:
            f.write(CNF.format(host=HOST))
        with open(os.path.join(tmp, "leaf.cnf"), "w") as f:
            f.write(LEAF_CNF.format(host=HOST))
        run = lambda *a: _run(openssl, *a, cwd=tmp)
        run("genrsa", "-out", "ca.key", "2048")
        run("req", "-x509", "-new", "-key", "ca.key", "-sha256", "-days", "3650", "-config", "x.cnf",
            "-extensions", "v3_ca", "-out", "ca.pem")
        run("genrsa", "-out", "leaf.key", "2048")
        run("req", "-new", "-key", "leaf.key", "-config", "leaf.cnf", "-out", "leaf.csr")
        run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-set_serial", "0x" + os.urandom(8).hex(), "-days", "825", "-sha256", "-extfile", "x.cnf",
            "-extensions", "v3_leaf", "-out", "leaf.pem")
        run("verify", "-CAfile", "ca.pem", "leaf.pem")
        for f in CA_FILES:
            ka_proxy.set_mode(os.path.join(tmp, f), 0o600)
        for f in CA_FILES:
            ka_proxy.replace_retry(os.path.join(tmp, f), os.path.join(ca_dir, f))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_ca(state_dir, force=False, log=print):
    """Create token + CA/leaf if needed. Returns (token, ca_dir). Raises RuntimeError without openssl."""
    openssl = find_openssl()
    if not openssl:
        raise RuntimeError("openssl not found: install it (macOS/Linux: package manager; Windows: it ships with "
                           "Git for Windows, e.g. C:\\Program Files\\Git\\mingw64\\bin\\openssl.exe) or set KA_OPENSSL")
    old = os.umask(0o077) if os.name != "nt" else None
    try:
        os.makedirs(os.path.join(state_dir, "ca"), mode=0o700, exist_ok=True)
        ka_proxy.set_mode(os.path.join(state_dir, "ca"), 0o700)
        ca_dir = os.path.realpath(os.path.join(state_dir, "ca"))
        token = ka_proxy.load_or_create_token(state_dir)   # created once, never overwritten
        if not force and leaf_ok(openssl, ca_dir):
            log(f"CA + leaf already present in {ca_dir} (--force to regenerate)")
        else:
            generate(openssl, ca_dir)
            log(f"generated CA + leaf in {ca_dir} (a running ka_proxy picks up the new leaf on the next CONNECT)")
    finally:
        if old is not None:
            os.umask(old)
    return token, ca_dir


def main(argv):
    force = argv[:1] == ["--force"]
    if os.name != "nt" and "https_proxy" in os.environ:   # value not printed: it may carry credentials
        print("ka_ca.py: warning: lowercase https_proxy is set in this environment; Claude Code reads it before "
              "HTTPS_PROXY, so it would bypass the ka proxy. Unset it where Claude Code starts, or set it to the "
              "same URL as HTTPS_PROXY below.", file=sys.stderr)
    state_dir = state_dir_from_env()
    port = os.environ.get("KA_PORT", "8787")
    try:
        token, _ = ensure_ca(state_dir, force)
    except (RuntimeError, ValueError, OSError) as e:
        print(f"ka_ca.py: {e}", file=sys.stderr)
        return 1
    print('Add to the "env" block of ~/.claude/settings.json (not done by this script), then restart Claude Code:')
    for k, v in settings_env(state_dir, port, HERE, token).items():
        print(f"  {json.dumps(k)}: {json.dumps(v)},")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
