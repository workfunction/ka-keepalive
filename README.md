# ka-keepalive: prompt-cache keepalive for Claude Code

A small local proxy that keeps each Claude Code session's **1-hour prompt cache** warm while you are away, so the next message you send does not re-write the whole prefix at the cache-write price.

繁體中文版:[README.zh-TW.md](README.zh-TW.md)

## What it does, and why

- Claude Code caches the conversation prefix for 1 hour. After 60 idle minutes the cache is gone. The next request writes the whole prefix again, at 2x the input price. On a 400k-token session that costs several dollars.
- `ka_proxy.py` sits between Claude Code and `api.anthropic.com` on `127.0.0.1:8787`. It passes every request through unchanged and keeps the last **main-loop** request of each session **in memory only**.
- After 55 minutes of idle time it replays that request with `max_tokens: 0` and `stream: false`. The API reads the cache (which refreshes the 1-hour TTL) and generates nothing. **No turn is added to your transcript**, and the model does not run.
- Each ping is checked: HTTP 200 and a cache read of at least 95% of the expected prefix. A transient failure (429 with retry-after, 529 overloaded) gets one retry. Anything else stops keepalive for that session.
- How long a session is kept warm is decided per session by a **dynamic cap**; see below. You can stop, resume or extend it by hand.
- `STATE_DIR/ka.log` holds **metadata only**: no bodies, prompts, outputs, header values or tokens. The proxy binds 127.0.0.1 only.

Two ways in:

| Mode | Who uses it | How |
|---|---|---|
| **full** (default) | CLI **and** the desktop app | `HTTPS_PROXY` in `~/.claude/settings.json`. The proxy terminates TLS for `api.anthropic.com` only, using a local CA that is name-constrained to that host and trusted only through `NODE_EXTRA_CA_CERTS`. Every other CONNECT target is a blind tunnel, never decrypted. CONNECT requires `Proxy-Authorization` with a random token. |
| **cli-only** | terminal sessions you opt in | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`. No CA, no settings change. |

## Requirements

- Claude Code (CLI and/or desktop app).
- **Python 3.10 or newer**, stdlib only. 3.10 is the floor because the relay's stream-timeout detection relies on `socket.timeout` being `TimeoutError` (3.10+), and the dashboard uses `str.removeprefix` (3.9+). The installer looks for a newer Python if you start it with an older one.
- **openssl** (full mode only, to make the local CA): macOS ships one (`/usr/bin/openssl`), Linux package managers have it, and on Windows the installer uses the copy inside **Git for Windows** (which Claude Code on Windows already needs). Set `KA_OPENSSL=<path>` to choose one.
- macOS: launchd (built in). Linux: `systemd --user` (otherwise start it yourself; the installer prints the commands). Windows: Task Scheduler (built in).

## Install

Unzip the package anywhere, open a terminal in it, and try a dry run first. It prints every step and changes nothing:

```bash
python3 install.py --dry-run
```

Then install:

| OS | Command |
|---|---|
| macOS | `python3 install.py` |
| Linux | `python3 install.py` |
| Windows (PowerShell or cmd) | `py -3 install.py` |

What the installer does:

1. Copies `bin/` to `~/.claude/ka/bin/`. On macOS/Linux the scripts' `#!` line is pinned to the interpreter you ran the installer with. On Windows it also writes `kactl.cmd`.
2. Installs the `keepwarm` skill to `~/.claude/skills/keepwarm/`, with the `kactl` command for this machine filled in. An existing skill is moved to `~/.claude/ka/backup/` first; not under `skills/`, because a copy there would load as a second skill.
3. Full mode: creates `~/.claude/ka/proxy.token` and the CA under `~/.claude/ka/ca/`.
4. Registers auto-start for the proxy and the dashboard:
   - macOS: LaunchAgents `com.ka-keepalive.proxy` / `com.ka-keepalive.dash` (`~/Library/LaunchAgents/`), RunAtLoad + KeepAlive.
   - Linux: `systemd --user` units `ka-keepalive-proxy.service` / `ka-keepalive-dash.service`, `Restart=always`. They start when you log in. For them to run without a login session, use `loginctl enable-linger`.
   - Windows: Task Scheduler tasks `ka-keepalive-proxy` / `ka-keepalive-dash`: run at logon, `pythonw.exe` (no console window), restart on failure.
