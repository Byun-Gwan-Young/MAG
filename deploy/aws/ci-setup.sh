#!/usr/bin/env bash
# C-19-11 parts 3 · 4 (DM-122) : what the PM prepares once for the GitHub deploy, from the PM's Mac (repository root).
# Claude Code never runs this : it reads target.env, the PM's ~/.ssh and (for three subcommands) the AWS CLI.
#
#   deploy/aws/ci-setup.sh keygen              make the deploy-only key ~/.ssh/guardrail-deploy-ci (ed25519, no passphrase)
#   deploy/aws/ci-setup.sh server              make the deploy-only account on the server (deploy-user-setup.sh), with the
#                                              administrator key of target.env ; that key stays on this Mac
#   deploy/aws/ci-setup.sh names               the secret · variable names the workflow reads, and where each value comes from
#   deploy/aws/ci-setup.sh value NAME          print one value to stdout, for `| pbcopy` (never shown otherwise) :
#                                              DEPLOY_SSH_KEY · DEPLOY_KNOWN_HOSTS · DEPLOY_HOST · AWS_SECURITY_GROUP_ID · AWS_REGION
#   deploy/aws/ci-setup.sh trust-policy        the IAM role's trust policy JSON (GitHub OIDC, this repository's
#                                              "production" environment only)
#   deploy/aws/ci-setup.sh permission-policy   the role's only permission : add · remove an inbound rule of the server's
#                                              security group
#   deploy/aws/ci-setup.sh ssh-rules           the CIDRs now allowed on port 22 (after a deploy : the PM's address only)
#
# Nothing is written to the repository. The AWS account ID appears only inside the two policy JSONs on stdout (they need
# it) ; AWS_DEPLOY_ROLE_ARN is copied from the console after the role is made.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
. "$SCRIPT_DIR/lib.sh"

CI_KEY="$HOME/.ssh/guardrail-deploy-ci"
OIDC_HOST=token.actions.githubusercontent.com
ENVIRONMENT=production

awsq() { aws --region "$GUARDRAIL_REGION" --output text "$@"; }

account_id() {
    local id
    id=$(awsq sts get-caller-identity --query Account) || die "AWS 자격 증명이 없다 (aws configure)"
    [[ "$id" =~ ^[0-9]{12}$ ]] || die "계정 ID 를 읽지 못했다"
    printf '%s' "$id"
}

security_group() {
    local sg
    sg=$(awsq ec2 describe-security-groups --filters "Name=group-name,Values=$GUARDRAIL_SG_NAME" \
        --query 'SecurityGroups[0].GroupId') || die "보안 그룹을 읽지 못했다"
    [[ "$sg" =~ ^sg-[0-9a-f]{8,17}$ ]] || die "보안 그룹 $GUARDRAIL_SG_NAME 이 없다"
    printf '%s' "$sg"
}

repository() {    # owner/name of origin, from its URL
    local url repo
    url=$(git -C "$SCRIPT_DIR" remote get-url origin) || die "git origin 이 없다"
    repo=$(printf '%s' "$url" | sed -E 's#^(https://[^/]+/|git@[^:]+:)##; s#\.git$##')
    [[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "origin 주소에서 저장소 이름을 읽지 못했다"
    printf '%s' "$repo"
}

known_hosts_line() {    # the server's host key as this Mac already trusts it (first ssh : remote.sh ssh)
    local lines
    lines=$(ssh-keygen -F "$GUARDRAIL_SERVER_IP" 2>/dev/null | grep -v '^#' || true)
    [[ -n "$lines" ]] || die "이 맥의 known_hosts 에 서버 호스트 키가 없다 (deploy/aws/remote.sh ssh 로 한 번 접속)"
    printf '%s\n' "$lines" | awk -v host="$GUARDRAIL_SERVER_IP" '{ $1 = host; print }'
}

case "${1:-}" in
    keygen)
        [[ ! -e "$CI_KEY" ]] || die "$CI_KEY 가 이미 있다 (다시 만들려면 그 파일과 .pub 를 먼저 옮긴다)"
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        ssh-keygen -q -t ed25519 -N '' -C guardrail-deploy-ci -f "$CI_KEY"
        chmod 600 "$CI_KEY"
        say "배포 전용 키 : $CI_KEY (개인 키는 GitHub Secret DEPLOY_SSH_KEY 로만, 공개 키는 서버로)" ;;
    server)
        load_target "$SCRIPT_DIR/target.env"
        [[ -f "$CI_KEY.pub" ]] || die "$CI_KEY.pub 이 없다 (ci-setup.sh keygen 먼저)"
        ssh "${SSH_OPTS[@]}" "$SSH_DEST" 'rm -rf /tmp/guardrail-ci-setup && mkdir -m 700 /tmp/guardrail-ci-setup'
        scp "${SSH_OPTS[@]}" "$CI_KEY.pub" "$SCRIPT_DIR/deploy-user-setup.sh" "$SCRIPT_DIR/guardrail-deploy.sh" \
            "$SSH_DEST:/tmp/guardrail-ci-setup/"
        ssh "${SSH_OPTS[@]}" "$SSH_DEST" 'sudo bash /tmp/guardrail-ci-setup/deploy-user-setup.sh \
            /tmp/guardrail-ci-setup/guardrail-deploy-ci.pub /tmp/guardrail-ci-setup/guardrail-deploy.sh; \
            status=$?; rm -rf /tmp/guardrail-ci-setup; exit $status' ;;
    names)
        cat <<'TEXT'
