# C-19-8K API 키 관리 · 프로젝트 모드 — PM 명령서

프로젝트마다 가드레일 API 키를 발급 · 조회 · 폐기하고, 프로젝트 모드 (관측 · 차단) 를 바꾸는 API 와 화면을 붙였다. 화면 이름은 **MAG** (부제 Multi-Agent Guardrail) 로 바꿨다. Claude Code 는 AWS · Google Cloud 에 접속하지 않았다. 순서는 C-19-10 명령서 ②~⑥ 과 같다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다.

```text
브라우저 ──HTTPS · 구글 로그인 먼저──▶ Caddy ─┬─ /api/auth/* · /api/projects* · /api/keys/* ──▶ 백엔드 127.0.0.1:8030
                                              ├─ /api/frontend/reference ──────────────────────▶ site/reference.json
                                              ├─ /api/* · /docs ─────────────────────────────────▶ 조회 API 127.0.0.1:8020
                                              └─ 나머지 ─────────────────────────────────────────▶ React 화면
```

달라지는 것 :

- **배포가 `/etc/guardrail/backend.env` 에 두 줄을 더 쓴다** : `GUARDRAIL_DEFAULT_PROVIDER=kmu` (PM 결정, 11/17 01 시 55 분) 와 `GUARDRAIL_PROVIDER_KMU_URL=https://factchat.mindlogic-kr-api.com/v1/gateway` (데모 프록시 설정 `guardrail-deadlock-v0.3-kmu.yaml` 의 업스트림과 같은 값). `kmu` 는 저장 계층에 내장 주소가 없어 이름만으로는 프로젝트를 만들 수 없어서 주소 줄도 같이 쓴다. 이미 있는 값은 그대로 둔다. 비밀값이 아니다 (키 없음). 이제 **프로젝트 만들기가 된다**
- 새 API : `GET · POST /api/projects/{id}/keys`, `POST /api/keys/{id}/revoke`, `PATCH /api/projects/{id}`. Caddy 가 `/api/keys/*` 를 백엔드로 보낸다
- 키 원문은 **발급 응답에 한 번만** 온다. DB 에는 SHA-256 과 앞자리 8 자만 남는다. 남의 프로젝트 · 키는 404, 공용 데모 프로젝트는 403
- 키로 모델 요청을 받는 서버 게이트웨이는 아직 없다 (C-19-9). 그래서 지금은 키의 "마지막 사용" 이 계속 "사용 전" 이다. 가이드 3 단계에 "서버 게이트웨이 준비 중" 이 붙어 있다

## ① 받기 · 확인

맥 (zsh)

```bash
git switch main
```

```bash
git pull --ff-only
```

```bash
venv/bin/python tools/run_offline_tests.py --output /tmp/c19-8k-tests.json
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
venv\Scripts\python.exe tools\run_offline_tests.py --output $env:TEMP\c19-8k-tests.json
```

```powershell
venv\Scripts\python.exe tools\final_freeze.py check
```

테스트는 `failures 0 · errors 0`, `network_attempts: []` (맥 기준 1,175 실행 · 1,157 통과 · 건너뜀 18), 동결 확인은 `"ok": true` · `"differences": []` 여야 한다.

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

`npm test` 는 `pass 10 · fail 0` (이름 MAG 검사 3 개 포함).

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

끝 줄이 `첫 화면 → /login 으로 보냄 · 로그인 화면 200` 이면 된다. 도중에 `backend.env 의 GUARDRAIL_DEFAULT_PROVIDER 모양이 아니다` 로 멈추면 서버의 그 줄이 손으로 바뀐 것이다 — 그 줄을 지우고 다시 배포하면 `kmu` 가 들어간다.

서버의 새 두 줄 확인 (맥, 선택) — 먼저 서버에 들어간다

```bash
deploy/aws/remote.sh ssh
```

서버 안에서

```bash
sudo grep -E '^GUARDRAIL_(DEFAULT_PROVIDER|PROVIDER_KMU_URL)=' /etc/guardrail/backend.env
```

```bash
exit
```

기대 : `GUARDRAIL_DEFAULT_PROVIDER=kmu`, `GUARDRAIL_PROVIDER_KMU_URL=https://factchat.mindlogic-kr-api.com/v1/gateway`.

