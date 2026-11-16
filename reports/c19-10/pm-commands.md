# C-19-10 React 화면 배포 전환 — PM 명령서

서버 화면을 파이썬이 만든 데모 HTML (`tools/demo_ui.py`) 에서 `frontend/` React 앱으로 바꾼다 (DM-111 · DM-114 · DM-119). Claude Code 는 AWS · Google Cloud 에 접속하지 않았다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다.

```text
브라우저 ──HTTPS · 구글 로그인 먼저──▶ Caddy ─┬─ /api/auth/* · /api/projects* ──▶ 백엔드 127.0.0.1:8030
                                              ├─ /api/frontend/reference ───────▶ site/reference.json (묶음)
                                              ├─ /api/* · /docs ─────────────────▶ 조회 API 127.0.0.1:8020
                                              ├─ /assets/* ──────────────────────▶ React JS · CSS (로그인 전에도 열림)
                                              └─ 나머지 (/, /login, …) ───────────▶ React 화면 site/index.html
                                                                                   (/legacy/ = 옛 파이썬 화면)
```

바뀌는 것 :

- 서버에는 Node 를 깔지 않는다. **PM 컴퓨터에서 `npm run build` 한 `frontend/dist` 를 묶음에 넣어 보낸다.** dist 가 없으면 묶음을 만들지 않고, main 의 프론트 소스와 다르거나 소스보다 오래된 dist 도 묶음 검사에 걸린다 → 그러면 `npm run build` 를 다시 한다
- 로그인 화면도 React 다. 구글 클라이언트 ID 는 빌드에 넣지 않고 서버 (`/api/auth/config`) 에서 읽는다 → 지금 서버의 `backend.env` 값 그대로 쓴다
- 로그아웃은 새 API (`/api/auth/signout`) 로 한다
- 옛 화면은 지우지 않고 `https://mag.ai.kr/legacy/` 에 남긴다 (발표 대비, 로그인 뒤)

## ① 받기 · 확인

맥 (zsh)

```bash
git switch main
```

```bash
git pull --ff-only
```

```bash
venv/bin/python tools/run_offline_tests.py --output /tmp/c19-10-tests.json
```

```bash
venv/bin/python tools/final_freeze.py check
```

Windows (PowerShell)

```powershell
git switch main
```

```powershell
git pull --ff-only
```

```powershell
venv\Scripts\python.exe tools\run_offline_tests.py --output $env:TEMP\c19-10-tests.json
```

```powershell
venv\Scripts\python.exe tools\final_freeze.py check
```

테스트는 `failures 0 · errors 0`, `network_attempts: []`, 동결 확인은 `"ok": true` · `"differences": []` 여야 한다.

## ② 화면 빌드 (Node 22.12 이상)

맥 (zsh)

```bash
cd frontend
```

```bash
npm ci
```

```bash
npm run build
```

```bash
cd ..
```

Windows (PowerShell) — `npm` 대신 `npm.cmd` (실행 정책에 막히지 않게)

```powershell
cd frontend
```

```powershell
npm.cmd ci
```

```powershell
npm.cmd run build
```

```powershell
cd ..
```

`dist/index.html`, `dist/assets/index-….js` · `….css`, `dist/records.json` 이 나오면 된다. `frontend/dist` 와 `node_modules` 는 git 에서 제외되어 있다 (커밋하지 않는다).

## ③ 묶음만 먼저 만들어 보기 (서버 접속 없음)

맥 (zsh)

```bash
venv/bin/python -B deploy/aws/bundle.py build --out /tmp/guardrail-bundle
```

Windows (PowerShell)

```powershell
venv\Scripts\python.exe -B deploy\aws\bundle.py build --out $env:TEMP\guardrail-bundle
```

출력에서 볼 것 : `"problems": []`, `"bundle_version": "guardrail-server-bundle-v2"`, `"frontend"` 의 `"files": 4` · `"reference_sessions": 67`, `"screen"` 의 `"sessions": 67` · `"path": "/legacy/"`. `problems` 에 `npm run build again` 이나 `frontend source differs` 가 있으면 ② 를 다시 한다.

## ④ 배포

인스턴스가 중지돼 있으면 EC2 콘솔에서 먼저 시작한다. 장소가 바뀌어 SSH 가 막히면 (`Operation timed out`, DM-109) 맥에서 `deploy/aws/provision.sh --update-ssh-ip` 뒤 다시 한다.

맥 (zsh)

```bash
deploy/aws/deploy.sh
```

