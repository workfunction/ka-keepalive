#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: a prompt with a line starting "#ka-off" stops keepalive pings for that
session (touches $KA_STATE_DIR/stop/<session_id>). Silent; always exits 0. Any Python 3.

Hook command (the installer prints the exact line):  "<python>" "<STATE_DIR>/bin/ka_off_hook.py"
"""
import json, os, re, sys


def main():
    try:
        j = json.load(sys.stdin)
        sid, prompt = str(j.get("session_id") or ""), str(j.get("prompt") or "")
        if re.search(r"(?m)^[ \t]*#ka-off\b", prompt) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", sid) and sid not in (".", ".."):
            d = os.path.join(os.path.expanduser(os.environ.get("KA_STATE_DIR", "~/.claude/ka")), "stop")
            os.makedirs(d, mode=0o700, exist_ok=True)
            open(os.path.join(d, sid), "a").close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
