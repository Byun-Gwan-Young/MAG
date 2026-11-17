# deploy/aws — 데모 서버 (AWS EC2 한 대)

데모 화면과 조회 API 를 팀원 · 교수님만 볼 수 있게 AWS 서버 한 대로 엽니다 (C-19-7, DM-91 · DM-93). Claude Code 는 AWS 에 접속하지 않습니다. 이 폴더의 스크립트는 **PM 이 맥과 서버에서**, 그리고 (C-19-11 부터) **GitHub 러너가** 돌립니다. 처음 만드는 순서는 [`reports/c19-7/pm-commands.md`](../../reports/c19-7/pm-commands.md), 자동 배포 준비는 [`reports/c19-11/pm-commands.md`](../../reports/c19-11/pm-commands.md) 에 있습니다.

## 자동 무중단 배포 (C-19-11, DM-122)

main 에 push 하면 GitHub Actions 가 오프라인 테스트 · 동결 검사 · 프론트 빌드를 돌리고 ([`ci.yml`](../../.github/workflows/ci.yml)), 통과하면 묶음을 만들어 서버로 보냅니다 ([`deploy.yml`](../../.github/workflows/deploy.yml)). PM 의 인터넷에 기대지 않습니다.

```text
GitHub 러너 ── OIDC ──▶ AWS 역할 (보안 그룹 22 번 규칙 추가 · 삭제만)
     │  ① 러너 IP/32 를 22 번에 추가        ④ 끝나면 (실패해도) 삭제
     │  ② 묶음을 배포 전용 계정으로 (명령 하나만 되는 키)
     ▼
서버  guardrail-deploy → remote-install.sh → slots.sh
     칸 a : 조회 API 8020 · 백엔드 8030 · 게이트웨이 8050
     칸 b : 조회 API 8021 · 백엔드 8031 · 게이트웨이 8051
     ③ 쉬는 칸에 새 버전 → 세 서비스가 응답하면 Caddy 를 그 칸으로 (reload, 연결 유지) → 옛 칸 정리
        하나라도 응답이 없으면 전환하지 않고 실패 (쓰던 버전 그대로)
```

- 쓰는 칸은 `/etc/guardrail/caddy.env` 의 `GUARDRAIL_SLOT` 과 업스트림 세 줄입니다. `deploy/aws/remote.sh status` 로 봅니다
- 옛 칸의 게이트웨이는 바로 끄지 않습니다. 진행 중 세션의 판정 상태 (연속 횟수) 가 그 프로세스 메모리에 있기 때문에, 열린 세션이 0 이 되거나 · 15 분 동안 요청이 없거나 · 1 시간이 지날 때까지 남고, 새 게이트웨이가 그 세션의 요청을 넘겨줍니다
- 로그인 세션은 백엔드 메모리에 있어 배포 뒤 한 번 다시 로그인합니다 (PM 11/17 : 감수)
- 되돌리기 : `deploy/aws/remote.sh rollback` (쓰는 릴리스와 그 앞 2 개가 서버에 남습니다, DB 적재 없음). 실패 연습 : `deploy/aws/remote.sh drill`
- 손 배포 `deploy/aws/deploy.sh` 도 같은 `remote-install.sh` · `slots.sh` 를 거칩니다

## 구조

```text
브라우저
   │ HTTPS · 구글 로그인 먼저 (DM-110, forward_auth) — 로그인 전에는 /login · /api/auth/* · /assets/* 만
   ▼
Caddy  (서버, 80 · 443 만 열림, 인증서 자동)
   ├─ /api/auth/* · /api/projects*  → 백엔드 (backend/, 127.0.0.1:8030, 구글 로그인 · 계정 · 프로젝트)
   ├─ /api/frontend/reference       → site/reference.json (묶음에 든 동결 참고 데이터)
   ├─ /api/* · /docs · /redoc · /openapi.json
   │                                → 조회 API (service/api.py, 127.0.0.1:8020, 읽기 전용, systemd guardrail-api)
   │                                     │ 유닉스 소켓 · peer 인증 (비밀번호 없음)
   │                                     ▼
   │                                  PostgreSQL guardrail_demo (같은 서버, 외부 포트 없음)
   ├─ /assets/*                     → React 빌드의 JS · CSS (해시 이름, 오래 캐시)
   └─ 나머지 (/, /login, …)          → React 화면 (site/index.html, 단일 페이지 앱, 캐시 안 함)
                                      /legacy/ 는 발표 대비 옛 파이썬 화면
```

