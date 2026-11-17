# C-19-9b · C-19-9c 실시간 모니터 보완 · 폐기 키 숨김 · 프로젝트 삭제 — PM 명령서 (한 번에 배포)

DM-120 : 실시간 모니터 · 세션 재생에 ① step 별 이번 토큰 · 누적 토큰과 누적 막대 (STOP 지점 표시, STOP 이후 구간 회색) ② step 마다 판정 근거 한 줄 ③ 프로젝트별 발화 기록 (기본 꺼짐, 켠 프로젝트만 LLM 응답 앞 1,000 자를 7 일) 을 더한다. DM-121 (C-19-9c) : ④ 폐기한 키는 키 목록에서 사라지고 ⑤ 프로젝트 설정에서 프로젝트를 삭제할 수 있다 (키 모두 폐기 · 세션 · step · 판정 · 발화 기록 삭제). **9b 와 9c 를 이 명령서 하나로 한 번에 배포 · 확인한다.** Claude Code 는 AWS · Google Cloud · 학교 게이트웨이에 접속하지 않았고 실제 세션 · LLM 호출을 하지 않았다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다. 짧은 세션 (⑥ · ⑪) 과 `/v1` 확인 (⑩ · ⑫) 은 C-19-9 ⑤ 처럼 맥에서만 한다.

달라지는 것 : DB 에 `projects.utterance_log` (기본 `false`) 칸과 `utterances` 표가 생긴다. 배포 때 `python -m storage init` 이 더하기만 한다 (기존 데이터를 지우거나 바꾸지 않는다). 판정 · STOP 원인 문장 · 동결 파일은 그대로다. 발화 기록은 프로젝트 설정에서 켠 프로젝트만 저장하고, 끄는 순간 그 프로젝트의 저장분을 지운다. 7 일이 지난 기록은 게이트웨이가 쓸 때와 화면이 읽을 때 지운다 (서버 타이머는 없다).

C-19-9c 로 달라지는 것 : DB 에 `projects.deleted_at` 칸이 생긴다 (역시 `storage init` 이 더하기만 한다). 키 목록 (`GET /api/projects/{id}/keys`) 은 살아 있는 키만 돌려준다 — 폐기한 키의 기록 (앞자리 · 해시 · 폐기 시각) 과 `auth_events` 는 DB 에 그대로 남고 그 키는 계속 거절된다. 프로젝트 삭제 (`DELETE /api/projects/{id}`) 는 한 트랜잭션에서 그 프로젝트의 키를 모두 폐기하고, 세션 · 관측 · 판정 · LLM 호출 · 발화 · 시나리오를 지우고, `auth_events` 에 `project_deleted` 한 줄 (프로젝트 이름 · 개수, 내용 없음) 을 남긴다. 프로젝트 행은 `deleted_at` 이 찍힌 채 남는다 — 폐기한 키 기록이 이 행을 가리키기 때문이고, 목록 · 키 · 게이트웨이 · 모니터 어디에도 나오지 않는다. 계정 최종 삭제 때 이 행과 키 기록도 함께 지워진다. 데모 프로젝트는 삭제할 수 없다 (403).

## ① 받기 · 확인

맥 (zsh)

```bash
git switch main
```

```bash
git pull --ff-only
```

```bash
venv/bin/python tools/run_offline_tests.py --output /tmp/c19-9b-tests.json
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
venv\Scripts\python.exe tools\run_offline_tests.py --output $env:TEMP\c19-9b-tests.json
```

```powershell
venv\Scripts\python.exe tools\final_freeze.py check
```

테스트는 `failures 0 · errors 0`, `network_attempts: []` (맥 기준 1,327 실행 · 1,308 통과 · 건너뜀 19, `reports/c19-9c/tests.json`), 동결 확인은 `"ok": true` · `"differences": []`.

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

`npm test` 는 `pass 12 · fail 0`.

## ③ 묶음 (서버 접속 없음)

맥 (zsh)

```bash
venv/bin/python -B deploy/aws/bundle.py build --out /tmp/guardrail-bundle
```

Windows (PowerShell)

```powershell
venv\Scripts\python.exe -B deploy\aws\bundle.py build --out $env:TEMP\guardrail-bundle
```

볼 것 : `"problems": []`.

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

설치 출력의 `DB : storage init → storage demo-all → service.load_demo check` 단계가 오류 없이 지나고 `게이트웨이 응답 확인` 이 있고 끝 줄이 `배포 끝` 이면 된다.

## ⑤ 발화 기록 켜기 (브라우저)

로그인 → **프로젝트** → 목록에서 `발표 차단` (C-19-9 에서 만든 차단 프로젝트) → **발화 기록** 칸이 `꺼짐 (기본)` 인지 본다 → **켜기** → "발화 기록을 켰습니다" 알림. 설명 문장 "켜면 에이전트 응답 본문 앞부분을 7 일 동안 저장해 판정 근거와 함께 보여 줍니다. LLM 키와 추론 내용은 저장하지 않습니다." 가 보인다. 다른 프로젝트는 그대로 꺼져 있다.

