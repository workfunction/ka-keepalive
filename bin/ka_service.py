#!/usr/bin/env python3
"""ka_service.py proxy|dash [KEY=VALUE ...]: run ka_proxy.py or kadash.py as a background service.

Used by the Windows Task Scheduler entries (started with pythonw.exe, so there is no console window and
sys.stdout / sys.stderr are None): output goes to STATE_DIR/<name>.out and <name>.err, like the
launchd / systemd definitions on macOS and Linux. KEY=VALUE pairs are set in the environment first
(the installer passes KA_PORT / KA_DASH_PORT / KA_STATE_DIR here when they are not the defaults).
"""
import os, runpy, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = {"proxy": "ka_proxy.py", "dash": "kadash.py"}


def main(argv):
    if not argv or argv[0] not in SCRIPTS:
        sys.exit("usage: ka_service.py proxy|dash [KEY=VALUE ...]")
    name = argv[0]
    for kv in argv[1:]:
        k, sep, v = kv.partition("=")
        if not sep or not k.startswith("KA_"):
            sys.exit(f"ka_service.py: bad setting {kv!r} (only KA_*=value)")
        os.environ[k] = v
    sd = os.path.expanduser(os.environ.get("KA_STATE_DIR", "~/.claude/ka"))
    os.makedirs(sd, mode=0o700, exist_ok=True)
    sys.stdout = open(os.path.join(sd, name + ".out"), "a", buffering=1, encoding="utf-8")
    sys.stderr = open(os.path.join(sd, name + ".err"), "a", buffering=1, encoding="utf-8")
    script = os.path.join(HERE, SCRIPTS[name])
    sys.argv = [script]
    sys.path.insert(0, HERE)
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
