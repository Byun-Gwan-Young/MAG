#!/usr/bin/env bash
# C-19-7 (DM-91, DM-97 ①) : install one bundle ON THE SERVER as root. deploy.sh sends and runs it ; not run by hand.
#
#   sudo bash remote-install.sh BUNDLE.tar.gz DOMAIN [GOOGLE_CLIENT_ID]
#
# 1. Unpack into a new release folder /opt/guardrail/releases/<commit>-<time>, update the service venv from the bundle's
#    service/requirements-server.txt (uv).
# 2. From that release, as user guardrail (peer authentication) : `python -m storage init`, `python -m storage demo-all`,
#    `python -m service.load_demo check` (the merged loader). The check result stays in /opt/guardrail/checks/. If any
#    check differs (or a step fails) the script stops HERE : the running API and site keep the previous release.
# 3. DM-108 : /etc/guardrail/backend.env for the service backend and the gateway (DB URL, sign-in URI on DOMAIN, secure
#    cookie, and GOOGLE_CLIENT_ID when given ; an empty argument keeps the one already on the server).
#    C-19-8K : also GUARDRAIL_DEFAULT_PROVIDER (kmu) and GUARDRAIL_PROVIDER_KMU_URL ; values already there are kept.
# 4. C-19-11 (DM-122) : the switch is the release's own deploy/aws/slots.sh — the query API, the backend and the
#    gateway start in the idle slot (a : 8020 · 8030 · 8050, b : 8021 · 8031 · 8051) beside the version in use ; only when
#    all three answer does Caddy move to them (reload, no dropped connection) ; a failed check leaves the version in
#    use untouched and ends with 1. The old gateway stays until its sessions are over. The release in use and the two
#    before it are kept (slots.sh rollback).
# The same script serves the PM's deploy.sh and the GitHub deploy (the server's guardrail-deploy command).
set -euo pipefail

BUNDLE=${1:-}
DOMAIN=${2:-}
CLIENT_ID=${3:-}
DB_URL='postgresql:///guardrail_demo?host=/var/run/postgresql'
PY=/opt/guardrail/venv/bin/python
CHECKS=/opt/guardrail/checks
BACKEND_ENV=/etc/guardrail/backend.env
export UV_PYTHON_INSTALL_DIR=/opt/guardrail/python

