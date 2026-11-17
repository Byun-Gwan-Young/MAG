#!/usr/bin/env bash
# C-19-11 part 2 (DM-122) : send one bundle from the GitHub runner to the server's deploy-only account.
# Run by .github/workflows/deploy.yml. Never run by Claude Code.
#
#   ci-send.sh BUNDLE.tar.gz
#
# Reads DEPLOY_SSH_KEY (the deploy-only private key), DEPLOY_KNOWN_HOSTS (the server's host key line) and DEPLOY_HOST
# from the environment (GitHub secrets, by name). The key and the host key go to owner-only files under $RUNNER_TEMP
# that are removed when the script ends. The server's host key must match (StrictHostKeyChecking=yes : no "accept on
# first use"). The account can do one thing : the bundle on stdin is handed to /usr/local/sbin/guardrail-deploy (its
# forced command), which installs it and prints the result. That command's exit status is this script's : a failed
# health check or switch on the server fails the job, and the server keeps the version it had.
# Nothing here prints a secret : no `set -x`, no ssh -v.
set -euo pipefail

DEPLOY_USER=guardrail-deploy
die() { printf 'stop : %s\n' "$*" >&2; exit 1; }

BUNDLE=${1:-}
[[ -f "$BUNDLE" && -s "$BUNDLE" ]] || die "no bundle file"
[[ -n "${DEPLOY_SSH_KEY:-}" ]] || die "DEPLOY_SSH_KEY is not set"
[[ -n "${DEPLOY_KNOWN_HOSTS:-}" ]] || die "DEPLOY_KNOWN_HOSTS is not set"
[[ "${DEPLOY_HOST:-}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || die "DEPLOY_HOST is not set or not a host name or address"

work=$(umask 077 && mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/guardrail-send.XXXXXX")
trap 'rm -rf "$work"' EXIT
( umask 077
  printf '%s\n' "$DEPLOY_SSH_KEY" > "$work/key"
  printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$work/known_hosts" )

# The security group rule for this runner was added a moment ago : wait until the port answers (at most a minute).
reachable=
for _ in $(seq 1 12); do
    if (exec 3<>"/dev/tcp/$DEPLOY_HOST/22") 2>/dev/null; then reachable=yes; break; fi
    sleep 5
done
[[ -n "$reachable" ]] || die "the server's SSH port does not answer from this runner"

printf 'sending the bundle (%s bytes) ; the server installs it and switches only if the new version answers\n' \
    "$(wc -c < "$BUNDLE" | tr -d ' ')"
ssh -i "$work/key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$work/known_hosts" -o GlobalKnownHostsFile=/dev/null -o ConnectTimeout=20 \
    -o ServerAliveInterval=30 -o LogLevel=ERROR -T "$DEPLOY_USER@$DEPLOY_HOST" < "$BUNDLE"
