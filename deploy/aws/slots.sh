#!/usr/bin/env bash
# C-19-11 (DM-119, DM-122) : two slots on the server, so a deploy never stops the version in use. Runs ON THE SERVER as
# root : remote-install.sh calls `deploy` ; the PM calls `status` and `rollback` by hand. Claude Code never runs it
# against a server.
#
#   sudo bash slots.sh deploy RELEASE_DIR DOMAIN   start RELEASE_DIR in the idle slot, check it, move Caddy to it
#   sudo bash slots.sh rollback [RELEASE_NAME]     the same with a release already on the server (default : the newest
#                                                  one that is not in use). No DB load.
#   sudo bash slots.sh drill                       practice a failed deploy : the release in use is started in the idle
#                                                  slot with its backend stopped, so the health check really fails ;
#                                                  shows that nothing switches. Ends with 1 on purpose.
#   sudo bash slots.sh status                      the slot in use, each slot's release and unit states
#   sudo bash slots.sh drain SLOT                  (started by deploy) stop SLOT's gateway when its sessions are over
#
# Slots : a = query API 8020 · backend 8030 · gateway 8050, b = 8021 · 8031 · 8051, units guardrail-{api,backend,gateway}@SLOT
# running /opt/guardrail/slots/SLOT (a symlink to a release). The slot in use is GUARDRAIL_SLOT in /etc/guardrail/caddy.env
# together with the three upstream lines the Caddyfile reads. A server deployed before this (single units on slot a's
# ports, no GUARDRAIL_SLOT) counts as slot "legacy" : the first deploy goes to b and removes the old units.
#
# deploy, step by step — the version in use is touched only at step 3, and only after the new one answered :
# 1. Idle slot : stop what a previous deploy left there, point it at the release, start its three units.
# 2. Health : the query API (/api/summary), the backend (/healthz, reads the DB) and the gateway (/healthz) on the new
#    ports. Any of them silent : the new units are stopped and the script ends with 1. Nothing else changed.
# 3. Switch : caddy.env gets the new upstream lines, the Caddyfile is validated with them, /opt/guardrail/app moves to
#    the release (the React files), Caddy reloads (open connections are kept). Then the sign-in config is asked through
#    Caddy itself ; no answer puts caddy.env, the Caddyfile and /opt/guardrail/app back, reloads, stops the new units, 1.
# 4. Old slot : the query API and the backend stop after a short wait. The gateway stays (its sessions' judgement state
#    is in its memory ; the new gateway hands their requests over) until it has no open session, or no request for
#    15 minutes, or an hour has passed — `drain`, started detached with systemd-run.
set -euo pipefail

P=${GUARDRAIL_PREFIX:-}                 # offline tests : a folder that stands in for / (no root, fake systemctl on PATH)
OPT=$P/opt/guardrail
ETC=$P/etc/guardrail
UNITS=$P/etc/systemd/system
CADDYFILE=$P/etc/caddy/Caddyfile
CADDY_ENV=$ETC/caddy.env
SELF_DIR=$(cd "$(dirname "$0")" && pwd -P)
SERVICES="api backend gateway"
HEALTH_TRIES=${GUARDRAIL_HEALTH_TRIES:-30}              # one second apart
RETIRE_WAIT=${GUARDRAIL_RETIRE_WAIT_SECONDS:-5}         # requests already inside the old API · backend finish
DRAIN_MAX=${GUARDRAIL_DRAIN_MAX_SECONDS:-3600}          # the old gateway never outlives this
DRAIN_IDLE=${GUARDRAIL_DRAIN_IDLE_SECONDS:-900}         # longer than one model turn may take (Caddy waits 660 s)
DRAIN_POLL=${GUARDRAIL_DRAIN_POLL_SECONDS:-15}
FIRST_DEPLOY=no
DRILL=no
KEEP_RELEASES=3                                         # the one in use and the two before it

die() { printf '중단 : %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

need_root() { [[ -n "$P" || $(id -u) -eq 0 ]] || die "root 로 돌린다 (sudo bash slots.sh …)"; }

# --- slots ---

slot_ports() {
    case "$1" in
        a|legacy) API_PORT=8020 BACKEND_PORT=8030 GATEWAY_PORT=8050 ;;
        b) API_PORT=8021 BACKEND_PORT=8031 GATEWAY_PORT=8051 ;;
        *) die "모르는 칸 : $1" ;;
    esac
}