GitHub → Settings → Environments → production (없으면 New environment "production")

Environment secrets (값은 PM 이 붙여 넣는다)
  DEPLOY_SSH_KEY          배포 전용 개인 키        deploy/aws/ci-setup.sh value DEPLOY_SSH_KEY | pbcopy
  DEPLOY_KNOWN_HOSTS      서버 호스트 키 한 줄     deploy/aws/ci-setup.sh value DEPLOY_KNOWN_HOSTS | pbcopy
  DEPLOY_HOST             서버 주소                deploy/aws/ci-setup.sh value DEPLOY_HOST | pbcopy
  AWS_SECURITY_GROUP_ID   서버 보안 그룹 ID        deploy/aws/ci-setup.sh value AWS_SECURITY_GROUP_ID | pbcopy
  AWS_DEPLOY_ROLE_ARN     IAM 역할 ARN             AWS 콘솔 → IAM → 역할 → mag-github-deploy → ARN 복사

Environment variables
  AWS_REGION              리전                     deploy/aws/ci-setup.sh value AWS_REGION | pbcopy

넣지 않는 것 : 관리자 SSH 키 (guardrail-demo.pem), AWS 접근 키 (AKIA…), DB 비밀번호 (서버 DB 는 비밀번호가 없다)
TEXT
        ;;
    value)
        case "${2:-}" in
            DEPLOY_SSH_KEY)
                [[ -f "$CI_KEY" ]] || die "$CI_KEY 가 없다 (ci-setup.sh keygen 먼저)"
                [[ ! -t 1 ]] || die "개인 키는 화면에 찍지 않는다 : deploy/aws/ci-setup.sh value DEPLOY_SSH_KEY | pbcopy"
                cat "$CI_KEY" ;;
            DEPLOY_KNOWN_HOSTS) load_target "$SCRIPT_DIR/target.env"; known_hosts_line ;;
            DEPLOY_HOST) load_target "$SCRIPT_DIR/target.env"; printf '%s\n' "$GUARDRAIL_SERVER_IP" ;;
            AWS_SECURITY_GROUP_ID) security_group; printf '\n' ;;
            AWS_REGION) printf '%s\n' "$GUARDRAIL_REGION" ;;
            *) die "모르는 이름 : ${2:-없음} (ci-setup.sh names)" ;;
        esac ;;
    trust-policy)
        account=$(account_id)
        repo=$(repository)
        cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Federated": "arn:aws:iam::${account}:oidc-provider/${OIDC_HOST}" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_HOST}:aud": "sts.amazonaws.com",
          "${OIDC_HOST}:sub": "repo:${repo}:environment:${ENVIRONMENT}"
        }
      }
    }
  ]
}
JSON
        ;;
    permission-policy)
        account=$(account_id)
        sg=$(security_group)
        cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress"
      ],
      "Resource": "arn:aws:ec2:${GUARDRAIL_REGION}:${account}:security-group/${sg}"
    }
  ]
}
JSON
        ;;
    ssh-rules)
        sg=$(security_group)
        awsq ec2 describe-security-groups --group-ids "$sg" \
            --query 'SecurityGroups[0].IpPermissions[?FromPort==`22`].IpRanges[].CidrIp' | tr '\t' '\n' ;;
    *)
        sed -n '2,19p' "$0"; exit 2 ;;
esac
