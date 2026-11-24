# C-19-12 모델 트랙 : 비코딩 498 쌍 합쳐 재학습 · 대형 → 소형 증류 · INT8 · 재생 평가 — PM 명령서

DM-118 · DM-123. **서버 탑재는 이 단위에 없다** (결과를 보고 다음 단위로). Claude Code 는 클라우드 · GPU 서비스에 접속하지 않았고, 라벨 · 발화 원문 · 정답 키를 저장소에 옮기지 않았다. 데이터는 모두 git 제외 폴더 `docs/handoff/model-track/` 에 있다. 블록 하나에 명령 하나다. ① · ③ 은 **PM 맥의 저장소 루트 (main)**, ② 는 **Colab (T4)** 에서 한다.

두 venv 를 쓴다

- 저장소 venv `venv/bin/python` : 라벨 확정 · 데이터 분할 · 재생 표 (torch 없음)
- 모델 트랙 venv `docs/handoff/model-track/bge-ft/.venv/bin/python` : 학생 증류 · INT8 · 재생용 점수 (torch 2.15 · transformers 5.22). **`onnx` · `onnxruntime` 이 없어 INT8 은 ③-0 설치 뒤에 된다** (Claude Code 는 패키지를 설치하지 않았다)

설정은 **valid 로만** 고르고 `--eval test` 는 고른 설정으로 **한 번** 돌린다 (시험셋을 보고 고르지 않는다).

## ① 맥 : 파트 0 · 1 확인

```bash
venv/bin/python -B tools/nc_labels.py --check-only
```

`"dm123_mismatches": []` 이면 된다. 사람 둘 κ 0.929 (497 쌍, 일치 97.6 %) · 사람 – AI 0.929 · 0.928 · 셋 Fleiss κ 0.928 (497 쌍) · 셋 다 같음 479 · 사람끼리 다름 13 (판단 보류 1 포함, 둘 다 라벨한 쌍에서는 12) · 다수결 진전 106 · 같은 주장 반복 392 · 못 정함 0. 결과 파일 `nc/nc498-labels-v1.jsonl` 은 이미 만들어 두었다 (다시 만들려면 `--check-only` 없이).

```bash
venv/bin/python -B tools/ce_data_v2.py --dry-run
```

`"session_leaks": []`, train 873 쌍 (72 세션) · test 80 쌍 (10 세션, 이전과 같음) · test-nc 102 쌍 (7 세션). 결과 파일 `bge-ft/main-v2.jsonl` (1,055 줄) 은 이미 만들어 두었다.

```bash
venv/bin/python -B tools/ce_replay.py run
```

재생 표의 "rule only" · "human labels" 두 줄이 나온다 (모델 줄은 ③ 뒤). 데모 67 : 보존율 20/24 → 22/24, 탐지 관측 8/9 · 차단 5/5 그대로 (DM-116 ③ 과 같음).

## ② Colab (T4) : 선생 학습

**올릴 파일 4 개** (Colab 왼쪽 파일 창에 끌어다 놓기) : `tools/ce_common.py` · `tools/ce_train.py` (저장소) · `docs/handoff/model-track/bge-ft/main-v2.jsonl` (git 제외, 발화 원문이 들어 있다 — 노트북을 다른 사람과 공유하지 않는다).

노트북 셀 순서 (런타임 유형 T4 GPU) :

셀 1 — GPU 확인

```bash
!nvidia-smi
```

셀 2 — 올린 파일을 도구 폴더 모양으로

```bash
!mkdir -p /content/mag/tools && cp /content/ce_common.py /content/ce_train.py /content/mag/tools/ && cp /content/main-v2.jsonl /content/mag/
```

셀 3 — 선생 valid (모든 업무로 학습, 학습셋 안 5 겹, 시험셋은 읽지 않음)

```bash
!cd /content/mag && python -B tools/ce_train.py --eval valid --data main-v2.jsonl --out runs/teacher-v2 --tag all
```

셀 4 — 선생 valid (대화 업무 CS · MR · LR 만으로 학습)

```bash
!cd /content/mag && python -B tools/ce_train.py --eval valid --data main-v2.jsonl --out runs/teacher-v2 --train-domain dialogue --tag dialogue
```