unit() {    # unit SERVICE SLOT
    if [[ "$2" == legacy ]]; then printf 'guardrail-%s.service' "$1"; else printf 'guardrail-%s@%s.service' "$1" "$2"; fi
}

env_value() { sed -n "s/^$1=//p" "$CADDY_ENV" 2>/dev/null | tail -n 1; }

active_slot() {
    local slot
    slot=$(env_value GUARDRAIL_SLOT)
    case "$slot" in
        a|b) printf '%s' "$slot" ;;
        '') if systemctl is-active --quiet guardrail-api.service 2>/dev/null; then printf legacy; else printf none; fi ;;
        *) die "caddy.env 의 GUARDRAIL_SLOT 이 a · b 가 아니다" ;;
    esac
}

idle_slot() {    # idle_slot ACTIVE : where the next version goes
    case "$1" in a) printf b ;; b|none) printf a ;; legacy) printf b ;; esac
}

# Replace a symlink in one step (GNU mv -T on the server ; macOS in the offline tests has no -T).
point() {
    ln -sfn "$1" "$2.next"
    mv -T "$2.next" "$2" 2>/dev/null || { rm -f "$2.next"; ln -sfn "$1" "$2"; }
}

stop_slot() {    # every unit of a slot, and a drain still waiting on it
    local slot=$1 service
    systemctl stop "guardrail-drain-$slot.service" 2>/dev/null || true
    for service in $SERVICES; do
        systemctl stop "$(unit "$service" "$slot")" 2>/dev/null || true
    done
}