5. Prints the `settings.json` env block. It writes that block only if you pass `--apply-settings`: a timestamped backup `settings.json.bak-ka-<time>` comes first, then a merge that keeps every other key. If a key already holds a value that is not ours (a corporate proxy, say), it refuses and changes nothing.

Options: `--cli-only`, `--no-dashboard`, `--no-services` (files only), `--port N`, `--dash-port N`, `--python PATH`, `--apply-settings`, `--dry-run`, `--status`, `--uninstall [--purge]`.

## The settings.json env lines (full mode)

The installer prints these with your real values. `CLAUDE_CODE_SHELL_PREFIX` keeps Bash-tool, hook and MCP child processes off the proxy: it unsets each variable only when its value exactly matches ours.

```json
"env": {
  "HTTPS_PROXY": "http://ka:<32-hex token from ~/.claude/ka/proxy.token>@127.0.0.1:8787",
  "NODE_EXTRA_CA_CERTS": "<home>/.claude/ka/ca/ca.pem",
  "NO_PROXY": "localhost,127.0.0.1",
  "CLAUDE_CODE_SHELL_PREFIX": "<home>/.claude/ka/bin/ka-shell-prefix.sh"
}
```

On Windows both paths are written as drive-letter paths with forward slashes (`C:/.../.claude/ka/...` under your `%USERPROFILE%`). Node accepts that form for `NODE_EXTRA_CA_CERTS`, and Git Bash, which runs Claude Code's Bash tool on Windows, accepts it for `CLAUDE_CODE_SHELL_PREFIX`. Backslashes would be eaten by the shell.

> **CRUCIAL: added env applies to RUNNING sessions mid-session, but removing it does NOT revert them.**
> Once `HTTPS_PROXY` is in settings.json, sessions that are already open start going through the proxy. Deleting the lines later does not take them back out: every session that saw the env keeps using the proxy until that session (or the desktop app) **restarts**. While the proxy is down, those sessions cannot reach the API. Do the smoke test before you add the env to settings.json.

If a lowercase `https_proxy` is set where Claude Code starts, Claude Code reads it before `HTTPS_PROXY`, and the proxy is bypassed.

## First-run smoke test

Try one CLI session with the env set **for that process only**, before touching settings.json.

1. Check that the proxy is up:
   ```bash
   python3 install.py --status        # service loaded/running, port 8787 listening
   ```
2. In a new terminal, start one session with the values the installer printed:
   ```bash
   # macOS / Linux
   HTTPS_PROXY='http://ka:<token>@127.0.0.1:8787' NODE_EXTRA_CA_CERTS=~/.claude/ka/ca/ca.pem \
     NO_PROXY=localhost,127.0.0.1 claude
   ```
   ```powershell
   # Windows PowerShell (the variables live only in this window; close it afterwards)
   $env:HTTPS_PROXY='http://ka:<token>@127.0.0.1:8787'; $env:NODE_EXTRA_CA_CERTS="$HOME/.claude/ka/ca/ca.pem"; $env:NO_PROXY='localhost,127.0.0.1'; claude
   ```
   CLI-only mode: `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`.
3. Send one message. It should answer normally.
4. From another terminal, run `python3 ~/.claude/ka/bin/kactl status all`. The session should be listed as `active` with a prefix size and a next-ping time.
5. Check that nothing was rejected: `grep -c proxy-auth-fail ~/.claude/ka/ka.log` should print `0`. That event is the proxy's 407.
6. If all of that passes, add the env block (`python3 install.py --apply-settings`) and restart Claude Code and the desktop app.

## Daily use

- **In a session:** the `keepwarm` skill: `/keepwarm` (on), `/keepwarm extend <hours>` (up to 24), `/keepwarm stop`, `/keepwarm status`. Chinese triggers work too (保溫, 延長保溫, 停止保溫). When the proxy is not running, the skill falls back to a 55-minute background tick in the session.
- **From a terminal:** `kactl` (`python3 ~/.claude/ka/bin/kactl ...`; on Windows `%USERPROFILE%\.claude\ka\bin\kactl.cmd ...`):
  - `kactl status all`: every tracked session
  - `kactl stop <sid-prefix>|all` / `kactl resume <sid-prefix>|all`
  - `kactl extend <sid-prefix> <hours>`: keep pinging past the dynamic cap, 24 h max
  - `kactl off` / `kactl on`: global switch