`발표 차단` 의 키가 손에 없으면 **API 키 관리** 에서 새로 발급하고 **복사** 한다 (C-19-9 ⑤-2).

## ⑥ 짧은 세션 — 3 step (맥, 창 1)

모델 요청 3 회와 관측 3 회. 1 분 안에 끝나고 크레딧은 연결 확인 3 회 분량이다. step 1 은 기준점, step 2 는 파일 · 테스트 변화 (진전), step 3 은 변화 없음 (정체 1/3) 이 되도록 관측 파일을 미리 만들어 두었다 (`reports/c19-9b/obs-1.json` ~ `obs-3.json`, 모델 문장 없음).

MAG 키를 복사한 뒤

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
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9b-check-1" --data-binary @reports/c19-9b/chat.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/sessions/observations -H "Content-Type: application/json" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9b-check-1" --data-binary @reports/c19-9b/obs-1.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9b-check-1" --data-binary @reports/c19-9b/chat.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/sessions/observations -H "Content-Type: application/json" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9b-check-1" --data-binary @reports/c19-9b/obs-2.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9b-check-1" --data-binary @reports/c19-9b/chat.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/sessions/observations -H "Content-Type: application/json" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9b-check-1" --data-binary @reports/c19-9b/obs-3.json
```

```bash
unset KMU_API_KEY GUARDRAIL_GATEWAY_KEY
```

기대 : 여섯 줄 모두 `200`. 같은 세션 키로 다시 돌리면 `409` 가 나오니, 다시 할 때는 세 줄의 `c19-9b-check-1` 을 `c19-9b-check-2` 로 바꾼다. 응답 본문 (모델 문장) 은 터미널에 출력하지 않는다.

## ⑦ 화면 확인 (브라우저)

**실시간 모니터** → `발표 차단 · c19-9b-check-1` 을 누른다.

- 누적 막대 아래 step 표에 `이번 토큰` · `누적 토큰` · `판정 근거` 칸. 누적 토큰은 step 마다 늘어난다. 학교 게이트웨이 응답에 usage 가 없으면 그 칸은 `측정 안 됨` 이다 (0 이 아니다)
- 판정 근거 : step 1 `기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다`, step 2 `새 파일 내용 1 개 · 통과 테스트 7 → 8`, step 3 `파일 변화 없음 · 통과 테스트 8/16 그대로 · 새 쟁점 없음 → 진전 없는 차례 1/3`
- 각 줄 오른쪽 **펼치기** → 역할 · 발화 앞부분 (연결 확인에 대한 모델의 한 단어 답) · 판정 근거

**세션 재생** → 아무 데모 세션 → 같은 표가 기록된 토큰으로 채워져 있고, STOP 이 있는 세션은 막대에 `STOP` 표시와 그 뒤 회색 구간이 있다. 펼치면 발화 앞부분 자리에 "데모 기록은 발화를 표시하지 않습니다." 가 나온다.

캡처 `reports/c19-9b/captures/` (가짜 데이터) 와 같은 모양이다.

## ⑧ 끄기 → 저장분 사라짐 (브라우저)

**프로젝트** → `발표 차단` → **발화 기록** → **끄기** → "발화 기록을 껐습니다. 이 프로젝트에 저장된 발화를 모두 지웠습니다." → **실시간 모니터** → `c19-9b-check-1` → 펼치기 → 발화 앞부분 자리에 "이 프로젝트는 발화 기록이 꺼져 있어 응답 본문을 저장하지 않았습니다." 가 나온다. 다시 **켜기** 를 눌러도 지운 발화는 돌아오지 않는다 (⑥ 을 새 세션 키로 다시 돌린 것부터 다시 쌓인다). 발표 전에는 꺼 두는 것이 기본값이다.

## ⑨ 시험 프로젝트 · 키 2 개 (브라우저 + 맥 창 1) — 여기부터 C-19-9c

`발표 차단` 은 건드리지 않고, 지워도 되는 시험 프로젝트를 새로 만든다. 키 원문은 발급할 때 한 번만 보이므로 발급하자마자 창 1 에 넣는다.

**프로젝트** → **프로젝트 만들기** : 이름 `c19-9c 삭제 시험`, 실행 모드 `관측`, LLM 제공자 `학교 게이트웨이 (팀 데모)` → **프로젝트 만들기**. **API 키 관리** → 프로젝트 고르기에서 `c19-9c 삭제 시험` → 키 이름 `삭제 시험` 으로 **키 발급** → **복사**.

```bash
export GUARDRAIL_GATEWAY_KEY="$(pbpaste)"
```

```bash
echo ${#GUARDRAIL_GATEWAY_KEY}
```

기대 : `55`. 브라우저에서 **확인했습니다** → 키 이름 `폐기 시험` 으로 하나 더 **키 발급** → **복사**.

```bash
export GUARDRAIL_REVOKED_KEY="$(pbpaste)"
```

```bash
echo ${#GUARDRAIL_REVOKED_KEY}
```

기대 : `55`. 브라우저에서 **확인했습니다**. 발급한 키 표에 두 줄이 있다.

## ⑩ 키 폐기 → 목록에서 사라짐 · `/v1` 401

브라우저 **API 키 관리** → `c19-9c 삭제 시험` → `폐기 시험` 줄 **폐기** → 확인 창 **확인** → 그 줄이 바로 사라지고 `'폐기 시험' 키를 폐기했습니다.` 알림이 한 번 나온다. 표에는 `삭제 시험` 한 줄만 남는다 (예전처럼 "폐기 날짜" 배지로 남지 않는다). 새로 고쳐도 같다.

맥 창 1 :

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "X-Guardrail-Key: $GUARDRAIL_REVOKED_KEY" -H "X-Guardrail-Session: c19-9c-revoked-1" --data-binary @reports/c19-9b/chat.json
```

```bash
unset GUARDRAIL_REVOKED_KEY
```

기대 : `401`. 키 확인에서 막히므로 학교 게이트웨이까지 가지 않는다 (LLM 키를 넣지 않았고 크레딧도 쓰지 않는다).

## ⑪ 남은 키로 짧은 세션 1 step (맥, 창 1)

학교 게이트웨이 키 (계정 A) 를 복사한다.

```bash
export KMU_API_KEY="$(pbpaste)"
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9c-check-1" --data-binary @reports/c19-9b/chat.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/sessions/observations -H "Content-Type: application/json" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9c-check-1" --data-binary @reports/c19-9b/obs-1.json
```

기대 : 두 줄 모두 `200` (모델 요청 1 회). 브라우저 **실시간 모니터** 에 `c19-9c 삭제 시험 · c19-9c-check-1` 이 보인다. 그 세션을 열고 주소창 주소를 복사해 둔다.

## ⑫ 시험 프로젝트 삭제 → 목록 · 모니터에서 사라짐 · `/v1` 401

브라우저 **프로젝트** → `c19-9c 삭제 시험` → 맨 아래 **프로젝트 삭제** 칸 → **프로젝트 삭제** → 확인 창. 이름을 한 글자라도 다르게 넣으면 버튼이 꺼져 있다. `c19-9c 삭제 시험` 을 그대로 넣으면 켜진다 → **프로젝트 삭제** → `'c19-9c 삭제 시험' 프로젝트를 삭제했습니다. …` 알림, 목록에서 사라짐. `발표 차단` 은 그대로 있다.

- **API 키 관리** 의 프로젝트 고르기에도 없다
- **실시간 모니터** 목록에서 `c19-9c-check-1` 이 사라진다. ⑪ 에서 복사해 둔 세션 주소를 다시 열면 "찾을 수 없습니다" (404)

맥 창 1 — 삭제 전과 같은 세션으로 한 번 더 :

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9c-check-1" --data-binary @reports/c19-9b/chat.json
```

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/sessions/observations -H "Content-Type: application/json" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9c-check-1" --data-binary @reports/c19-9b/obs-1.json
```

기대 : 두 줄 모두 `200` (모델 요청 1 회). 브라우저 **실시간 모니터** 에 `c19-9c 삭제 시험 · c19-9c-check-1` 이 보인다. 그 세션을 열어 둔 주소창 주소를 기억해 둔다.

## ⑫ 시험 프로젝트 삭제 → 목록 · 모니터에서 사라짐 · `/v1` 401

브라우저 **프로젝트** → `c19-9c 삭제 시험` → 맨 아래 **프로젝트 삭제** 칸 → **프로젝트 삭제** → 확인 창. 이름을 한 글자라도 다르게 넣으면 버튼이 꺼져 있다. `c19-9c 삭제 시험` 을 그대로 넣으면 켜진다 → **프로젝트 삭제** → `'c19-9c 삭제 시험' 프로젝트를 삭제했습니다. …` 알림, 목록에서 사라짐. `발표 차단` 은 그대로 있다.

- **API 키 관리** 의 프로젝트 고르기에도 없다
- **실시간 모니터** 목록에서 `c19-9c-check-1` 이 사라진다. ⑪ 에서 열어 둔 세션 주소를 다시 열면 "찾을 수 없습니다" (404)

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://mag.ai.kr/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $KMU_API_KEY" -H "X-Guardrail-Key: $GUARDRAIL_GATEWAY_KEY" -H "X-Guardrail-Session: c19-9c-check-1" --data-binary @reports/c19-9b/chat.json
```

```bash
unset KMU_API_KEY GUARDRAIL_GATEWAY_KEY
```

기대 : `401` (삭제와 함께 키가 폐기돼 학교 게이트웨이까지 가지 않는다). 실행 중이던 세션의 다음 요청도 이렇게 거절된다.

캡처 `reports/c19-9c/captures/` (키 목록 · 삭제 확인 창 · 삭제 뒤 목록, 가짜 데이터) 와 같은 모양이다.

이상이 있으면 그 화면 · 주소 · 명령 출력을 캡처해 보고한다. **LLM 키 · MAG 키 원문은 캡처 · 보고에 넣지 않는다.**
