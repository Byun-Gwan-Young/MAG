# C-19-11 CI/CD : 자동 테스트 + 무중단 배포 — PM 명령서

DM-119 · DM-122 : main 에 push 하면 GitHub 러너가 테스트한 뒤 서버로 배포한다. PM 인터넷 (핫스팟) 으로 묶음을 올리지 않는다. **이 명령서의 첫 자동 배포 (⑥) 가 C-19-9b · C-19-9c 를 처음 서버에 싣는다.** Claude Code 는 GitHub · AWS · 서버 설정을 바꾸지 않았고 접속하지 않았다. 아래는 모두 **PM 맥의 저장소 루트 (main)** 에서 돌린다. 블록 하나에 명령 하나다. ④ 는 AWS 콘솔, ⑤ 는 GitHub 웹에서 한다.

달라지는 것

- `.github/workflows/ci.yml` : 풀 리퀘스트마다, 그리고 배포 전에 — 오프라인 테스트 전체 (`network_attempts` 가 비어 있지 않으면 실패) · `final_freeze.py check` · 프론트 `npm ci` · 빌드 · 테스트. 비밀값 없이 돈다
- `.github/workflows/deploy.yml` : main push (또는 Actions 에서 수동 실행) → 위 CI → 통과하면 빌드 · 묶음 → 러너 IP/32 를 보안 그룹 22 번에 추가 → 서버로 → **끝나면 (실패해도) 삭제**
- 서버 : 조회 API · 백엔드 · 게이트웨이가 **두 칸** (a : 8020 · 8030 · 8050, b : 8021 · 8031 · 8051) 으로 번갈아 뜬다. 새 버전을 쉬는 칸에 띄워 세 서비스가 응답하면 Caddy 를 그 칸으로 바꾸고 (`caddy reload`, 연결 유지), 하나라도 응답이 없으면 **쓰던 버전을 그대로 두고 실패로 끝낸다**
- 첫 자동 배포는 지금 서버 (칸 없이 8020 · 8030 · 8050) 를 칸 b 로 옮기고 옛 유닛을 없앤다. 그다음 배포는 a, 그다음은 b
- 옛 칸의 게이트웨이는 진행 중 세션 때문에 바로 끄지 않는다 (열린 세션 0 · 15 분 무요청 · 1 시간 중 먼저 오는 것까지). 그동안 새 게이트웨이가 그 세션의 요청을 옛 게이트웨이로 넘긴다
- **배포 뒤 한 번 다시 로그인한다** (로그인 세션이 백엔드 메모리에 있다, PM 11/17 감수). 화면 · API 가 끊기는 것이 아니라 로그인 화면으로 한 번 간다
- 손 배포 `deploy/aws/deploy.sh` 도 그대로 되고, 같은 두 칸 전환을 거친다

GitHub 에 넣는 것 (이름만 워크플로에 있다) : `DEPLOY_SSH_KEY` · `DEPLOY_KNOWN_HOSTS` · `DEPLOY_HOST` · `AWS_SECURITY_GROUP_ID` · `AWS_DEPLOY_ROLE_ARN` (비밀), `AWS_REGION` (변수). **넣지 않는 것** : 관리자 키 `guardrail-demo.pem`, AWS 접근 키 (`AKIA…`), DB 비밀번호 (서버 DB 는 비밀번호가 없다).

## ① 확인

```bash
git switch main
```

```bash
venv/bin/python tools/run_offline_tests.py --baseline tests/fixtures/p0-3-mechanism-test-ids.json --output /tmp/c19-11-tests.json
```

```bash
venv/bin/python tools/ci_check.py /tmp/c19-11-tests.json
```

```bash
venv/bin/python tools/final_freeze.py check
```

`ci_check.py` 는 문제 줄 없이 끝나고 (맥 기준 1,379 실행 · 1,360 통과 · 건너뜀 19 · 실패 0, `reports/c19-11/tests.json`), 동결 확인은 `"differences": []`.

## ② 배포 전용 키 만들기 (PM 맥)

```bash
deploy/aws/ci-setup.sh keygen
```

`~/.ssh/guardrail-deploy-ci` (개인 키) 와 `.pub` 가 생긴다. 이 키는 서버에서 **명령 하나** (묶음 설치) 만 된다. 이미 있으면 멈춘다.

## ③ 서버에 배포 전용 계정 만들기

서버 SSH 는 PM IP 만 열려 있다. 장소가 바뀌었으면 먼저 연다.

```bash
deploy/aws/provision.sh --update-ssh-ip
```

```bash
deploy/aws/ci-setup.sh server
```

