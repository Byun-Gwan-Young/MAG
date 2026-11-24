#!/usr/bin/env bash
# C-19-11 part 2b (DM-122 ①) : let THIS GitHub runner reach the server's SSH port for one deploy, and nobody else.
# Run by .github/workflows/deploy.yml after it took the AWS role through GitHub OIDC. Never run by Claude Code.
#
#   ci-runner-access.sh open     add  <this runner's public IP>/32, tcp 22, to the security group
#   ci-runner-access.sh close    remove that same rule (the workflow runs it with `if: always()`)
#
# Reads AWS_SECURITY_GROUP_ID and AWS_REGION from the environment (GitHub secret · variable, by name). The rule is only
# ever one address with /32 : there is no path here that opens 0.0.0.0/0 or a range. `open` remembers the address in
# $RUNNER_TEMP before it asks AWS, so `close` removes the rule even when `open` stopped half-way. Nothing is printed
# but short status lines : the address is masked in the log first, AWS output (rule IDs, the group) is not shown, and
# there is no `set -x`.
set -euo pipefail

STATE=${RUNNER_TEMP:-${TMPDIR:-/tmp}}/guardrail-runner-ip
die() { printf 'stop : %s\n' "$*" >&2; exit 1; }

# A value pasted into a GitHub secret can carry a trailing newline : trim white space at both ends only (a space inside still fails).
AWS_SECURITY_GROUP_ID=${AWS_SECURITY_GROUP_ID:-}
AWS_SECURITY_GROUP_ID=${AWS_SECURITY_GROUP_ID#"${AWS_SECURITY_GROUP_ID%%[![:space:]]*}"}
AWS_SECURITY_GROUP_ID=${AWS_SECURITY_GROUP_ID%"${AWS_SECURITY_GROUP_ID##*[![:space:]]}"}
AWS_REGION=${AWS_REGION:-}
AWS_REGION=${AWS_REGION#"${AWS_REGION%%[![:space:]]*}"}
AWS_REGION=${AWS_REGION%"${AWS_REGION##*[![:space:]]}"}
# `close` with nothing opened (the job stopped before `open`) has nothing to check or remove.
if [[ "${1:-}" == close && ! -f "$STATE" ]]; then
    printf 'no rule was opened by this job : nothing to close\n'
    exit 0
fi
[[ "$AWS_SECURITY_GROUP_ID" =~ ^sg-[0-9a-f]{8,17}$ ]] || die "AWS_SECURITY_GROUP_ID is not set or not a security group ID"
[[ "$AWS_REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || die "AWS_REGION is not set or not a region name"

valid_single_ip() {
    [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    local part
    for part in "${BASH_REMATCH[@]:1}"; do (( 10#$part <= 255 )) || return 1; done
    [[ "$1" != 0.0.0.0 ]]
}

rule() {    # rule authorize|revoke IP : AWS's answer is kept out of the log ; only its error code comes back
    local out
    if out=$(aws ec2 "$1-security-group-ingress" --region "$AWS_REGION" --group-id "$AWS_SECURITY_GROUP_ID" \
                 --protocol tcp --port 22 --cidr "$2/32" 2>&1 >/dev/null); then
        return 0
    fi
    printf '%s' "$out" | sed -n 's/.*(\([A-Za-z.]*\)).*/\1/p' | head -n 1
    return 1
}

case "${1:-}" in
    open)
        ip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]') || die "could not read this runner's public IP"
        valid_single_ip "$ip" || die "the public IP answer is not one IPv4 address"
        printf '::add-mask::%s\n' "$ip"
        ( umask 077; printf '%s\n' "$ip" > "$STATE" )
        if code=$(rule authorize "$ip"); then
            printf 'port 22 opened for this runner (one address, /32)\n'
        elif [[ "$code" == InvalidPermission.Duplicate ]]; then
            printf 'port 22 was already open for this runner (one address, /32)\n'
        else
            die "the security group rule was not added (${code:-no error code})"
        fi ;;
    close)
        [[ -f "$STATE" ]] || { printf 'no rule was opened by this job : nothing to close\n'; exit 0; }
        ip=$(tr -d '[:space:]' < "$STATE")
        valid_single_ip "$ip" || die "the remembered address is not one IPv4 address"
        printf '::add-mask::%s\n' "$ip"
        if code=$(rule revoke "$ip"); then
            printf 'port 22 closed again for this runner\n'
        elif [[ "$code" == InvalidPermission.NotFound ]]; then
            printf 'the rule was not there any more\n'
        else
            die "THE RULE WAS NOT REMOVED (${code:-no error code}) : remove it by hand (reports/c19-11/pm-commands.md)"
        fi
        rm -f "$STATE" ;;
    *)
        sed -n '2,13p' "$0"; exit 2 ;;
esac