die() { printf '중단 : %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
as_guardrail() { runuser -u guardrail -- env -i PATH=/usr/bin:/bin LANG=C.UTF-8 GUARDRAIL_DB_URL="$DB_URL" "$PY" -B "$@"; }

[[ $(id -u) -eq 0 ]] || die "root 로 돌린다"
[[ -f "$BUNDLE" ]] || die "묶음 파일이 없다 : $BUNDLE"
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || die "도메인 모양이 아니다"
[[ -z "$CLIENT_ID" || "$CLIENT_ID" =~ ^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$ ]] || die "구글 클라이언트 ID 모양이 아니다"
[[ -x "$PY" ]] || die "서비스 가상환경이 없다 (server-setup.sh 먼저)"

step "풀기"
stage=$(mktemp -d /opt/guardrail/releases/.incoming.XXXXXX)
tar -xzf "$BUNDLE" -C "$stage" --no-same-owner
commit=$("$PY" -I -c 'import json, sys; print(json.load(open(sys.argv[1]))["commit"])' "$stage/DEPLOY_INFO.json")
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "DEPLOY_INFO.json 의 commit 이 없다"
release=/opt/guardrail/releases/${commit:0:12}-$(date -u +%Y%m%dT%H%M%SZ)
mv "$stage" "$release"
chown -R root:root "$release" && chmod -R a+rX,go-w "$release"
printf '릴리스 %s\n' "$release"

step "서비스 가상환경 갱신"
uv pip install --python "$PY" -q -r "$release/service/requirements-server.txt"
chmod -R a+rX /opt/guardrail/venv

step "DB : storage init → storage demo-all → service.load_demo check (DM-97 ①)"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
result="$CHECKS/check-postgres-$stamp.json"
cd "$release"
as_guardrail -m storage init
as_guardrail -m storage demo-all
set +e
as_guardrail -m service.load_demo check --output "$result" > "$CHECKS/check-postgres-$stamp.out"
status=$?
set -e
cp "$result" "$CHECKS/check-postgres-latest.json" 2>/dev/null || true
chmod 644 "$CHECKS"/check-postgres-* 2>/dev/null || true
if (( status != 0 )); then
    tail -n 40 "$CHECKS/check-postgres-$stamp.out" >&2 || true
    die "check 가 같지 않다 (종료 코드 $status). API 와 화면은 이전 릴리스 그대로다. 결과 : $result"
fi
printf 'check : 모두 같음 (%s)\n' "$result"

step "backend.env (백엔드 · 게이트웨이가 읽는다, DM-108)"
[[ -n "$CLIENT_ID" ]] || CLIENT_ID=$(sed -n 's/^GOOGLE_CLIENT_ID=//p' "$BACKEND_ENV" 2>/dev/null | tail -n 1)
# C-19-8K part 0 : creating a project needs a default provider. PM : kmu (the school gateway, the demo's upstream).
# A value already on the server is kept. Neither line is a secret (no key, only a provider name and a public URL).
DEFAULT_PROVIDER=$(sed -n 's/^GUARDRAIL_DEFAULT_PROVIDER=//p' "$BACKEND_ENV" 2>/dev/null | tail -n 1)
[[ -n "$DEFAULT_PROVIDER" ]] || DEFAULT_PROVIDER=kmu
[[ "$DEFAULT_PROVIDER" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ ]] || die "backend.env 의 GUARDRAIL_DEFAULT_PROVIDER 모양이 아니다"
KMU_URL=$(sed -n 's/^GUARDRAIL_PROVIDER_KMU_URL=//p' "$BACKEND_ENV" 2>/dev/null | tail -n 1)
[[ -n "$KMU_URL" ]] || KMU_URL=https://factchat.mindlogic-kr-api.com/v1/gateway    # = upstream.base_url of guardrail-deadlock-v0.3-kmu.yaml
[[ "$KMU_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]] || die "backend.env 의 GUARDRAIL_PROVIDER_KMU_URL 모양이 아니다"
tmp=$(umask 027 && mktemp "$BACKEND_ENV.XXXXXX")
{
    printf 'GUARDRAIL_DB_URL=%s\n' "$DB_URL"
    printf 'GOOGLE_LOGIN_URI=https://%s/api/auth/google\n' "$DOMAIN"
    printf 'AUTH_COOKIE_SECURE=true\n'
    printf 'GUARDRAIL_OPEN_SIGNUP=true\n'    # DM-109 : any verified Google account the OAuth app lets through
    printf 'GUARDRAIL_DEFAULT_PROVIDER=%s\n' "$DEFAULT_PROVIDER"
    printf 'GUARDRAIL_PROVIDER_KMU_URL=%s\n' "$KMU_URL"
    [[ -z "$CLIENT_ID" ]] || printf 'GOOGLE_CLIENT_ID=%s\n' "$CLIENT_ID"
} > "$tmp"
chown root:guardrail "$tmp" && chmod 640 "$tmp" && mv "$tmp" "$BACKEND_ENV"
[[ -n "$CLIENT_ID" ]] || printf '주의 : 구글 클라이언트 ID 가 없다. 로그인 화면은 열리지만 로그인 버튼이 나오지 않는다\n'

step "두 칸 전환 (C-19-11) : 새 버전을 쉬는 칸에 → 상태 확인 → Caddy 전환 → 옛 칸 정리"
# Everything from here is slots.sh : a failed health check or switch ends with 1 and the version in use stays.
bash "$release/deploy/aws/slots.sh" deploy "$release" "$DOMAIN" \
    || die "새 버전으로 바꾸지 못했다 (위 출력). 서버는 이전 버전 그대로다"
printf '\n배포 끝 : %s (commit %s)\n' "$release" "${commit:0:12}"