**셀 5 전에 고른다** : 두 결과 파일 (`result-valid-all.json` · `result-valid-dialogue.json`) 의 `pooled.dialogue.model` (2 차 판정이 쓰는 대화 쌍 정확도) 이 높은 쪽. 같으면 `all`. 고른 것과 두 숫자를 적어 둔다. 아래 셀은 `all` 을 고른 경우다 — `dialogue` 를 골랐으면 셀 5 · 6 에 `--train-domain dialogue` 를 붙인다.

셀 5 — 선생 test **한 번** (학습셋 전체로 학습 → test 80 · test-nc 102 채점, 모델 저장)

```bash
!cd /content/mag && python -B tools/ce_train.py --eval test --data main-v2.jsonl --out runs/teacher-v2
```

셀 6 — 학습셋 바깥 예측 (각 쌍을 그 쌍의 겹을 보지 않은 선생이 채점 → 증류용 선생 확률)

```bash
!cd /content/mag && python -B tools/ce_train.py --eval oof --data main-v2.jsonl --out runs/teacher-v2
```

셀 7 — 내려받기 묶음 (모델 가중치는 빼고 결과 · 확률만)

```bash
!cd /content/mag/runs && zip -r /content/teacher-v2-results.zip teacher-v2 -x 'teacher-v2/model/*'
```

**내려받을 파일** : `/content/teacher-v2-results.zip` (안에 `result-valid-all.json` · `result-valid-dialogue.json` · `result-test.json` · `teacher-oof.json`). 맥에서 `docs/handoff/model-track/bge-ft/runs/` 에 풀어 `runs/teacher-v2/` 가 되게 한다. 선생 모델 (`runs/teacher-v2/model`, 약 2.2 GB) 은 재생에 쓰지 않으므로 받지 않아도 된다.

시간 (예상, 측정 아님) : 이전 BGE valid 1 겹 (학습 381 쌍 · 3 바퀴) 이 맥 MPS 384 초였다. 이번 학습은 겹마다 약 700 쌍 (대화만이면 약 400) 이라 T4 에서 셀 3 · 6 이 각 수십 분, 셀 4 는 그 절반쯤, 셀 5 는 10 ~ 20 분으로 본다. Colab 이 끊기면 그 셀만 다시 돌린다 (결과 파일은 셀마다 따로 쓴다).

## ③ 맥 : 증류 · INT8 · 재생

③-0 — 모델 트랙 venv 에 ONNX 도구 설치 (INT8 에만 필요, 한 번)

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -m pip install onnx onnxruntime==1.29.0
```

③-1 — 학생 valid (증류 없이 = 기준) : Claude Code 가 fold 0 하나를 맥에서 이미 돌렸다 (`runs/student-v2/result-valid-mac-fold0-alpha0.json`, 사본 `reports/c19-12/student-valid-mac-fold0-alpha0.json` : 183 쌍 정확도 80.3 % · 같은 쌍 단어 규칙 89.1 %, 대화 쌍 143 개 81.8 % · 규칙 89.5 %). 5 겹 전체 :

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_distill.py --eval valid --alpha 0 --tag alpha0
```

③-2 — 학생 valid (증류) : α 와 온도 T 를 고른다. 한 줄에 한 설정이고, 다 돌릴 필요는 없다 (위에서부터).

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_distill.py --eval valid --teacher docs/handoff/model-track/bge-ft/runs/teacher-v2/teacher-oof.json --alpha 0.5 --temperature 2 --tag a05-t2
```

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_distill.py --eval valid --teacher docs/handoff/model-track/bge-ft/runs/teacher-v2/teacher-oof.json --alpha 0.8 --temperature 2 --tag a08-t2
```

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_distill.py --eval valid --teacher docs/handoff/model-track/bge-ft/runs/teacher-v2/teacher-oof.json --alpha 0.5 --temperature 4 --tag a05-t4
```

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_distill.py --eval valid --teacher docs/handoff/model-track/bge-ft/runs/teacher-v2/teacher-oof.json --alpha 0.5 --temperature 1 --tag a05-t1
```