React 화면은 같은 서버의 API (`/api/auth/me` · `/api/summary` · `/api/sessions` · `/api/projects` …) 를 불러 보여 주기만 합니다. 실시간 모니터 (`/api/live/*`) 는 서비스 게이트웨이 (C-19-9) 전까지 "서버 게이트웨이 준비 중" 으로 나옵니다.

## 파일

| 파일 | 어디서 | 하는 일 |
|---|---|---|
| [`provision.sh`](provision.sh) | 맥 (AWS CLI) | 키 페어 · 보안 그룹 (SSH 는 지금 공인 IP/32 만, 80 · 443 전체) · EC2 (서울, Ubuntu 24.04, t3.small, 20 GiB gp3) · 탄력적 IP · (Route 53 이면) A 레코드. `--dry-run` 이 기본, `--apply` 일 때만 만든다. `--update-ssh-ip` 는 SSH 허용 IP 만 바꾼다. 결과로 `target.env` 를 쓴다 |
| [`teardown.sh`](teardown.sh) | 맥 (AWS CLI) | 태그 `project=guardrail-demo` 로 찾은 인스턴스 · 탄력적 IP · 보안 그룹 · 키 페어를 보여 주고, `delete` 를 입력해야 지운다 |
| [`configure.sh`](configure.sh) | 맥 | `provision.sh` 를 쓰지 않았을 때 도메인 · 서버 IP · SSH 키 경로를 물어 `target.env` 에 쓴다 |
| [`remote.sh`](remote.sh) | 맥 | `target.env` 값으로 서버에 접속 (`ssh`), 서버 준비 (`setup`), 상태 (`status`), DNS 반영 확인 (`dns`), 브라우저로 열기 (`open`) |
| [`server-setup.sh`](server-setup.sh) | 서버 (sudo, 한 번) | PostgreSQL · Caddy (공식 apt 저장소) · uv · Python 3.14, 사용자 `guardrail`, 폴더, DB 역할 · DB, 서비스 가상환경. 다시 돌려도 안전 |
| [`deploy.sh`](deploy.sh) | 맥 | 묶음 만들기 → 보내기 → 서버 설치 (`remote-install.sh`) → 확인 결과 가져오기 → `https://도메인/api/summary` 확인 |
| [`bundle.py`](bundle.py) | 맥 | 서버로 가는 묶음 : `git archive main` 의 포함 목록 − 제외 목록, React 빌드 (`frontend/dist`) · 참고 데이터 · 옛 화면, 검사 (빌드 JS · CSS 의 키 패턴 포함) |
| [`remote-install.sh`](remote-install.sh) | 서버 (deploy 가 실행) | 릴리스 폴더 → 가상환경 → `storage init` · `storage demo-all` · `service.load_demo check` (하나라도 다르면 멈춤, 이전 릴리스 유지) → `backend.env` → `slots.sh deploy` |
| [`slots.sh`](slots.sh) | 서버 | 두 칸 전환 : 쉬는 칸에 새 버전 → 상태 확인 → Caddy 전환 → 옛 칸 정리 (`deploy`), 되돌리기 (`rollback`), 실패 연습 (`drill`), 상태 (`status`), 옛 게이트웨이 대기 (`drain`) |
| [`guardrail-deploy.sh`](guardrail-deploy.sh) | 서버 `/usr/local/sbin/guardrail-deploy` | 배포 전용 계정이 root 로 돌릴 수 있는 단 하나의 명령 : 표준 입력의 묶음을 검사하고 `remote-install.sh` 로 설치 |
| [`deploy-user-setup.sh`](deploy-user-setup.sh) | 서버 (sudo, 한 번) | 배포 전용 계정 `guardrail-deploy` · 그 명령 하나에 묶인 키 · sudoers 한 줄 |
| [`ci-setup.sh`](ci-setup.sh) | 맥 | 배포 전용 키 만들기 · 서버에 계정 만들기 · GitHub Secrets 값 하나씩 꺼내기 · IAM 정책 JSON |
| [`ci-runner-access.sh`](ci-runner-access.sh) | GitHub 러너 | 러너 IP/32 를 보안 그룹 22 번에 추가 (`open`) · 삭제 (`close`). 그보다 넓게 여는 길이 없다 |
| [`ci-send.sh`](ci-send.sh) | GitHub 러너 | 묶음을 배포 전용 계정으로 보냄 (호스트 키 고정, 키 파일은 끝나면 삭제) |
| [`Caddyfile.template`](Caddyfile.template) | 서버 `/etc/caddy/Caddyfile` | HTTPS, 로그인 먼저, 백엔드 (구글 로그인) · API 로 가는 경로 목록, React 단일 페이지 앱, 보안 헤더. 도메인 · 해시는 환경 파일에서 |
| [`guardrail-api@.service`](guardrail-api@.service) · [`guardrail-backend@.service`](guardrail-backend@.service) · [`guardrail-gateway@.service`](guardrail-gateway@.service) | 서버 systemd | 칸마다 하나씩 뜨는 유닛 (`guardrail-api@a` …). 칸의 릴리스 `/opt/guardrail/slots/칸`, 포트 `/etc/guardrail/slot-칸.env` |
| [`lib.sh`](lib.sh) | — | 위 스크립트가 함께 쓰는 값 · 함수 |