wait_http() {    # wait_http URL
    local i
    for i in $(seq 1 "$HEALTH_TRIES"); do
        curl -fsS -o /dev/null --max-time 5 "$1" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

healthy() {    # healthy SLOT : every service of the slot answers on its own port
    local slot=$1 failed=
    slot_ports "$slot"
    wait_http "http://127.0.0.1:$API_PORT/api/summary" || failed=api
    [[ -n "$failed" ]] || wait_http "http://127.0.0.1:$BACKEND_PORT/healthz" || failed=backend
    [[ -n "$failed" ]] || wait_http "http://127.0.0.1:$GATEWAY_PORT/healthz" || failed=gateway
    [[ -z "$failed" ]] && return 0
    journalctl -u "$(unit "$failed" "$slot")" -n 30 --no-pager >&2 2>/dev/null || true
    say "상태 확인 실패 : $failed (칸 $slot)" >&2
    return 1
}

# --- 1 · 2 : start the idle slot and check it ---

start_slot() {    # start_slot SLOT RELEASE
    local slot=$1 release=$2 service tmp
    slot_ports "$slot"
    stop_slot "$slot"
    mkdir -p "$OPT/slots" "$ETC" "$UNITS"
    tmp=$(umask 027 && mktemp "$ETC/slot-$slot.env.XXXXXX")
    printf 'GUARDRAIL_API_PORT=%s\nGUARDRAIL_BACKEND_PORT=%s\nGUARDRAIL_GATEWAY_PORT=%s\n' \
        "$API_PORT" "$BACKEND_PORT" "$GATEWAY_PORT" > "$tmp"
    [[ -n "$P" ]] || chown root:guardrail "$tmp"
    chmod 640 "$tmp" && mv "$tmp" "$ETC/slot-$slot.env"
    for service in $SERVICES; do
        install -m 644 "$SELF_DIR/guardrail-$service@.service" "$UNITS/guardrail-$service@.service" || return 1
    done
    systemctl daemon-reload || return 1
    point "$release" "$OPT/slots/$slot" || return 1
    for service in $SERVICES; do
        systemctl restart "$(unit "$service" "$slot")" || return 1
    done
    # drill : a version whose backend does not come up (the check below is the real one and really fails).
    [[ "$DRILL" == no ]] || systemctl stop "$(unit backend "$slot")"
    healthy "$slot"
}

# --- 3 : move Caddy ---

caddy_env_for() {    # caddy_env_for SLOT DOMAIN > file : the other lines of caddy.env are kept
    slot_ports "$1"
    { grep -v -e '^GUARDRAIL_DOMAIN=' -e '^GUARDRAIL_BASIC_AUTH_' -e '^GUARDRAIL_SLOT=' \
          -e '^GUARDRAIL_API_UPSTREAM=' -e '^GUARDRAIL_BACKEND_UPSTREAM=' -e '^GUARDRAIL_GATEWAY_UPSTREAM=' \
          "$CADDY_ENV" 2>/dev/null || true
      printf 'GUARDRAIL_DOMAIN=%s\n' "$2"
      printf 'GUARDRAIL_SLOT=%s\n' "$1"
      printf 'GUARDRAIL_API_UPSTREAM=127.0.0.1:%s\n' "$API_PORT"
      printf 'GUARDRAIL_BACKEND_UPSTREAM=127.0.0.1:%s\n' "$BACKEND_PORT"
      printf 'GUARDRAIL_GATEWAY_UPSTREAM=127.0.0.1:%s\n' "$GATEWAY_PORT"
    }
}

caddy_with() {    # caddy_with ENV_FILE ARGS... : caddy with the file's values (the Caddyfile reads {$GUARDRAIL_*})
    local file=$1
    shift
    ( set -a; . "$file"; set +a; caddy "$@" >/dev/null )
}

caddy_reload() {
    if systemctl is-active --quiet caddy 2>/dev/null; then
        caddy_with "$CADDY_ENV" reload --config "$CADDYFILE" --adapter caddyfile --force \
            || systemctl reload-or-restart caddy
    else
        systemctl restart caddy
    fi
}

through_caddy() {    # through_caddy DOMAIN : the backend answers through Caddy on this machine (no sign-in needed)
    local i
    for i in $(seq 1 10); do
        curl -fsS -o /dev/null --max-time 10 --resolve "$1:443:127.0.0.1" "https://$1/api/auth/config" 2>/dev/null \
            && return 0
        sleep 1
    done
    return 1
}

switch_to() {    # switch_to SLOT RELEASE DOMAIN
    local slot=$1 release=$2 domain=$3 next before_app= service
    mkdir -p "$(dirname "$CADDYFILE")"
    next=$(umask 077 && mktemp "$CADDY_ENV.XXXXXX")
    caddy_env_for "$slot" "$domain" > "$next"
    chmod 600 "$next"
    if ! caddy_with "$next" validate --config "$SELF_DIR/Caddyfile.template" --adapter caddyfile; then
        rm -f "$next"
        say "새 Caddyfile 검사 실패 — Caddy 는 그대로다" >&2
        return 1
    fi
    [[ -e "$OPT/app" ]] && before_app=$(readlink "$OPT/app" || true)
    cp -p "$CADDY_ENV" "$CADDY_ENV.before" 2>/dev/null || : > "$CADDY_ENV.before"
    cp -p "$CADDYFILE" "$CADDYFILE.before" 2>/dev/null || true
    install -m 644 "$SELF_DIR/Caddyfile.template" "$CADDYFILE"
    mv "$next" "$CADDY_ENV"
    point "$release" "$OPT/app"
    # A server that never served before may still be waiting for its certificate : not a reason to undo.
    if caddy_reload && { through_caddy "$domain" || [[ "$FIRST_DEPLOY" == yes ]]; }; then
        for service in $SERVICES; do
            systemctl enable "$(unit "$service" "$slot")" >/dev/null 2>&1 || true
        done
        rm -f "$CADDY_ENV.before" "$CADDYFILE.before"
        return 0
    fi
    say "전환 뒤 Caddy 경유 확인 실패 — 이전 칸으로 되돌린다" >&2
    cp -p "$CADDY_ENV.before" "$CADDY_ENV" && chmod 600 "$CADDY_ENV"
    [[ -f "$CADDYFILE.before" ]] && cp -p "$CADDYFILE.before" "$CADDYFILE"
    [[ -n "$before_app" ]] && point "$before_app" "$OPT/app"
    rm -f "$CADDY_ENV.before" "$CADDYFILE.before"
    caddy_reload || true
    return 1
}

# --- 4 : the old slot ---

retire() {    # retire OLD_SLOT
    local old=$1 service
    [[ "$old" == none ]] && return 0
    sleep "$RETIRE_WAIT"
    for service in api backend; do
        systemctl stop "$(unit "$service" "$old")" 2>/dev/null || true
        systemctl disable "$(unit "$service" "$old")" >/dev/null 2>&1 || true
    done
    [[ "$old" != legacy ]] || rm -f "$UNITS/guardrail-api.service" "$UNITS/guardrail-backend.service"
    # The script of the release now in use : the old slot's own release may be removed before the drain ends.
    if ! systemd-run --quiet --collect --unit "guardrail-drain-$old" \
            /bin/bash "$SELF_DIR/slots.sh" drain "$old" 2>/dev/null; then
        say "주의 : 게이트웨이 대기 작업을 띄우지 못해 옛 게이트웨이를 바로 끈다 (진행 중 세션은 새 세션 키로 다시)" >&2
        stop_gateway "$old"
    fi
}

stop_gateway() {
    local gateway
    gateway=$(unit gateway "$1")
    systemctl stop "$gateway" 2>/dev/null || true
    systemctl disable "$gateway" >/dev/null 2>&1 || true
    if [[ "$1" == legacy ]]; then
        rm -f "$UNITS/guardrail-gateway.service"
        systemctl daemon-reload || true
    fi
}

drain() {    # drain OLD_SLOT : wait for the old gateway's sessions, then stop it
    local old=$1 waited=0 body open idle why="상한 ${DRAIN_MAX} 초"
    [[ "$(active_slot)" != "$old" ]] || die "칸 $old 은 지금 쓰는 칸이다"
    slot_ports "$old"
    while (( waited < DRAIN_MAX )); do
        if ! body=$(curl -fsS --max-time 5 "http://127.0.0.1:$GATEWAY_PORT/healthz" 2>/dev/null); then
            why="이미 꺼짐"; break
        fi
        open=$(printf '%s' "$body" | sed -n 's/.*"open_sessions": *\([0-9][0-9]*\).*/\1/p')
        idle=$(printf '%s' "$body" | sed -n 's/.*"idle_seconds": *\([0-9][0-9]*\).*/\1/p')
        # A gateway from before C-19-11 reports only "sessions" (every session it ever started).
        [[ -n "$open" ]] || open=$(printf '%s' "$body" | sed -n 's/.*"sessions": *\([0-9][0-9]*\).*/\1/p')
        if [[ "${open:-1}" == 0 ]]; then why="열린 세션 0"; break; fi
        if [[ -n "$idle" ]] && (( idle >= DRAIN_IDLE )); then why="요청 없음 ${idle} 초"; break; fi
        sleep "$DRAIN_POLL"
        waited=$(( waited + DRAIN_POLL ))
    done
    stop_gateway "$old"
    say "옛 게이트웨이 (칸 $old) 종료 : $why"
}

# --- releases ---

prune() {    # keep the newest KEEP_RELEASES and whatever a slot or app still points at
    local old keep
    keep=$(for link in "$OPT/app" "$OPT/slots/a" "$OPT/slots/b"; do
               if [[ -e "$link" ]]; then (cd "$link" && pwd -P); fi
           done)
    ls -1dt "$OPT"/releases/*/ 2>/dev/null | tail -n +$(( KEEP_RELEASES + 1 )) | while read -r old; do
        old=$(cd "$old" && pwd -P)
        grep -qxF "$old" <<< "$keep" || rm -rf -- "$old"
    done
}

deploy() {    # deploy RELEASE DOMAIN
    local release=$1 domain=$2 active target
    [[ -f "$release/DEPLOY_INFO.json" ]] || die "릴리스 폴더가 아니다 : $release"
    [[ "$domain" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || die "도메인 모양이 아니다"
    release=$(cd "$release" && pwd -P)
    active=$(active_slot)
    target=$(idle_slot "$active")
    FIRST_DEPLOY=no
    [[ "$active" != none ]] || FIRST_DEPLOY=yes
    step "칸 : 지금 $active → 새 버전은 $target"
    if ! start_slot "$target" "$release"; then
        stop_slot "$target"
        die "새 버전이 칸 $target 에서 응답하지 않는다. 전환하지 않았다 — 지금 버전 (칸 $active) 그대로다"
    fi
    say "칸 $target 응답 확인 (조회 API · 백엔드 · 게이트웨이)"
    step "전환 : Caddy → 칸 $target"
    if ! switch_to "$target" "$release" "$domain"; then
        stop_slot "$target"
        die "전환하지 못했다 — 지금 버전 (칸 $active) 그대로다"
    fi
    step "옛 칸 $active 정리 (게이트웨이는 진행 중 세션이 끝날 때까지 남는다)"
    retire "$active"
    if [[ "$FIRST_DEPLOY" == no ]] && ! through_caddy "$domain"; then
        say "주의 : 옛 칸을 끈 뒤 Caddy 경유 응답이 없다 → Caddy 를 다시 시작한다" >&2
        systemctl restart caddy
        through_caddy "$domain" || die "Caddy 경유 응답이 없다 (칸 $target 은 떠 있다) : slots.sh status"
    fi
    prune
    say "전환 끝 : 칸 $target ← $(basename "$release")"
}

rollback() {    # rollback [RELEASE_NAME]
    local name=${1:-} current release domain
    current=$(cd "$OPT/app" 2>/dev/null && pwd -P) || die "배포된 릴리스가 없다"
    if [[ -n "$name" ]]; then
        [[ "$name" =~ ^[0-9a-f]{12}-[0-9TZ]{16}$ ]] || die "릴리스 이름 모양이 아니다 (slots.sh status 의 이름)"
        release=$OPT/releases/$name
    else
        release=$(ls -1dt "$OPT"/releases/*/ 2>/dev/null | while read -r dir; do
            dir=$(cd "$dir" && pwd -P)
            [[ "$dir" == "$current" ]] || { printf '%s' "$dir"; break; }
        done)
    fi
    [[ -n "$release" && -d "$release" ]] || die "되돌릴 릴리스가 없다"
    [[ "$(cd "$release" && pwd -P)" != "$current" ]] || die "그 릴리스는 지금 쓰는 릴리스다"
    domain=$(env_value GUARDRAIL_DOMAIN)
    step "되돌림 : $(basename "$release") (DB 적재 없음, 패키지는 그 릴리스의 목록으로)"
    UV_PYTHON_INSTALL_DIR=$OPT/python uv pip install --python "$OPT/venv/bin/python" -q \
        -r "$release/service/requirements-server.txt"
    deploy "$release" "$domain"
}