- **Dashboard:** a local page with the same controls. Its URL (it includes a random token) is the last `kadash serving ...` line in `~/.claude/ka/dash.out`. You can also get it from `python3 install.py --status` or `python3 ~/.claude/ka/bin/kadash.py --url`.
- **Daily report:** `python3 ~/.claude/ka/bin/ka_report.py [--day YYYY-MM-DD]` shows pings, ping cost, and the potential avoided re-warm cost.
- **Per-prompt off switch (optional):** a prompt with a line that starts with `#ka-off` stops pings for that session once you add the `UserPromptSubmit` hook the installer prints: `"<python>" "<home>/.claude/ka/bin/ka_off_hook.py"`. A `#ka-off` in the middle of a sentence does not count, so talking about the marker is safe.

Control goes through files in `~/.claude/ka/` (`stop/<sid>`, `off`, `extend/<sid>`), never HTTP. The proxy applies them within one 30-second tick. A stopped, capped or expired session starts again on its next real request.

## How the dynamic cap works

For each session the proxy estimates two costs. **Save** is what re-warming the prefix would cost: prefix tokens minus about 30k shared tokens, times (1h-write price minus read price). **Ping** is what one keepalive costs: a cache read of the prefix plus about 3.8k uncached tokens. It then picks the number of idle hours H, from 1 to 12, that maximises the expected net saving. For that it uses an embedded return-time curve: the share of idle stretches of an hour or more that ended within h hours, measured on 471 real idle stretches. A return in hour h saves "save minus the (h-1) pings already paid". No return by H costs H pings. If no H comes out positive, the cap is 0 and the session is not kept warm. The hard ceiling is 12 h (`KA_CAP_H_FABLE`, `KA_CAP_H_OPUS`). Models without a price entry get a flat 4 h (`KA_CAP_H_DEFAULT`). `kactl extend` overrides the cap for up to 24 h. The cap clock restarts each time you send a real message.

## Stop / uninstall

- Pause everything: `kactl off` (sessions stay routed through the proxy; they just stop getting pings).
- Stop the proxy service: macOS `launchctl bootout gui/$UID/com.ka-keepalive.proxy`; Linux `systemctl --user stop ka-keepalive-proxy`; Windows `schtasks /End /TN ka-keepalive-proxy`. **Do this only after you remove the env and restart your sessions.** See the warning above.
- Uninstall: `python3 install.py --uninstall --dry-run`, then without `--dry-run`. It stops and unregisters both services and removes our env keys from settings.json, and only the keys whose value exactly matches ours; a backup comes first. It also removes `~/.claude/ka/bin` and the skill (only if we installed it). It keeps `~/.claude/ka` (log, CA, tokens) unless you add `--purge`. **Then restart every Claude Code session and the desktop app.**

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **407** / `proxy-auth-fail` in `ka.log` | The token in `HTTPS_PROXY` does not match `~/.claude/ka/proxy.token`. It may have been regenerated, or it was mistyped. Re-run `python3 install.py` to print the current block, fix settings.json, and restart the sessions. If the desktop app does not send the credentials from the URL, use CLI-only mode for now. |
| **Port in use** (service keeps restarting, `Address already in use` in `proxy.err`) | Another program, or an older copy of this proxy under a different service name, holds 8787. Find it (`lsof -iTCP:8787 -sTCP:LISTEN`; Windows `netstat -ano \| findstr 8787`), stop it, or install with `--port N`. With a non-default port, the env block also carries `KA_PORT`. |
| **CA not trusted** (`self-signed certificate in certificate chain`, `unable to get local issuer certificate`) | `NODE_EXTRA_CA_CERTS` is missing, wrong, or points to an older CA. Check the path and restart the session (Node reads it only at start). If a lowercase `https_proxy` is set, that proxy is used instead. Regenerate with `python3 ~/.claude/ka/bin/ka_ca.py --force`; a running proxy picks up the new leaf by itself. |
| **429 usage limit** | Pings are ordinary API requests, so they can run into rate or usage limits too. A 429 with `retry-after` gets one retry. Otherwise keepalive stops for that session (`verify-fail` in `kactl status all`) and your next real message starts it again. Lower the load with `kactl stop` or `kactl off`. |
| **Proxy down: sessions cannot reach the API** | Every session that saw `HTTPS_PROXY` (or `ANTHROPIC_BASE_URL`) now fails its requests. **Fastest fix: start the proxy again.** macOS `launchctl kickstart -k gui/$UID/com.ka-keepalive.proxy`; Linux `systemctl --user restart ka-keepalive-proxy`; Windows `schtasks /Run /TN ka-keepalive-proxy`; any OS, by hand: `python3 ~/.claude/ka/bin/ka_proxy.py`. To leave the proxy for good: remove the env keys (`install.py --uninstall`, or edit settings.json), then **restart** each session. `claude --resume` in a fresh terminal gets the conversation back. |
| Session never shows up in `kactl status all` | Only main-loop requests (with tools) are tracked, and only after one real message goes through the proxy. Check `ka.log` for `"event": "real"` lines. |

