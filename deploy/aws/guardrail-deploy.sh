#!/usr/bin/env bash
# C-19-11 part 3 (DM-122) : the ONE command the server's deploy-only account may run as root.
# Installed as /usr/local/sbin/guardrail-deploy (root, 755) by deploy-user-setup.sh. The account's SSH key is tied to
# it (authorized_keys : restrict,command="sudo -n /usr/local/sbin/guardrail-deploy") and sudoers allows that account
# this command only, with no argument. So the key in GitHub can hand the server a bundle and nothing else : no shell,
# no file copy, no port forwarding, no other sudo.
#
#   (bundle.tar.gz on stdin)
#
# Takes no argument and no environment (sudo resets it). The domain comes from the server's own
# /etc/guardrail/caddy.env and the Google client ID stays what the server has. One deploy at a time. The bundle must be
# a gzip tar of at most 64 MiB with only relative paths, holding DEPLOY_INFO.json and its installer ; then the bundle's
# own deploy/aws/remote-install.sh runs — the same script the PM's deploy.sh runs — so a failed load check, health
# check or switch ends with 1 and the version in use stays.
# Reported limit : the installer comes with the bundle and runs as root, so whoever holds this key (or can push to
# main) can run code on the server — that is what deploying is. What this command removes is everything else.
set -euo pipefail
umask 077

P=${GUARDRAIL_PREFIX:-}        # offline tests only (sudo drops the variable on the server)
MAX_BYTES=$(( 64 * 1024 * 1024 ))
die() { printf '중단 : %s\n' "$*" >&2; exit 1; }

[[ $# -eq 0 ]] || die "인자를 받지 않는다"
[[ -n "$P" || $(id -u) -eq 0 ]] || die "root 로 돌린다"

if command -v flock >/dev/null 2>&1; then
    exec 9> "$P/run/lock/guardrail-deploy.lock"
    flock -n 9 || die "다른 배포가 진행 중이다"
fi

work=$(mktemp -d "$P/var/tmp/guardrail-deploy.XXXXXX")
trap 'rm -rf "$work"' EXIT
bundle=$work/bundle.tar.gz
head -c $(( MAX_BYTES + 1 )) > "$bundle"
size=$(wc -c < "$bundle" | tr -d ' ')
(( size > 0 )) || die "묶음이 비어 있다"
(( size <= MAX_BYTES )) || die "묶음이 상한 ($MAX_BYTES 바이트) 을 넘는다"

names=$(tar -tzf "$bundle" 2>/dev/null) || die "gzip tar 묶음이 아니다"
if printf '%s\n' "$names" | grep -q -e '^/' -e '^\.\./' -e '/\.\./' -e '/\.\.$' -e '^\.\.$'; then
    die "묶음에 폴더 밖을 가리키는 경로가 있다"
fi
for needed in DEPLOY_INFO.json deploy/aws/remote-install.sh deploy/aws/slots.sh; do
    printf '%s\n' "$names" | grep -qxF "$needed" || die "묶음에 $needed 가 없다"
done

domain=$(sed -n 's/^GUARDRAIL_DOMAIN=//p' "$P/etc/guardrail/caddy.env" 2>/dev/null | tail -n 1)
[[ "$domain" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] \
    || die "서버에 도메인이 없다 (첫 배포는 PM 이 deploy/aws/deploy.sh 로)"

tar -xzf "$bundle" -C "$work" --no-same-owner deploy/aws/remote-install.sh
chmod 644 "$bundle"    # remote-install.sh reads it as root ; the folder itself stays owner-only
bash "$work/deploy/aws/remote-install.sh" "$bundle" "$domain" ""