끝 줄에 `배포 전용 계정 준비 끝 : guardrail-deploy (명령 하나 : /usr/local/sbin/guardrail-deploy)` 와 sudo 로 되는 것 한 줄 (`(root) NOPASSWD: /usr/local/sbin/guardrail-deploy ""`) 이 나온다. 보내는 파일은 작은 파일 3 개라 핫스팟으로도 된다.

키가 그 명령에만 묶였는지 확인한다. 셸이 열리지 않고 `중단 : 묶음이 비어 있다` 가 나오면 맞다.

```bash
ssh -i ~/.ssh/guardrail-deploy-ci -o IdentitiesOnly=yes guardrail-deploy@"$(deploy/aws/ci-setup.sh value DEPLOY_HOST)" id < /dev/null
```

## ④ AWS : OIDC 공급자 · IAM 역할 (콘솔)

**가. OIDC 공급자** — IAM → 자격 증명 공급자 → 공급자 추가 → OpenID Connect

- 공급자 URL : `https://token.actions.githubusercontent.com`
- 대상 (Audience) : `sts.amazonaws.com`

이미 있으면 건너뛴다.

**나. 역할** — IAM → 역할 → 역할 생성 → 사용자 지정 신뢰 정책. 아래 명령이 신뢰 정책 JSON 을 클립보드에 넣는다 (계정 ID · 저장소 이름이 채워져 있다). 붙여 넣는다.

```bash
deploy/aws/ci-setup.sh trust-policy | pbcopy
```

이 정책은 **이 저장소의 `production` 환경에서 도는 작업만** 역할을 받게 한다 (다른 저장소 · 브랜치 · 풀 리퀘스트는 못 받는다). 권한 정책은 아무것도 고르지 않고 다음으로 넘어가 역할 이름을 `mag-github-deploy` 로 만든다.

**다. 권한** — 만든 역할 → 권한 추가 → 인라인 정책 생성 → JSON. 아래 명령의 결과를 붙여 넣고 이름을 `mag-deploy-ssh-rule` 로 저장한다.

```bash
deploy/aws/ci-setup.sh permission-policy | pbcopy
```

권한은 서버 보안 그룹 하나의 `AuthorizeSecurityGroupIngress` · `RevokeSecurityGroupIngress` 둘뿐이다. 인스턴스 · 다른 보안 그룹 · IAM 은 건드릴 수 없다.

**라.** 역할 화면 위쪽의 ARN (`arn:aws:iam::…:role/mag-github-deploy`) 을 복사해 둔다 (⑤ 의 `AWS_DEPLOY_ROLE_ARN`).

## ⑤ GitHub : 환경 · Secrets · Variables (웹)

저장소 → Settings → Environments → New environment → 이름 `production`. Deployment branches and tags 를 **Selected branches and tags** 로 바꾸고 `main` 을 넣는다.

이름과 값의 출처를 다시 본다.

```bash
deploy/aws/ci-setup.sh names
```

Environment secrets 에 하나씩 Add environment secret. 값은 아래 명령이 클립보드에 넣는다 (화면에 찍히지 않는다).

`DEPLOY_SSH_KEY`

```bash
deploy/aws/ci-setup.sh value DEPLOY_SSH_KEY | pbcopy
```

`DEPLOY_KNOWN_HOSTS`

```bash
deploy/aws/ci-setup.sh value DEPLOY_KNOWN_HOSTS | pbcopy
```

`DEPLOY_HOST`

```bash
deploy/aws/ci-setup.sh value DEPLOY_HOST | pbcopy
```

`AWS_SECURITY_GROUP_ID`

```bash
deploy/aws/ci-setup.sh value AWS_SECURITY_GROUP_ID | pbcopy
```

`AWS_DEPLOY_ROLE_ARN` — ④ 라 에서 복사한 ARN.

Environment variables 에 Add environment variable : `AWS_REGION`

```bash
deploy/aws/ci-setup.sh value AWS_REGION | pbcopy
```

다 넣은 뒤 클립보드를 비운다.

```bash
pbcopy < /dev/null
```

## ⑥ 첫 자동 배포 (이 배포가 9b + 9c 를 처음 싣는다)

```bash
git push origin main
```

저장소 → Actions → Deploy 에서 본다. `Offline tests and freeze check` · `Frontend build and tests` 가 통과한 뒤 `Build, send, switch` 가 돈다. 마지막 단계 로그에 `칸 : 지금 legacy → 새 버전은 b` → `칸 b 응답 확인` → `전환 끝 : 칸 b ← …` → `배포 끝` 이 나오고, `Close port 22 again` 이 `port 22 closed again for this runner` 로 끝난다. 로그에 서버 주소 · 러너 IP 는 `***` 로 나온다.

서버 상태를 본다.

```bash
deploy/aws/remote.sh status
```

