#!/usr/bin/env bash
# First-time (and repeatable) install of the lookup bot as a systemd service on Ubuntu 22.04/24.04.
# Run from a checkout of this repo:  sudo bash deploy/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=deploy/common.sh
source "${SCRIPT_DIR}/common.sh"

require_root
[[ -f "${SRC_DIR}/bot.py" ]] || die "Can't find bot.py in ${SRC_DIR}. Run this from a checkout of the repo."

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git rsync >/dev/null

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is required (found $(python3 --version 2>&1))"

if id -u "${APP_USER}" >/dev/null 2>&1; then
    log "User ${APP_USER} already exists"
else
    log "Creating system user ${APP_USER}"
    useradd --system --user-group --home-dir "${APP_DIR}" --no-create-home \
        --shell /usr/sbin/nologin "${APP_USER}"
fi

install -d -o root -g "${APP_USER}" -m 0750 "${APP_DIR}"
sync_code "${SRC_DIR}"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    log "Creating virtualenv in ${APP_DIR}/.venv"
    python3 -m venv "${APP_DIR}/.venv"
fi
install_requirements

NEW_ENV=0
if [[ -f "${APP_DIR}/.env" ]]; then
    log "Keeping existing ${APP_DIR}/.env"
else
    log "Creating ${APP_DIR}/.env from .env.example"
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    NEW_ENV=1
fi
chown root:"${APP_USER}" "${APP_DIR}/.env"
chmod 640 "${APP_DIR}/.env"

install_unit || true
systemctl enable --quiet "${SERVICE_NAME}"

if env_unfilled; then
    if [[ ${NEW_ENV} -eq 1 ]]; then
        warn "${APP_DIR}/.env was just created with placeholder values."
    else
        warn "${APP_DIR}/.env still has placeholder values."
    fi
    warn "Fill it in, then start the bot:"
    warn "  sudo nano ${APP_DIR}/.env"
    warn "  sudo systemctl restart ${SERVICE_NAME}"
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
else
    log "Restarting ${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    sleep 2
    systemctl --no-pager --lines=0 status "${SERVICE_NAME}" || true
fi

log "Install complete. The service is enabled and will start on boot."
print_help_commands