## ⑤ 터미널 확인 (로그인 없이)

맥 (zsh)

```bash
curl -sS -o /dev/null -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/projects/1/keys
```

```bash
curl -sS https://mag.ai.kr/login
```

Windows (PowerShell)

```powershell
curl.exe -sS -o NUL -w "%{http_code} %{redirect_url}\n" https://mag.ai.kr/api/projects/1/keys
```

```powershell
curl.exe -sS https://mag.ai.kr/login
```

기대 : 첫 줄 `303 https://mag.ai.kr/login` (키 API 도 로그인 먼저), 둘째 HTML 의 `<title>` 이 `MAG · Multi-Agent Guardrail`.

## ⑥ 브라우저 확인

시크릿 창으로 연다. 계정 A (평소 계정) 와 계정 B (다른 구글 계정) 가 필요하다.

1. **이름** : 로그인 화면 왼쪽 위 `MAG` · `Multi-Agent Guardrail`, 브라우저 탭 `MAG · Multi-Agent Guardrail`. 로그인 뒤 왼쪽 메뉴 위도 `MAG`, 화면 제목 위 작은 글자 `MAG / DASHBOARD`, 맨 아래 줄 `MAG · Multi-Agent Guardrail · 캡스톤 실험`
2. **프로젝트 만들기** (계정 A) : **프로젝트** → 이름 `키 시험` · 관측 → 프로젝트 만들기 → 목록에 `키 시험 · 관측 · deadlock-v0.3`. 전에는 여기서 503 이었다
3. **모드 바꾸기** : 목록에서 `키 시험` 을 누르면 아래 "프로젝트 모드" 에 관측 · 차단 두 칸 → **차단** 을 누른다 → 테두리가 차단으로 옮겨 가고 목록이 `차단 · …` → 새로 고침 (F5) 해도 차단 그대로. 다시 **관측** 으로 돌려 둔다. 이 화면의 "프로젝트 세션" 아래 `프로젝트 #숫자` 의 **숫자를 적어 둔다** (아래 6 번)
4. **키 발급 · 원문 한 번만** : **API 키 관리** → `키 시험` 고르기 → 키 이름 `시험 키` → 키 발급 → 주황 상자에 `gk_` 로 시작하는 긴 키와 "창을 닫으면 다시 볼 수 없습니다" → **복사** 가 `복사됨` 으로 바뀐다 (메모장에 붙여 넣어 보고 지운다) → **확인했습니다** 를 누르면 상자가 사라지고 키 원문은 화면 어디에도 없다. 아래 "발급한 키" 표에는 `시험 키 · gk_xxxxxxxx_… · 만든 날 · 사용 전 · 폐기 버튼` 만. 새로 고침해도 원문은 다시 나오지 않는다
5. **폐기** : 표의 **폐기** → 확인 창 → 상태가 빨간 `폐기 날짜` 로 바뀐다. 새로 고침해도 그대로. (폐기한 키로 보낸 요청이 401 인지는 게이트웨이가 생기는 C-19-9 에서 본다)
6. **다른 계정에서 404** : 로그아웃 → 계정 B 로 로그인 → 주소창에 `https://mag.ai.kr/api/projects/숫자/keys` (3 번에서 적은 숫자) → `{"detail":"Not found"}`. **프로젝트** · **API 키 관리** 에 계정 A 의 `키 시험` 이 보이지 않는다
7. **가이드** : **사용 가이드** 2 단계 "API 키 발급" 에 "구현 전" · "API 가 없습니다" 문구가 없고 "바로 가기" 링크가 키 화면으로 간다. 3 단계 "모델 호출 주소 연결" 에 `서버 게이트웨이 준비 중` 표시
8. 휴대폰 폭 (창을 좁히기) 에서 메뉴 버튼 → **API 키 관리** 가 열리고 발급 상자의 긴 키가 줄바꿈되어 보인다

이상이 있으면 그 화면 · 주소 · 개발자 도구 콘솔의 빨간 줄을 캡처해 보고한다. **키 원문은 캡처 · 보고에 넣지 않는다.**
