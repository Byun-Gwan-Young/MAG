# C-19-9b 실시간 모니터 보완 — PM 명령서

DM-120 : 실시간 모니터 · 세션 재생에 ① step 별 이번 토큰 · 누적 토큰과 누적 막대 (STOP 지점 표시, STOP 이후 구간 회색) ② step 마다 판정 근거 한 줄 ③ 프로젝트별 발화 기록 (기본 꺼짐, 켠 프로젝트만 LLM 응답 앞 1,000 자를 7 일) 을 더한다. Claude Code 는 AWS · Google Cloud · 학교 게이트웨이에 접속하지 않았고 실제 세션 · LLM 호출을 하지 않았다. 아래는 모두 **PM 컴퓨터의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. 맥은 zsh, Windows 는 PowerShell 이다. 짧은 세션 (⑥) 은 C-19-9 ⑤ 처럼 맥에서만 한다.

달라지는 것 : DB 에 `projects.utterance_log` (기본 `false`) 칸과 `utterances` 표가 생긴다. 배포 때 `python -m storage init` 이 더하기만 한다 (기존 데이터를 지우거나 바꾸지 않는다). 판정 · STOP 원인 문장 · 동결 파일은 그대로다. 발화 기록은 프로젝트 설정에서 켠 프로젝트만 저장하고, 끄는 순간 그 프로젝트의 저장분을 지운다. 7 일이 지난 기록은 게이트웨이가 쓸 때와 화면이 읽을 때 지운다 (서버 타이머는 없다).

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

테스트는 `failures 0 · errors 0`, `network_attempts: []` (맥 기준 1,279 실행 · 1,261 통과 · 건너뜀 18, `reports/c19-9b/tests.json`), 동결 확인은 `"ok": true` · `"differences": []`.

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

이상이 있으면 그 화면 · 주소 · 명령 출력을 캡처해 보고한다. **LLM 키 · MAG 키 원문은 캡처 · 보고에 넣지 않는다.**
