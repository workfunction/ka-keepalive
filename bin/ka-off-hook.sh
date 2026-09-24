#!/bin/bash
# Claude Code UserPromptSubmit hook (shell form): a prompt containing "#ka-off" stops keepalive pings for
# that session. Delegates to ka_off_hook.py next to this file. Interpreter: $KA_PYTHON, else python3, else
# python. Silent; always exits 0. The installer prints a direct "<python> ka_off_hook.py" form too.
here="$(cd "$(dirname "$0")" && pwd -P)"
py="${KA_PYTHON:-$(command -v python3 || command -v python)}"
[ -n "$py" ] && "$py" "$here/ka_off_hook.py" >/dev/null 2>&1
exit 0
