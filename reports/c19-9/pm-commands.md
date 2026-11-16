# C-19-9 서비스 게이트웨이 — PM 명령서

고객 에이전트가 **MAG API 키** (`X-Guardrail-Key`) 로 `https://mag.ai.kr/v1/chat/completions` 를 부르면, 서버의 게이트웨이 (`service/gateway.py`, 127.0.0.1:8050) 가 키로 프로젝트를 찾고, 그 프로젝트의 제공자 주소로만 넘기며 (`kmu` 는 서버 설정 `GUARDRAIL_PROVIDER_KMU_URL`), 맥 프록시와 같은 판정 (관측 · 차단) 을 하고, 기록을 DB 에 쓴다. 대시보드 (**실시간 모니터**) 는 그 DB 기록을 읽는다. LLM 키 (`Authorization`) 는 전달만 하고 어디에도 남기지 않는다. Claude Code 는 AWS · Google Cloud · 학교 게이트웨이에 접속하지 않았고 실제 세션을 돌리지 않았다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다. 세션 실행 (⑥ · ⑧) 은 C-19-4 · C-19-7 처럼 맥에서만 한다.

```text
고객 에이전트 ──HTTPS · MAG API 키──▶ Caddy ─┬─ /v1/* · /sessions* · /log · /conditions ──▶ 게이트웨이 127.0.0.1:8050 ──▶ 프로젝트의 제공자 (kmu = 학교 게이트웨이)
브라우저 ──HTTPS · 구글 로그인 먼저──▶ Caddy ─┼─ /api/live/* · /api/auth/* · /api/projects* · … ──▶ 백엔드 127.0.0.1:8030
                                              └─ (그 밖은 C-19-8P 와 같다)
```

달라지는 것 : 서버에 `guardrail-gateway` 서비스가 하나 는다 (백엔드와 같은 `backend.env` 를 읽는다). 게이트웨이 경로는 로그인 대신 키로 막는다 (키 없음 · 폐기된 키 401). Caddy 는 응답을 660 초까지 기다린다 (프록시의 업스트림 대기 600 초보다 길게). 사용 가이드 3 · 4 단계의 `서버 게이트웨이 준비 중` 표시가 없어지고 실제 주소가 나온다.

## ① 받기 · 확인

맥 (zsh)

```bash
git switch main
```

```bash
git pull --ff-only
```

```bash
venv/bin/python tools/run_offline_tests.py --output /tmp/c19-9-tests.json
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
venv\Scripts\python.exe tools\run_offline_tests.py --output $env:TEMP\c19-9-tests.json
```

```powershell
venv\Scripts\python.exe tools\final_freeze.py check
```

테스트는 `failures 0 · errors 0`, `network_attempts: []` (맥 기준 1,223 실행 · 1,205 통과 · 건너뜀 18), 동결 확인은 `"ok": true` · `"differences": []`.

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

## ③ 묶음 (서버 접속 없음)

맥 (zsh)

```bash
venv/bin/python -B deploy/aws/bundle.py build --out /tmp/guardrail-bundle
```

Windows (PowerShell)

```powershell
venv\Scripts\python.exe -B deploy\aws\bundle.py build --out $env:TEMP\guardrail-bundle
```

볼 것 : `"problems": []`. 이번부터 묶음에 `proxy.py` 와 판정 모듈 · 데모 설정이 들어간다 (게이트웨이가 서버 안에서 쓴다). 실행기 · 배치 실행기는 여전히 들어가지 않는다.

## ④ 배포

인스턴스가 중지돼 있으면 EC2 콘솔에서 먼저 시작한다.

맥 (zsh)

```bash
deploy/aws/deploy.sh
```

Windows (PowerShell) — Claude Code 가 시험하지 못했다. 막히면 맥에서 돌린다

```powershell
& "C:\Program Files\Git\bin\bash.exe" deploy/aws/deploy.sh
```

설치 출력에 `게이트웨이 응답 확인` 이 있고 끝 줄이 `배포 끝` 이면 된다. `새 Caddyfile 검사 실패` 가 나오면 Caddy 는 이전 설정 그대로다 — 그 출력을 보고한다.

## ⑤ 서버 연결 확인

**5-1. 키 없이 (터미널)**