고르는 기준 : 각 `result-valid-<tag>.json` 의 `pooled.dialogue.model` 이 가장 높은 설정 (같으면 α 가 작은 쪽). α 0 이 가장 높으면 증류 없이 간다 — 그 사실을 결과로 적는다. 아래는 `a05-t2` 를 고른 경우다 (다르면 `--alpha` · `--temperature` 만 바꾼다).

③-3 — 학생 test **한 번** (모델 저장 → INT8 입력)

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_distill.py --eval test --teacher docs/handoff/model-track/bge-ft/runs/teacher-v2/teacher-oof.json --alpha 0.5 --temperature 2
```

③-4 — INT8 : ONNX 내보내기 → 동적 양자화 → fp32 와 정확도 · 크기 · 지연 (CPU 2 스레드, 224 토큰, p50 · p99)

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_int8.py --model docs/handoff/model-track/bge-ft/runs/student-v2/model
```

③-5 — 재생용 점수 (데모 67 + 비코딩 39 세션의 대화 쌍을 INT8 학생으로 채점)

```bash
docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_replay.py score --onnx docs/handoff/model-track/bge-ft/runs/student-v2-int8/model-int8.onnx --out docs/handoff/model-track/replay/student-int8.json
```

③-6 — 재생 표 (규칙만 / 사람 라벨 / 모델 예측)

```bash
venv/bin/python -B tools/ce_replay.py run --scores docs/handoff/model-track/replay/student-int8.json --out reports/c19-12/replay-model.json
```

시간 (맥, 측정 1 회) : 학생 valid 1 겹 (학습 690 쌍 · 4 바퀴 · 224 토큰) 이 이 맥 (MPS) 에서 학습 59 초 · 채점 포함 90 초였다 → 5 겹 한 설정은 약 8 분, ③-2 네 줄은 약 30 분 (예상). 선생 확률을 쓰는 증류도 학습 계산은 같다. MPS 가 없는 CPU 만의 맥이면 몇 배 더 걸린다 (측정 안 함). INT8 내보내기 · 채점 · 지연 측정은 몇 분 (예상).

## ④ 결과 확인 표

PM 이 ② · ③ 을 돌린 뒤 결과 파일에서 옮겨 적는다 (Claude Code 가 다시 정리할 수 있다 — 결과 파일만 주면 된다).

| 항목 | 값 | 출처 |
|---|---|---|
| 선생 valid (모든 업무) 대화 쌍 정확도 · 단어 규칙 | | `runs/teacher-v2/result-valid-all.json` `pooled` |
| 선생 valid (대화 업무만) 대화 쌍 정확도 · 단어 규칙 | | `runs/teacher-v2/result-valid-dialogue.json` `pooled` |
| 고른 선생과 근거 | | 셀 5 전 기록 |
| 선생 test : test 80 / test-nc 102 정확도 (대화 쌍 · 단어 규칙) | | `runs/teacher-v2/result-test.json` `folds[0].by_split` |
| 학생 valid : α 0 / 고른 α · T | | `runs/student-v2/result-valid-*.json` `pooled` |
| 학생 test : test 80 / test-nc 102 | | `runs/student-v2/result-test.json` |
| INT8 : fp32 → INT8 정확도 차이 · 판정 바뀐 쌍 · 크기 MB · p50 / p99 ms | | `runs/student-v2-int8/result-int8.json` |
| 재생 데모 67 : 보존율 · 탐지 관측 · 차단 · 절감 · 첫 STOP 중앙값 (규칙 / 사람 / 모델) | | `reports/c19-12/replay-model.json` `table.demo67` |
| 재생 비코딩 39 : 같은 항목 | | `table.nc41` |
| 학습에 쓴 세션이 재생에 들어간 수 | | `sessions_in_training` |

읽는 법 : 재생은 판정 동작 확인이지 일반화 수치가 아니다 (학습에 쓴 세션이 대부분 들어 있다). 일반화 숫자는 test-nc (학습에 없는 7 세션) 정확도다. S-32 ⑤ "단어 겹침 규칙보다 높을 것" 은 같은 쌍의 `word_overlap_rule` 과 비교한다.