Logs: `~/.claude/ka/ka.log` (JSONL, metadata only), plus `proxy.out`/`proxy.err` and `dash.out`/`dash.err` next to it.

## Known limits / not verified

- **Windows is untested on real Windows.** The paths, Task Scheduler registration (XML with a logon trigger, `pythonw.exe`, restart on failure), the `kactl.cmd` shim, the Git Bash handling of `CLAUDE_CODE_SHELL_PREFIX`, and the Git-for-Windows openssl lookup are written from documentation and a static review only. Try `--dry-run` first. If `CLAUDE_CODE_SHELL_PREFIX` breaks the Bash tool there, remove that one key: child processes then inherit the proxy settings, which still work while the proxy runs.
- **Linux** `systemd --user` units are untested on a real Linux machine.
- **Windows file permissions:** the `0600`/`0700` modes that protect `proxy.token`, `dash.token` and `ca/ca.key` on macOS/Linux do not exist on Windows. There, protection rests on your user-profile ACL. Use a python.org Python rather than the Microsoft Store one: Task Scheduler may not start the Store's app-execution alias (`--python` picks another).
- **Desktop app + `HTTPS_PROXY`** (including whether it sends the `Proxy-Authorization` credentials from the URL) has been checked only on macOS.
- **Cost numbers are list-price equivalents.** They come from a small built-in price table (`PRICES` in `ka_proxy.py`). On a subscription plan they show relative value, not money actually billed.
- **Terms:** replaying your own requests to keep a cache warm on an OAuth/subscription login is your own decision. Check the terms that apply to your account.
- One ping per idle hour per session, at most 8 sessions (`KA_MAX_SESSIONS`), at most 3 pings in flight. The stored request lives only in the proxy's memory, so a proxy restart forgets it until the session's next real message.

## Package contents

```
install.py               installer (stdlib only)
bin/ka_proxy.py          proxy + keepalive controller
bin/kactl                CLI control (kactl.cmd is generated on Windows)
bin/kadash.py            dashboard (127.0.0.1:8788, token URL)
bin/ka_report.py         daily summary of ka.log
bin/ka_ca.py             local CA + leaf (openssl)
bin/ka-shell-prefix.sh   CLAUDE_CODE_SHELL_PREFIX wrapper (bash; Git Bash on Windows)
bin/ka_off_hook.py       #ka-off UserPromptSubmit hook (ka-off-hook.sh = shell form)
bin/ka_service.py        Windows service launcher (pythonw; output to STATE_DIR/*.out|err)
skill/keepwarm/SKILL.md  in-session skill template ({{KACTL}} filled in at install)
STATUS-CONTRACT.md       status.json / control-file contract shared by proxy, kactl, dashboard
tests/                   python3 -m unittest discover -s tests  (temp dirs, free ports only)
```

Tuning (environment of the proxy service): `KA_PORT`, `KA_STATE_DIR`, `KA_PING_AFTER_S` (3300), `KA_TTL_S` (3600), `KA_TICK_S` (30), `KA_MAX_SESSIONS` (8), `KA_CAP_H_OPUS` / `KA_CAP_H_FABLE` (12), `KA_CAP_H_DEFAULT` (4), `KA_EXTEND_MAX_H` (24). See `load_config()` in `ka_proxy.py`.