맥 (zsh)

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST https://mag.ai.kr/v1/chat/completions
```

Windows (PowerShell)

```powershell
curl.exe -sS -o NUL -w "%{http_code}\n" -X POST https://mag.ai.kr/v1/chat/completions
```

기대 : `401` (로그인 화면으로 가는 303 이 아니다 — 키로 막는 경로).

**5-2. 발표용 프로젝트 · 키 (브라우저)** : 로그인 → **프로젝트** → 프로젝트 만들기 : 이름 `발표 차단`, LLM 제공자 `학교 게이트웨이 (팀 데모)` → 목록에서 `발표 차단` → 프로젝트 모드 **차단** → **API 키 관리** → `발표 차단` 고르기 → 키 이름 `발표 키` → 키 발급 → **복사** (아직 **확인했습니다** 를 누르지 않아도 된다. 키는 아래 5-3 첫 명령으로 바로 옮긴다).

**5-3. 짧은 요청 1 회 (맥 → 서버 → 학교 게이트웨이)** — 맥 (zsh), 창 1

```bash
export GUARDRAIL_GATEWAY_KEY="$(pbpaste)"
```

```bash
echo ${#GUARDRAIL_GATEWAY_KEY}
```

기대 : `55`. 이제 학교 게이트웨이 키 (계정 A) 를 복사한다.

```bash
export KMU_API_KEY="$(pbpaste)"
```

```bash
echo ${#KMU_API_KEY}
```

0 이 아니면 된다.

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/conditions -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY"
```

기대 : `200`.

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: conn-check-1" --data-binary @reports/c19-9/conn-check.json
```

기대 : `200` (서버가 학교 게이트웨이에 닿았다). `401` 은 MAG 키, `403` + 응답에 `upstream_not_allowed` 는 서버의 `GUARDRAIL_PROVIDER_KMU_URL`, `502` · `504` 는 서버 → 학교 게이트웨이 연결 문제다. 응답 본문 (모델 문장) 은 출력하지 않는다. 이 요청은 대시보드에 `발표 차단 · conn-check-1` (1 step, 진행 중) 으로 남는다.

## ⑥ 발표용 세션 1 회 — 서버로 (맥, 창 1 그대로)

리허설 계획 `batch_plans/demo-live-rehearsal-v1.yaml` 레인 1 (CT 교착판 차단, 약 9~13 분 · 55~57 크레딧, 교착이 안 나면 30 step 까지 가고 더 든다). 발표 당일에는 같은 명령에서 계획 경로만 `batch_plans/demo-live-v1.yaml` 이다.

```bash
venv/bin/python -B tools/final_freeze.py check
```

```bash
venv/bin/python -B tools/run_batch.py run --plan batch_plans/demo-live-rehearsal-v1.yaml --lane 1 --lanes 2 --account-label A --block-proxy https://mag.ai.kr
```

시작 전에 배치 실행기가 서버의 `/conditions` 를 읽어 조건 (차단 · deadlock-v0.3 · 동결 `4fa93919…` · 고정 모델 `glm-5.3-flash`) 을 확인한다. 다르면 시작하지 않는다 — 그 줄을 보고한다.

```bash
unset KMU_API_KEY GUARDRAIL_GATEWAY_KEY
```

## ⑦ 대시보드 확인 (브라우저, ⑥ 이 도는 동안)

**실시간 모니터** → 제목 아래 `서비스 게이트웨이 기록 (DB)` → 맨 위 줄 `발표 차단 · <세션 id>` 가 "진행 중", step 이 늘어난다 → STOP 이 나면 알림 카드에 원인 문장과 "차단 모드 : N단계의 모델 요청을 업스트림으로 보내지 않고 403 으로 돌려주었습니다" → 세션을 누르면 step 표 끝에 `차단 (403)` 칸. 캡처 `reports/c19-9/captures/live-monitor-desktop.png` 와 같은 모양이다 (캡처는 가짜 데이터). **사용 가이드** 3 단계에 `https://mag.ai.kr/v1` 과 `X-Guardrail-Key`, 4 단계에 `https://mag.ai.kr/sessions/observations`, `서버 게이트웨이 준비 중` 표시는 없다. 다른 구글 계정으로 로그인하면 이 세션이 보이지 않는다.

## ⑧ 대비 경로 — 맥 프록시 (서버가 안 될 때)

⑤ 나 ⑥ 이 막히면 C-19-7 리허설 절과 같은 맥 프록시로 돈다. 서버 키는 쓰지 않는다 (`GUARDRAIL_GATEWAY_KEY` 가 없으면 실행기는 예전과 똑같이 돈다).

창 M — 맥 모니터

```bash
venv/bin/python -B tools/demo_live.py serve
```

```bash
open http://127.0.0.1:8010/#live
```

창 Q — 차단 프록시 (포트 8001)

```bash
GUARDRAIL_CONFIG=guardrail-deadlock-v0.3-kmu-block.yaml venv/bin/python -m uvicorn proxy:app --host 127.0.0.1 --port 8001 --log-level info
```

창 2 (계정 B)

```bash
export KMU_API_KEY="$(pbpaste)"
```

```bash
echo ${#KMU_API_KEY}
```

```bash
venv/bin/python -B tools/run_batch.py run --plan batch_plans/demo-live-rehearsal-v1.yaml --lane 2 --lanes 2 --account-label B
```

```bash
unset KMU_API_KEY
```

모니터 화면 (`127.0.0.1:8010`) 에서 STOP 알림과 403 칸을 본다. 끝나면 창 Q · 창 M 을 Ctrl+C 로 끈다. 그래도 안 되면 **기록 재생** 화면을 쓴다.

이상이 있으면 그 화면 · 주소 · 명령 출력을 캡처해 보고한다. **LLM 키 · MAG 키 원문은 캡처 · 보고에 넣지 않는다.**