`지금 쓰는 칸 : b`, `guardrail-api@b.service : active` · `guardrail-backend@b.service : active` · `guardrail-gateway@b.service : active` 가 나온다. 옛 게이트웨이 (`guardrail-gateway.service`) 는 길게는 1 시간 `active` 로 남았다가 사라진다.

브라우저에서 다시 로그인한 뒤 **`reports/c19-9b/pm-commands.md` ⑤~⑫** 로 9b · 9c 화면을 확인한다 (발화 기록 켜기 → 짧은 세션 → 누적 토큰 · 근거 · 발화 → 끄기 → 키 폐기 → 프로젝트 삭제).

CI 가 실패하면 배포는 돌지 않는다. Actions 의 실패한 작업에서 테스트 이름을 보고, 자세한 내용은 그 실행의 Artifacts → `offline-tests` 를 받는다.

## ⑦ 실패하는 버전 : 전환하지 않는지 확인

백엔드가 뜨지 않는 버전을 일부러 배포한다. **`중단 : 새 버전이 칸 … 에서 응답하지 않는다. 전환하지 않았다 — 지금 버전 (칸 …) 그대로다` 로 끝나야 맞다.**

```bash
deploy/aws/remote.sh drill
```

```bash
deploy/aws/remote.sh status
```

`지금 쓰는 칸` 이 ⑥ 과 같고 브라우저 화면이 그대로 열린다 (로그인도 유지된다).

## ⑧ 되돌림 : 지난 릴리스로, 그리고 다시 새 버전으로

서버에는 쓰는 릴리스와 그 앞 2 개가 남는다. 아래는 바로 앞 릴리스 (9b · 9c 전) 를 쉬는 칸에 띄워 확인하고 전환한다. DB 는 건드리지 않는다.

```bash
deploy/aws/remote.sh rollback
```

```bash
deploy/aws/remote.sh status
```

`app →` 이 앞 릴리스 이름으로 바뀐다. 화면에서 9b 의 누적 토큰 칸이 없는 옛 화면인지 본다. 같은 명령을 한 번 더 돌리면 새 버전으로 돌아온다.

```bash
deploy/aws/remote.sh rollback
```

```bash
deploy/aws/remote.sh status
```

특정 릴리스로 되돌리려면 서버에서 `status` 의 릴리스 이름을 붙여 `sudo bash /opt/guardrail/app/deploy/aws/slots.sh rollback 이름` 을 돌린다 (`deploy/aws/remote.sh ssh` 로 들어가서).

## ⑨ 배포 중 끊김이 없는지 확인

맥 창 1 에서 1 초마다 응답 코드를 찍는다. 끝까지 `200` 만 나와야 한다. Ctrl-C 로 끝낸다.

```bash
while true; do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' "https://$(sed -n 's/^GUARDRAIL_DOMAIN=//p' deploy/aws/target.env)/login"; sleep 1; done
```

브라우저에는 대시보드를 띄워 두고 계속 새로 고친다. 맥 창 2 에서 내용 없는 커밋을 올려 배포를 일으킨다.

```bash
git commit --allow-empty -m "Deploy check: no change"
```

```bash
git push origin main
```

창 1 은 계속 `200`, 브라우저는 전환 순간 **로그인 화면으로 한 번** 가고 (감수한 것) 오류 화면 (502 · 연결 끊김) 은 나오지 않는다. 진행 중 세션까지 보려면 배포 직전에 `reports/c19-9b/pm-commands.md` ⑥ 의 짧은 세션을 시작해 배포가 끝난 뒤 다음 step 을 보낸다 — 409 없이 이어지고 연속 횟수가 이어진다.

## ⑩ 배포 뒤 보안 그룹에 러너 IP 가 남지 않았는지

```bash
deploy/aws/ci-setup.sh ssh-rules
```

PM 주소 한 줄 (`…/32`) 만 나와야 한다. 다른 줄이 있으면 (배포 작업이 강제로 끊긴 경우) EC2 콘솔 → 보안 그룹 `guardrail-demo-sg` → 인바운드 규칙 편집에서 그 22 번 줄을 지운다. `0.0.0.0/0` 줄은 있으면 안 된다.

## 그 뒤

- 배포 : `git push origin main` 하나. 급하면 Actions → Deploy → Run workflow (main)
- 자동 배포를 잠시 멈추려면 : Actions → Deploy → `…` → Disable workflow
- 배포 전에 사람 승인을 받게 하려면 : Settings → Environments → production → Required reviewers (PM 선택)
- 러너가 못 들어갈 때 손 배포 (대비) : `reports/c19-9b/pm-commands.md` ② ~ ④
- 배포 전용 키를 바꾸려면 : `~/.ssh/guardrail-deploy-ci` 와 `.pub` 를 다른 이름으로 옮긴 뒤 ② · ③ · ⑤ 의 `DEPLOY_SSH_KEY`
