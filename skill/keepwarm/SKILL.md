---
name: keepwarm
description: Prompt-cache keepalive control for THIS Claude Code session — `/keepwarm` (on), `/keepwarm extend <hours>`, `/keepwarm stop`, `/keepwarm status`. Uses the local ka-proxy when it is running (no transcript turns); otherwise falls back to a 55-minute background tick. Use when the user asks to keep this session warm, extend or stop keepalive (保溫, 延長保溫, 停止保溫, keepwarm, #ka-off), and whenever a background task described `keepwarm tick` completes.
---

# keepwarm

A 1-hour prompt cache dies after 60 idle minutes; the next message re-writes the whole prefix at 2× input price. The local **ka-proxy** keeps each active session's cache warm by itself (a `max_tokens: 0` replay, no turn in this transcript), with a per-session cap it computes from the prefix size. This skill is the in-session remote for it; the **tick** mode below is only the fallback when the proxy is not running.

`KACTL` = `{{KACTL}}` (rendered by the ka-keepalive installer for this machine). It targets this session by default through `$CLAUDE_CODE_SESSION_ID`.

## 1. Pick the mode — one Bash call

Run `$KACTL status`.
- Exit 0 → **proxy mode** (section 2). Its one-line output is this session's state.
- Output says "not tracked yet" → proxy mode; the proxy picks this session up on its next request. Continue with section 2.
- Command fails for any other reason (proxy down, stale status, file missing) → **tick mode** (section 3), and say `ka-proxy not running — using tick mode` in the reply.

## 2. Proxy mode — each command is one Bash call and one reply line

| User says | Run | Reply with |
|---|---|---|
| `/keepwarm`, `/keepwarm on`, 開啟保溫 | `$KACTL resume; $KACTL status` | the status line |
| `/keepwarm extend <h>`, 延長保溫 <h>h (h ≤ 24) | `$KACTL extend <h>; $KACTL status` | the status line (cap end now includes the extension) |
| `/keepwarm stop`, 停止保溫, `#ka-off` | `$KACTL stop` | `keepwarm off (proxy)` |
| `/keepwarm status` | `$KACTL status` | the status line |

Proxy mode never arms a tick and never writes per-hour turns — the proxy does the heartbeat. `kactl status all` shows every session; the dashboard (`{{STATE_DIR}}/dash.out` holds its URL) has the same controls.

## 3. Tick mode (fallback) — a background tick wakes this session every 55 min

The tick decides in the shell, so a tick turn is exactly two requests: re-arm, then one line.

**Arm** (start, and every re-arm) with Bash, `run_in_background: true`, description `keepwarm tick n=<N> start=<S> cap=<C>`:

```
A=$(date +%s); sleep 3300; T=$(date +%s); [ $((T-A)) -le 3480 ] && [ $((T-<S>)) -lt $((<C>*3600)) ]
```

`<S>` = epoch start (on first arm run `date +%s` inside the same command: replace `<S>` with `$A`), `<C>` = cap hours (argument, else 8 for a Fable session, 4 otherwise), `<N>` = tick number. Exit 0 = still worth keeping; exit 1 = slept past the TTL or cap reached.

**Tick turn** — the completion notice carries the description and the exit code:
1. Exit code non-zero → reply `keepwarm done (cap reached or slept past TTL)`; do not re-arm.
2. The user sent a message since the previous tick → they are active; use `start=<now>` for the re-arm (cap restarts).
3. Otherwise re-arm with `n=<N+1>` and the same start and cap, and reply `ka <N+1>`.

A tick turn runs that one Bash call and writes that one line — no file reads, no summary. `/keepwarm extend <h>` in tick mode re-arms with `cap = hours since start + h`; `/keepwarm stop` stops the running `keepwarm tick` task (TaskStop) and replies `keepwarm off`.

<!-- ka-keepalive: installed by install.py; `install.py --uninstall` removes this skill. -->