Windows (PowerShell) — Git for Windows 의 bash 로 같은 스크립트를 돌린다. 이 컴퓨터에 `deploy/aws/target.env` 와 SSH 키 파일이 있어야 한다 (없으면 Git Bash 에서 `deploy/aws/configure.sh`). **Windows 배포는 Claude Code 가 시험하지 못했다** — 막히면 맥에서 돌린다.

```powershell
& "C:\Program Files\Git\bin\bash.exe" deploy/aws/deploy.sh
```

`deploy.sh` 가 하는 일 : 묶음 (③ 과 같은 검사) → 서버로 보냄 → 서버 설치 (적재 · `load_demo check` → API · 백엔드 재시작 → **새 Caddyfile 을 `caddy validate` 로 먼저 검사**하고 통과해야 `/etc/caddy/Caddyfile` 을 바꿈 → Caddy 다시 읽기) → `https://mag.ai.kr/` 가 `/login` 으로 보내고 `/login` 이 200 인지 확인. 끝 줄이 `첫 화면 → /login 으로 보냄 · 로그인 화면 200` 이면 된다. `새 Caddyfile 검사 실패` 로 멈추면 서버의 Caddy 설정은 그대로이고, 그 출력을 그대로 보고한다.

## ⑤ 터미널 확인 (로그인 없이)

맥 (zsh)

```bash
curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/
```

```bash
curl -sS -o /dev/null -w "%{http_code} %{content_type}\n" https://mag.ai.kr/login
```

```bash
curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/summary
```

```bash
curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/frontend/reference
```

```bash
curl -sS https://mag.ai.kr/api/auth/config
```

Windows (PowerShell) — `curl` 이 아니라 `curl.exe`

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/
```

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{content_type}\n" https://mag.ai.kr/login
```

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/summary
```

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/frontend/reference
```

```powershell
curl.exe -sS https://mag.ai.kr/api/auth/config
```

기대 : 첫 줄 `303 https://mag.ai.kr/login`, 둘째 `200 text/html…`, 셋째 · 넷째 `303 https://mag.ai.kr/login` (로그인 없이는 데이터가 나오지 않는다), 다섯째 `{"google_client_id":"….apps.googleusercontent.com","login_uri":"https://mag.ai.kr/api/auth/google","open_signup":true}` (클라이언트 ID 는 비밀이 아니다. 비어 있으면 `deploy/aws/set-google-client.sh` 뒤 ④ 를 다시).

## ⑥ 브라우저 확인

시크릿 (사생활 보호) 창으로 연다.

1. `https://mag.ai.kr/` → 주소가 `/login` 으로 바뀌고 React 로그인 화면 ("실험 워크스페이스에 로그인") 과 Google 버튼이 보인다. "저장된 데모 먼저 살펴보기" 링크는 서버 화면에 없다 (정상)
2. Google 로그인 → 대시보드. 위 막대가 "DB 조회 API · 집계와 세션" 이고, 숫자가 지금 데모와 같다 : 관측 54 · 탐지 8/9 · 절감 60.8% · 판정 p99 0.41 ms · 오탐 4/24
3. **세션 재생** : 표에서 세션 하나 → 단계 이동 · 첫 STOP 이 보인다
4. **비교** · **통계** · **시스템 구조** · **사용 가이드** 가 열린다
5. **실시간 모니터** : "서버 게이트웨이 준비 중" (정상 — C-19-9 에서 연다)
6. **프로젝트** : 내 프로젝트 목록이 뜬다 (처음이면 비어 있음). 만들기는 서버에 기본 제공자 설정 (`GUARDRAIL_DEFAULT_PROVIDER`) 이 없으면 "서버 또는 DB" 오류가 난다 — 이번 단위 범위 밖 (보고서의 PM 결정 참고)
7. **API 키 관리** : "서버 API 미제공" (정상 — C-19-8K 에서 연다)
8. **내 계정** : 이름 · 이메일 · 활성 계정 → **로그아웃** → 로그인 화면. 뒤로 가기나 새로 고침을 해도 로그인 화면이다
9. 다시 로그인 → `https://mag.ai.kr/legacy/` 에 옛 화면이 뜬다 (발표 대비)
10. 휴대폰 폭 (창을 좁히기) 에서 메뉴 버튼으로 메뉴가 열린다

이상이 있으면 그 화면 · 주소 · 브라우저 개발자 도구 콘솔의 빨간 줄을 캡처해 보고한다. 발표 대비는 `/legacy/` 와 맥 기록 재생 (DM-92 ⑥) 이다.