`target.env` (도메인 · 서버 IP · SSH 키 경로) 는 git 에서 제외됩니다. 비밀번호 · AWS 자격 증명 · 계정 ID 는 어느 파일에도 쓰지 않습니다 (자격 증명은 맥의 `~/.aws` 에만).

## 서버로 가는 것 · 가지 않는 것

- **간다** (main 의 커밋된 파일만, `bundle.py` 의 `INCLUDE`) : `service/`, `storage/`, `deploy/aws/`, 그 둘이 가져다 쓰는 모듈 (`tools/analyze_session.py` · `tools/gateway_prices.py` · `experiments/` 의 계약 · 프로토콜 상수 3 파일 · `scenario_schema.py` · `json_subset.py` · `progress_schema.py`), 적재 · API 가 읽는 작은 기록 (`reports/c19/aggregate-main.json`, `reports/c18-8/` 제외 목록 · 계정 보정, 세션별 `summary.json` · `scenario.yaml` · `conditions.json` · `record/*.json`, `scenarios/README.md`, `batch_plans/`, 동결 기록), **React 빌드 결과 `frontend/dist` → `site/`** (C-19-10 : PM 컴퓨터에서 `npm ci` · `npm run build` 먼저. 서버에는 Node 를 깔지 않는다. dist 가 없으면 묶음을 만들지 않고, main 의 프론트 소스와 다르거나 소스보다 오래된 dist 는 문제로 잡는다), React 가 읽는 동결 참고 데이터 `site/reference.json` (`frontend/export_reference.py`), 발표 대비 옛 화면 `site/legacy/index.html` (`tools/demo_ui.py build --api`)
- **가지 않는다** (`bundle.py` 의 `EXCLUDE`, 묶음 목록에서 한 번 더 검사) : 프록시 · 실행기 · 배치 실행기, KMU · Tavily 키 (`.env*`, `*.pem` 등), 세션 원본 (`logs/`, `session.json`, `steps.json` 등), `docs/handoff`, venv
- 묶음의 모든 파일 (빌드된 JS · CSS 포함) 을 키 패턴 · 모델 발언 필드 (narrative · reasoning 본문 · 메시지 내용) · 1 MiB 넘는 파일로 검사하고, 하나라도 걸리면 묶음을 만들지 않습니다

## 다시 배포하기

main 에 push 하면 자동으로 배포됩니다 (위 "자동 무중단 배포"). 손으로는 맥에서 `deploy/aws/deploy.sh` 를 돌립니다. 어느 쪽이든 새 릴리스 폴더에서 적재 · 확인이 모두 같고 새 칸의 세 서비스가 응답할 때만 전환하며, 쓰는 릴리스와 그 앞 2 개를 남깁니다.

## 끄기 · 비용

- **잠시 멈춤** : EC2 콘솔에서 인스턴스 중지 (디스크 · 탄력적 IP 는 남는다. 멈춘 인스턴스에 붙은 탄력적 IP 와 디스크는 계속 과금)
- **완전히 끄기** (발표 뒤) : `deploy/aws/teardown.sh` → 도메인 A 레코드 삭제 → IAM 사용자 `guardrail-deploy` 의 접근 키 비활성화 · 삭제
- 비용 (서울, 대략) : t3.small 켜 둔 동안 시간당 요금 + gp3 20 GiB + 공인 IPv4 주소 시간당 요금. 예산 알림은 PM 이 콘솔에서 직접 켭니다 (DM-93 ⑥)
