# ka status contract (shared by ka_proxy.py, kactl, kadash.py)

Written by ka_proxy.py every tick to `STATE_DIR/status.json` (mode 0600, atomic replace: write tmp + rename). Metadata only — never bodies, prompts, header values, or tokens.

```json
{
  "schema": 1,
  "updated": 1790210000.0,
  "proxy_pid": 28588,
  "global_off": false,
  "sessions": [
    {
      "sid": "5f2c8e1a-7b3d-4c9e-a1f0-2d6b8c4e9a07",
      "model": "claude-opus-5-5",
      "prefix_tokens": 443880,
      "last_real_ts": 1790204352.1,
      "last_refresh_ts": 1790204352.1,
      "next_ping_ts": 1790207652.1,
      "pings": 0,
      "cap_h": 5,
      "cap_end_ts": 1790222352.1,
      "state": "active",
      "stop_reason": null,
      "est_ping_usd": 0.115,
      "est_rewarm_usd": 3.67
    }
  ],
  "today": {"pings": 3, "ping_usd": 0.31, "avoided_rewarm_usd": 7.2, "stops": {"cap": 1, "flag": 0, "verify-fail": 1}}
}
```

- `state` ∈ `active` (tracked, will ping), `retry-wait`, `stopped` (manual flag), `capped`, `expired`, `verify-fail`, `over-max`. Non-active tracks stay listed until KA_INACTIVE_RETAIN_H (default 2) hours after they became non-active, then drop (with their in-memory metadata; `stop/` and `extend/` files are left as they are). A dropped session reappears on its next real request.
- `cap_end_ts` = the effective cap end: max(idle start + `cap_h` hours, `extend_until_ts`).
- `extend_until_ts` (OPTIONAL, active tracks only) = the user extension deadline from `STATE_DIR/extend/<sid>`, bounded by now + KA_EXTEND_MAX_H (default 24). Absent when no extension is in effect; readers must not require it.
- `est_rewarm_usd` = (prefix_tokens − KA_SHARED_PREFIX) × (write1h − read) / 1e6 with KA_SHARED_PREFIX default 30000.
- Control = files, never HTTP to the proxy: `STATE_DIR/stop/<sid>` (per session), `STATE_DIR/off` (global), `STATE_DIR/extend/<sid>` (absolute epoch-seconds deadline as a plain-text float, 0600; written by `kactl extend`, expired files removed by the proxy). Writers: `kactl`, `kadash.py`, the `#ka-off` hook. The proxy applies them within one tick. An extension keeps an active track pinging past its dynamic cap (even cap 0); it never revives a stopped/capped/expired track — it applies from that session's next real request.
