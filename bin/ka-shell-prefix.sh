#!/bin/bash
# CLAUDE_CODE_SHELL_PREFIX wrapper: Claude Code passes each Bash-tool / hook / stdio-MCP command as ONE
# shell-quoted string in $1. Child processes must not inherit the ka proxy: each variable is unset
# only when its value EXACTLY equals what ka_ca.py / install.py told the user to set (our proxy URL, our ca.pem
# path, our NO_PROXY value); anything else (e.g. a corporate proxy) is left untouched. Then the
# command runs with the same stdio, and its exit code is returned.
ka_sd="${KA_STATE_DIR:-$HOME/.claude/ka}"
ka_tok=""
IFS= read -r ka_tok 2>/dev/null < "$ka_sd/proxy.token"
ka_url="http://ka:${ka_tok}@127.0.0.1:${KA_PORT:-8787}"   # exactly what ka_ca.py prints
ka_ca_logical="$ka_sd/ca/ca.pem"
ka_ca_physical="$(cd "$ka_sd/ca" 2>/dev/null && pwd -P)/ca.pem"
ka_ca_mixed="" ka_ca_win=""
if command -v cygpath >/dev/null 2>&1; then   # Git Bash on Windows: settings hold C:/... (or C:\...) paths
  ka_ca_mixed="$(cygpath -m "$ka_ca_physical" 2>/dev/null)"
  ka_ca_win="$(cygpath -w "$ka_ca_physical" 2>/dev/null)"
fi
if [ -n "$ka_tok" ]; then
  for v in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy; do
    [ "${!v-}" = "$ka_url" ] && unset "$v"
  done
fi
if [ -n "${NODE_EXTRA_CA_CERTS-}" ] && { [ "$NODE_EXTRA_CA_CERTS" = "$ka_ca_logical" ] || [ "$NODE_EXTRA_CA_CERTS" = "$ka_ca_physical" ] \
     || { [ -n "$ka_ca_mixed" ] && [ "$NODE_EXTRA_CA_CERTS" = "$ka_ca_mixed" ]; } \
     || { [ -n "$ka_ca_win" ] && [ "$NODE_EXTRA_CA_CERTS" = "$ka_ca_win" ]; }; }; then
  unset NODE_EXTRA_CA_CERTS
fi
for v in NO_PROXY no_proxy; do
  [ "${!v-}" = "localhost,127.0.0.1" ] && unset "$v"
done
unset ka_sd ka_tok ka_url ka_ca_logical ka_ca_physical ka_ca_mixed ka_ca_win v
if [ $# -eq 1 ]; then
  exec /bin/bash -c "$1"
fi
exec "$@"
