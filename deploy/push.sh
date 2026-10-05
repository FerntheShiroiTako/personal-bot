#!/usr/bin/env bash
# Run on your own machine: copy the working tree to the server over SSH, then install or update there.
#
#   bash deploy/push.sh user@host
#   bash deploy/push.sh host user
#   DEPLOY_HOST=user@host bash deploy/push.sh
#
# Optional environment:
#   DEPLOY_USER       SSH user, if not given as user@host or as the second argument
#   DEPLOY_DIR        staging directory on the server (default: lookup-bot, in the SSH user's home)
#   DEPLOY_SSH_OPTS   extra ssh options, e.g. "-p 2222 -i ~/.ssh/vps"
#
# The SSH user needs sudo on the server. Uses rsync if both ends have it, otherwise tar over SSH
# (Git Bash on Windows has no rsync).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=deploy/common.sh
source "${SCRIPT_DIR}/common.sh"

HOST="${1:-${DEPLOY_HOST:-}}"
USER_ARG="${2:-${DEPLOY_USER:-}}"
REMOTE_DIR="${DEPLOY_DIR:-lookup-bot}"
[[ -n "${HOST}" ]] || die "Usage: bash deploy/push.sh [user@]host [user]   (or set DEPLOY_HOST)"

if [[ "${HOST}" != *@* && -n "${USER_ARG}" ]]; then
    TARGET="${USER_ARG}@${HOST}"
else
    TARGET="${HOST}"
fi

# Intentionally split: DEPLOY_SSH_OPTS holds several options.
read -r -a SSH_OPTS <<< "${DEPLOY_SSH_OPTS:-}"
# Remote commands are built locally on purpose (paths are validated above).
# shellcheck disable=SC2029
ssh_cmd() { ssh "${SSH_OPTS[@]}" "$@"; }

case "${REMOTE_DIR}" in
    *[!A-Za-z0-9._/~-]*) die "DEPLOY_DIR may only contain letters, digits and ._/~-" ;;
esac

log "Copying ${SRC_DIR} to ${TARGET}:${REMOTE_DIR}"
if command -v rsync >/dev/null 2>&1 && ssh_cmd "${TARGET}" 'command -v rsync >/dev/null'; then
    args=()
    for pattern in "${EXCLUDES[@]}"; do
        args+=(--exclude "${pattern}")
    done
    rsync -az --delete "${args[@]}" -e "ssh ${DEPLOY_SSH_OPTS:-}" "${SRC_DIR}/" "${TARGET}:${REMOTE_DIR}/"
else
    log "rsync not available on both ends, using tar over SSH"
    args=()
    for pattern in "${EXCLUDES[@]}"; do
        args+=(--exclude "${pattern#/}")
    done
    # Replace the staging copy wholesale so deleted files don't linger.
    tar -C "${SRC_DIR}" -czf - "${args[@]}" . | ssh_cmd "${TARGET}" \
        "rm -rf ${REMOTE_DIR}.new && mkdir -p ${REMOTE_DIR}.new && tar -xzf - -C ${REMOTE_DIR}.new \
         && rm -rf ${REMOTE_DIR} && mv ${REMOTE_DIR}.new ${REMOTE_DIR}"
fi

log "Running install/update on ${TARGET} (sudo may ask for your password)"
ssh_cmd -t "${TARGET}" \
    "if [ -x ${APP_DIR}/.venv/bin/python ]; then sudo bash ${REMOTE_DIR}/deploy/update.sh --no-pull; \
     else sudo bash ${REMOTE_DIR}/deploy/install.sh; fi"
