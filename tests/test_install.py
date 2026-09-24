"""install.py against a throwaway home (--home, KA_STATE_DIR in a temp dir, --no-services).

Never registers services, never touches the real ~/.claude. Run from the package root:
python3 -m unittest discover -s tests -v
"""
import json, os, shutil, socket, subprocess, sys, tempfile, unittest

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
POSIX = os.name != "nt"
sys.path.insert(0, os.path.join(PKG, "bin"))
import ka_ca  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def snapshot(root):
    """{relative path: (size, mtime_ns)} of every file/dir under root."""
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


@unittest.skipUnless(ka_ca.find_openssl(), "openssl not found")
class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ka-inst-")
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.home, ".claude", "skills", "keepwarm"))
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w") as f:
            json.dump({"env": {"KEEP": "1"}, "model": "opus", "hooks": {}}, f)
        with open(os.path.join(self.home, ".claude", "skills", "keepwarm", "SKILL.md"), "w") as f:
            f.write("---\nname: keepwarm\n---\nan older hand-made skill\n")
        self.env = dict({k: v for k, v in os.environ.items() if k not in ("KA_PORT", "KA_DASH_PORT")},
                        KA_STATE_DIR=self.state)
        self.port, self.dport = free_port(), free_port()     # installer probes ports: never the live 8787/8788
        self.assertNotIn(self.port, (8787, 8788))
        self.assertNotIn(self.dport, (8787, 8788))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_settings(self):
        with open(self.settings) as f:
            return f.read()

    def inst(self, *args):
        return subprocess.run([PY, os.path.join(PKG, "install.py"), "--home", self.home, "--no-services",
                               "--port", str(self.port), "--dash-port", str(self.dport), *args],
                              capture_output=True, text=True, env=self.env)

    def test_dry_run_changes_nothing(self):
        before = snapshot(self.tmp)
        for args in ((), ("--apply-settings",), ("--cli-only",), ("--uninstall",), ("--uninstall", "--purge")):
            r = self.inst("--dry-run", *args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("DRY-RUN", r.stdout)
            self.assertNotIn("\n+ ", r.stdout, args)                    # no executed step
        self.assertEqual(snapshot(self.tmp), before)
        r = self.inst("--dry-run", "--apply-settings")
        self.assertIn("would: back up existing skill", r.stdout)
        self.assertIn("would: back up %s and merge" % self.settings, r.stdout)
        self.assertIn('"HTTPS_PROXY": "http://ka:<token>@127.0.0.1:%d"' % self.port, r.stdout)

    def test_install_apply_uninstall_purge(self):
        r = self.inst("--apply-settings")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b = os.path.join(self.state, "bin")
        for n in ("ka_proxy.py", "kactl", "kadash.py", "ka_ca.py", "ka-shell-prefix.sh", "ka_off_hook.py",
                  "ka_service.py", "ka_report.py"):
            self.assertTrue(os.path.isfile(os.path.join(b, n)), n)
        if POSIX:
            with open(os.path.join(b, "kactl"), "rb") as f:
                self.assertEqual(f.readline(), b"#!" + PY.encode() + b"\n")   # interpreter pinned
            self.assertTrue(os.access(os.path.join(b, "kactl"), os.X_OK))
        else:
            self.assertTrue(os.path.isfile(os.path.join(b, "kactl.cmd")))
        for n in ("ca.pem", "ca.key", "leaf.pem", "leaf.key"):
            self.assertTrue(os.path.isfile(os.path.join(self.state, "ca", n)), n)
        with open(os.path.join(self.state, "proxy.token")) as f:
            tok = f.read().strip()
        # skill: rendered, old one backed up OUTSIDE ~/.claude/skills
        with open(os.path.join(self.home, ".claude", "skills", "keepwarm", "SKILL.md")) as f:
            skill = f.read()
        self.assertNotIn("{{", skill)
        self.assertIn(os.path.join(b, "kactl").replace("\\", "/") if not POSIX else os.path.join(b, "kactl"), skill)
        self.assertEqual(os.listdir(os.path.join(self.home, ".claude", "skills")), ["keepwarm"])
        (bak,) = os.listdir(os.path.join(self.state, "backup"))
        self.assertTrue(bak.startswith("keepwarm-skill-"))
        # settings: merged, other keys kept, backup written
        with open(self.settings) as f:
            d = json.load(f)
        env = d["env"]
        self.assertEqual((env["KEEP"], d["model"]), ("1", "opus"))
        self.assertEqual(env["HTTPS_PROXY"], "http://ka:%s@127.0.0.1:%d" % (tok, self.port))
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"],
                         ka_ca.env_path(os.path.join(os.path.realpath(os.path.join(self.state, "ca")), "ca.pem")))
        self.assertEqual(env["CLAUDE_CODE_SHELL_PREFIX"],
                         ka_ca.env_path(os.path.join(os.path.realpath(b), "ka-shell-prefix.sh")))
        self.assertIn("KA_STATE_DIR", env)                                  # non-default state dir travels along
        self.assertEqual(env["KA_PORT"], str(self.port))                    # ... and a non-default port
        self.assertTrue([n for n in os.listdir(os.path.join(self.home, ".claude")) if ".bak-ka-" in n])
        # installed kactl runs with the pinned interpreter
        k = subprocess.run([PY, os.path.join(b, "kactl"), "status", "all"], capture_output=True, text=True,
                           env=self.env)
        self.assertEqual(k.returncode, 0, k.stderr)
        self.assertIn("no status at", k.stdout)
        # --status never prints the proxy token
        r = self.inst("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("HTTPS_PROXY=ours", r.stdout)
        self.assertNotIn(tok, r.stdout + r.stderr)
        # re-install is idempotent: same token, settings unchanged
        before = self.read_settings()
        r = self.inst("--apply-settings")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read_settings(), before)
        # a foreign proxy value blocks the merge; nothing written
        d["env"]["HTTPS_PROXY"] = "http://proxy.corp:3128"
        with open(self.settings, "w") as f:
            json.dump(d, f)
        foreign = self.read_settings()
        r = self.inst("--apply-settings")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTPS_PROXY", r.stderr)
        self.assertEqual(self.read_settings(), foreign)
        d["env"]["HTTPS_PROXY"] = "http://ka:%s@127.0.0.1:%d" % (tok, self.port)
        with open(self.settings, "w") as f:
            json.dump(d, f)
        # uninstall: env keys (exact matches) gone, KEEP kept, bin + skill gone, state kept
        r = self.inst("--uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(tok, r.stdout + r.stderr)
        with open(self.settings) as f:
            d2 = json.load(f)
        self.assertEqual(d2["env"], {"KEEP": "1"})
        self.assertEqual((d2["model"], d2["hooks"]), ("opus", {}))
        self.assertFalse(os.path.exists(b))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "skills", "keepwarm")))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "ca", "ca.pem")))
        self.assertIn("Restart Claude Code", r.stdout)
        r = self.inst("--uninstall", "--purge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.state))

    def test_cli_only_writes_no_settings(self):
        before = self.read_settings()
        r = self.inst("--cli-only", "--apply-settings")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:%d" % self.port, r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.state, "ca")))
        self.assertEqual(self.read_settings(), before)

    def test_foreign_skill_left_on_uninstall(self):
        r = self.inst("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("was not installed by ka-keepalive", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".claude", "skills", "keepwarm", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
