#!/usr/bin/env bash
# Update an installed bot from this checkout and restart it.
#   sudo bash deploy/update.sh            # git pull the checkout first (if it is a git repo)
#   sudo bash deploy/update.sh --no-pull  # use the files as they are (push.sh does this)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=deploy/common.sh
source "${SCRIPT_DIR}/common.sh"

PULL=1
for arg in "$@"; do
    case "${arg}" in
        --no-pull) PULL=0 ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) die "Unknown option: ${arg}" ;;
    esac
done

require_root
[[ -x "${APP_DIR}/.venv/bin/python" ]] || die "Not installed yet. Run: sudo bash deploy/install.sh"

if [[ ${PULL} -eq 1 && -d "${SRC_DIR}/.git" ]]; then
    log "Pulling latest code in ${SRC_DIR}"
    # Pull as the checkout's owner, not root, so git doesn't complain about ownership.
    owner="$(stat -c %U "${SRC_DIR}")"
    if [[ "${owner}" == "root" ]]; then
        git -C "${SRC_DIR}" pull --ff-only
    else
        sudo -u "${owner}" git -C "${SRC_DIR}" pull --ff-only
    fi
fi

sync_code "${SRC_DIR}"

if [[ -f "${REQ_STAMP}" && "$(cat "${REQ_STAMP}")" == "$(requirements_hash)" ]]; then
    log "requirements.txt unchanged, skipping pip"
else
    install_requirements
fi

install_unit || true

if env_unfilled; then
    die "${APP_DIR}/.env still has placeholder values. Fill it in, then: sudo systemctl restart ${SERVICE_NAME}"
fi

log "Restarting ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl --no-pager --lines=0 status "${SERVICE_NAME}" || true
log "Update complete"
print_help_commands