drill() {
    local current
    current=$(cd "$OPT/app" 2>/dev/null && pwd -P) || die "배포된 릴리스가 없다"
    [[ "$(active_slot)" != none ]] || die "쓰는 칸이 없다"
    step "연습 : 백엔드가 뜨지 않는 버전을 배포한다 (실패해야 정상, 지금 버전은 그대로여야 한다)"
    DRILL=yes
    HEALTH_TRIES=5
    deploy "$current" "$(env_value GUARDRAIL_DOMAIN)"
}

status() {
    local active slot service link
    active=$(active_slot)
    say "지금 쓰는 칸 : $active"
    for link in app slots/a slots/b; do
        if [[ -e "$OPT/$link" ]]; then say "$link → $(basename "$(cd "$OPT/$link" && pwd -P)")"; fi
    done
    for slot in a b legacy; do
        for service in $SERVICES; do
            say "$(unit "$service" "$slot") : $(systemctl is-active "$(unit "$service" "$slot")" 2>/dev/null || true)"
        done
    done
    say "서버에 남은 릴리스 (새것부터) :"
    ls -1dt "$OPT"/releases/*/ 2>/dev/null | while read -r link; do say "  $(basename "$link")"; done
}

case "${1:-}" in
    deploy) need_root; [[ $# -eq 3 ]] || die "slots.sh deploy RELEASE_DIR DOMAIN"; deploy "$2" "$3" ;;
    rollback) need_root; rollback "${2:-}" ;;
    drill) need_root; drill ;;
    drain) need_root; [[ $# -eq 2 ]] || die "slots.sh drain SLOT"; drain "$2" ;;
    status) status ;;
    pick) active=$(active_slot); idle_slot "$active"; printf '\n' ;;
    *) sed -n '2,29p' "$0"; exit 2 ;;
esac
