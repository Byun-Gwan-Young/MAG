#!/usr/bin/env bash
# C-19-11 part 3 (DM-122) : make the deploy-only account. Runs ON THE SERVER as root, once (safe to repeat) ; the PM
# sends and runs it with deploy/aws/ci-setup.sh server. Never run by Claude Code.
#
#   sudo bash deploy-user-setup.sh PUBLIC_KEY_FILE GUARDRAIL_DEPLOY_SCRIPT
#
# - User guardrail-deploy (no password, not in any sudo/admin group).
# - /usr/local/sbin/guardrail-deploy : a root-owned copy of guardrail-deploy.sh (the account cannot change it).
# - Its authorized_keys holds exactly one key, tied to that command : no shell, no pty, no forwarding, no scp.
# - /etc/sudoers.d/guardrail-deploy : that account may run that one command as root, with no argument ; nothing else.
#   The file is checked with visudo before it is put in place.
# The public key is the deploy-only key made on the PM's computer (ci-setup.sh keygen) ; the server's administrator
# key is not touched and never goes to GitHub.
set -euo pipefail

USER_NAME=guardrail-deploy
COMMAND=/usr/local/sbin/guardrail-deploy
die() { printf '중단 : %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "root 로 돌린다 (sudo bash deploy-user-setup.sh …)"
PUB=${1:-}
SCRIPT=${2:-}
[[ -f "$PUB" && -f "$SCRIPT" ]] || die "공개 키 파일과 guardrail-deploy.sh 를 준다"
[[ $(grep -c . "$PUB") -eq 1 ]] || die "공개 키 파일은 한 줄이어야 한다"
key=$(grep . "$PUB")
# One ed25519 public key : type, base64 body, optional plain comment. No options, no private key, no second key.
[[ "$key" =~ ^ssh-ed25519\ [A-Za-z0-9+/]{60,80}=*(\ [A-Za-z0-9@._-]{1,64})?$ ]] || die "ed25519 공개 키 한 줄 모양이 아니다"
bash -n "$SCRIPT" || die "guardrail-deploy.sh 문법 오류"

id "$USER_NAME" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USER_NAME"
passwd -l "$USER_NAME" >/dev/null
install -o root -g root -m 755 "$SCRIPT" "$COMMAND"

home=$(getent passwd "$USER_NAME" | cut -d: -f6)
install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 "$home/.ssh"
tmp=$(umask 077 && mktemp "$home/.ssh/authorized_keys.XXXXXX")
printf 'restrict,command="sudo -n %s" %s\n' "$COMMAND" "$key" > "$tmp"
chown "$USER_NAME:$USER_NAME" "$tmp" && chmod 600 "$tmp" && mv "$tmp" "$home/.ssh/authorized_keys"

tmp=$(umask 077 && mktemp /etc/sudoers.d/.guardrail-deploy.XXXXXX)
printf '%s ALL=(root) NOPASSWD: %s ""\n' "$USER_NAME" "$COMMAND" > "$tmp"
visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "sudoers 줄 검사 실패 — 아무것도 바꾸지 않았다"; }
chmod 440 "$tmp" && mv "$tmp" /etc/sudoers.d/guardrail-deploy

printf '배포 전용 계정 준비 끝 : %s (명령 하나 : %s)\n' "$USER_NAME" "$COMMAND"
printf 'sudo 로 되는 것 : %s\n' "$(sudo -l -U "$USER_NAME" | tail -n 1 | sed 's/^ *//')"
