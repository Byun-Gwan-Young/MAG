# C-19-10T React 화면 MAG 테마 되돌리기 — PM 명령서

React 화면의 색 · 글꼴 · 로고만 원래 MAG 테마 (`tools/demo_ui.py`) 로 되돌렸다. 화면 구조 · 문구 · 기능 · API · 라우팅 · Caddy · 백엔드는 그대로다. Claude Code 는 AWS · Google Cloud 에 접속하지 않았다. 순서는 C-19-10 명령서 ②~⑥ 과 같다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다.

C-19-10 과 달라진 점 : 로고 파일이 빌드에 하나 더 들어가서 ③ 의 `"frontend"` `"files"` 가 **4 → 5** 다 (`dist/assets/logo-….svg`). 로고는 `/assets/*` 아래라 로그인 전에도 열린다.

## ① 받기 · 확인

맥 (zsh)

```bash
git switch main
```

```bash
git pull --ff-only
```

```bash
venv/bin/python tools/run_offline_tests.py --output /tmp/c19-10t-tests.json
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
venv\Scripts\python.exe tools\run_offline_tests.py --output $env:TEMP\c19-10t-tests.json
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
npm test
```

```bash
npm run build
```

```bash
cd ..
```

Windows (PowerShell)

```powershell
cd frontend
```

```powershell
npm.cmd ci
```

```powershell
npm.cmd test
```

```powershell
npm.cmd run build
```

```powershell
cd ..
```

`npm test` 는 `pass 7 · fail 0` (새 테마 검사 4 개 포함). 빌드 결과에 `dist/assets/logo-….svg` 가 있어야 한다.

## ③ 묶음만 먼저 만들어 보기 (서버 접속 없음)

맥 (zsh)

```bash
venv/bin/python -B deploy/aws/bundle.py build --out /tmp/guardrail-bundle
```

Windows (PowerShell)

```powershell
venv\Scripts\python.exe -B deploy\aws\bundle.py build --out $env:TEMP\guardrail-bundle
```

볼 것 : `"problems": []`, `"frontend"` 의 `"files": 5` · `"reference_sessions": 67`, `"screen"` 의 `"sessions": 67` · `"path": "/legacy/"`. `problems` 에 `npm run build again` 이 있으면 ② 를 다시 한다.

## ④ 배포

인스턴스가 중지돼 있으면 EC2 콘솔에서 먼저 시작한다. SSH 가 `Operation timed out` 이면 맥에서 `deploy/aws/provision.sh --update-ssh-ip` 뒤 다시 한다.

맥 (zsh)

```bash
deploy/aws/deploy.sh
```

Windows (PowerShell) — Claude Code 가 시험하지 못했다. 막히면 맥에서 돌린다

```powershell
& "C:\Program Files\Git\bin\bash.exe" deploy/aws/deploy.sh
```

끝 줄이 `첫 화면 → /login 으로 보냄 · 로그인 화면 200` 이면 된다.

## ⑤ 터미널 확인 (로그인 없이)

맥 (zsh)

```bash
curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/
```

```bash
curl -sS -o /dev/null -w "%{http_code} %{content_type}\n" https://mag.ai.kr/login
```

```bash
curl -sS https://mag.ai.kr/login
```

Windows (PowerShell)

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/
```

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{content_type}\n" https://mag.ai.kr/login
```

```powershell
curl.exe -sS https://mag.ai.kr/login
```

기대 : 첫 줄 `303 https://mag.ai.kr/login`, 둘째 `200 text/html…`, 셋째 HTML 안에 `rel="icon"` 과 `/assets/logo-….svg` 가 있고 `fonts.googleapis.com` 은 없다.

## ⑥ 브라우저 확인

시크릿 창으로 연다. 기준 그림은 `reports/c19-10t/captures/` (왼쪽 원래 · 오른쪽 새 화면).

1. `https://mag.ai.kr/` → `/login`. 왼쪽 어두운 판 (`#1f1d1a`) 에 MAG 로고 (모자 쓴 인물), 오른쪽 베이지 바탕, 브라우저 탭에 같은 로고
2. Google 로그인 → 대시보드. 바탕 베이지, 왼쪽 메뉴 어두움, **선택된 메뉴 왼쪽 막대 · 아이콘이 주황**, 숫자는 그대로 (관측 54 · 탐지 8/9 · 절감 60.8% · p99 0.41 ms · 오탐 4/24). 초록은 "계속 진행" 표 · "API 연결" 점 같은 정상 표시에만
3. **세션 재생** : 타임라인 진전 하늘색 · 정체 주황 · STOP 짙은 갈색 · 차단 짙은 빨강, "자동 재생" 버튼이 진한 주황 바탕에 흰 글자
4. **비교** · **통계** (막대 주황) · **시스템 구조** · **사용 가이드** 가 열린다
5. **프로젝트** · **내 계정** 이 같은 테마. 로그아웃 → 로그인 화면
6. 휴대폰 폭 (창을 좁히기) 에서 메뉴 버튼으로 메뉴가 열린다

이상이 있으면 그 화면 · 주소 · 개발자 도구 콘솔의 빨간 줄을 캡처해 보고한다.
