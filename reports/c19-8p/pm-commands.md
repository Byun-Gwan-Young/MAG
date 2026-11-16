# C-19-8P 프로젝트마다 LLM 제공자 고르기 — PM 명령서

프로젝트를 만들 때 **고객 요청을 넘겨줄 LLM 제공자**를 고르고, 프로젝트 화면에서 바꿀 수 있게 했다. 목록은 가이드라인 부록 C-19-8P 표 그대로 16 곳 + 학교 게이트웨이 (`kmu`) 다. 고객이 주소를 직접 넣는 길은 열지 않았다 (직접 입력 `custom` 은 계속 꺼져 있다). 이 단위는 **고르기 · 저장까지**다 — 실제로 그 주소로 요청을 넘기는 것은 C-19-9 게이트웨이다. Claude Code 는 AWS · Google Cloud 에 접속하지 않았고, 어느 제공자에도 요청을 보내지 않았다. 순서는 C-19-8K 명령서와 같다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다.

```text
브라우저 ──HTTPS · 구글 로그인 먼저──▶ Caddy ─┬─ /api/auth/* · /api/projects* · /api/keys/* · /api/providers ──▶ 백엔드 127.0.0.1:8030
                                              ├─ /api/frontend/reference ──────────────────────────────────────▶ site/reference.json
                                              ├─ /api/* · /docs ─────────────────────────────────────────────────▶ 조회 API 127.0.0.1:8020
                                              └─ 나머지 ─────────────────────────────────────────────────────────▶ React 화면
```

달라지는 것 :

- 새 API `GET /api/providers` : 제공자 id · 화면 이름 · 묶음 (해외 · 국내 · 팀 데모) · 쓸 수 있는지. **주소와 서버 설정값은 응답에 없다.** Caddy 가 이 경로를 백엔드로 보낸다 (로그인 먼저는 그대로)
- `POST /api/projects` 와 `PATCH /api/projects/{id}` 가 `provider` 를 받는다. 목록 밖 id · 빈 값 · `custom` · 주소는 422. `provider` 를 안 보내면 예전처럼 서버 기본값 (`kmu`)
- `kmu` 는 서버에 `GUARDRAIL_PROVIDER_KMU_URL` 이 있을 때만 고를 수 있다 (C-19-8K 배포가 이미 써 두었다). 없으면 화면에 "이 서버에서 사용할 수 없음" 으로 흐리게 나온다
- 고정 제공자 (OpenAI 등) 는 서버 환경 변수로 주소를 바꿀 수 없게 했다 — 저장되는 주소는 언제나 표의 주소다
- DB 스키마는 바뀌지 않는다 (`projects.provider` 는 이미 있다). 이미 있는 프로젝트는 지금 값 (`kmu`) 그대로

## ① 받기 · 확인

맥 (zsh)

```bash
git switch main
```

```bash
git pull --ff-only
```

```bash
venv/bin/python tools/run_offline_tests.py --output /tmp/c19-8p-tests.json
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
venv\Scripts\python.exe tools\run_offline_tests.py --output $env:TEMP\c19-8p-tests.json
```

```powershell
venv\Scripts\python.exe tools\final_freeze.py check
```

테스트는 `failures 0 · errors 0`, `network_attempts: []` (맥 기준 1,192 실행 · 1,174 통과 · 건너뜀 18), 동결 확인은 `"ok": true` · `"differences": []` 여야 한다.

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

`npm test` 는 `pass 10 · fail 0`.

## ③ 묶음만 먼저 만들어 보기 (서버 접속 없음)

맥 (zsh)

```bash
venv/bin/python -B deploy/aws/bundle.py build --out /tmp/guardrail-bundle
```

Windows (PowerShell)

```powershell
venv\Scripts\python.exe -B deploy\aws\bundle.py build --out $env:TEMP\guardrail-bundle
```

볼 것 : `"problems": []`. `problems` 에 `npm run build again` 이 있으면 ② 를 다시 한다.

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
curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/providers
```

Windows (PowerShell)

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/providers
```

기대 : `303 https://mag.ai.kr/login` (제공자 목록도 로그인 먼저).

## ⑥ 브라우저 확인

시크릿 창으로 연다. 계정 A (평소 계정) 와 계정 B (다른 구글 계정) 가 필요하다.

1. **목록** (계정 A) : 로그인 뒤 주소창에 `https://mag.ai.kr/api/providers` → 17 줄 (`openai` 부터 `kmu` 까지), 각 줄에 `id · name · group · available` 만 있고 `https://` 로 시작하는 주소가 **없다**. `kmu` 가 `"available":true` 다 (서버에 주소가 있으므로)
2. **만들기** : **프로젝트** → "프로젝트 만들기" 에 **LLM 제공자** 칸이 있고 처음 값이 `OpenAI (GPT)` → 펼치면 `해외` (OpenAI ~ Z.ai 14 곳) · `국내` (Upstage, 네이버) · `팀 데모` (학교 게이트웨이) 세 묶음 → `Anthropic (Claude)` 를 고르고 이름 `제공자 시험` → 프로젝트 만들기 → 목록에 `제공자 시험 · 관측 · Anthropic (Claude) · deadlock-v0.3`
3. **바꾸기** : 목록에서 `제공자 시험` 을 누르면 아래 "LLM 제공자" 칸에 `지금 Anthropic (Claude) 로 요청을 넘깁니다` → **바꿀 제공자** 에서 `네이버 (HyperCLOVA X)` → **제공자 저장** (같은 값일 때는 눌리지 않는다) → 목록이 `… · 네이버 (HyperCLOVA X) · …` → 새로 고침 (F5) 해도 그대로. "프로젝트 세션" 아래 `프로젝트 #숫자` 의 **숫자를 적어 둔다** (5 번)
4. **예전 프로젝트** : C-19-8K 확인 때 만든 `키 시험` 은 `학교 게이트웨이 (팀 데모)` 로 나온다 (그때 기본값)
5. **다른 계정에서 404** : 로그아웃 → 계정 B 로 로그인 → **프로젝트** 에 계정 A 의 `제공자 시험` 이 보이지 않는다. (주소창으로는 PATCH 를 보낼 수 없으므로 404 는 오프라인 테스트로 확인했다 — `tests/test_c19_8p_providers.py`)
6. **가이드** : **사용 가이드** 1 단계에 "LLM 제공자를 선택하세요", 3 단계에 "LLM 키는 프로젝트에서 고른 제공자의 키를 그대로 Authorization 헤더에 넣으세요 — MAG 는 그 키를 저장하지 않고 전달만 합니다" (3 단계의 `서버 게이트웨이 준비 중` 표시는 C-19-9 까지 그대로)
7. 휴대폰 폭 (창을 좁히기) 에서 프로젝트 만들기 · LLM 제공자 칸이 한 줄씩 쌓여 보인다 (캡처 `reports/c19-8p/captures/*-mobile.png` 와 같은 모양)

이상이 있으면 그 화면 · 주소 · 개발자 도구 콘솔의 빨간 줄을 캡처해 보고한다. **LLM 키 · 가드레일 키 원문은 캡처 · 보고에 넣지 않는다.**
