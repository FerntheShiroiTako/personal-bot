# shellcheck shell=bash
# Shared settings and helpers for install.sh, update.sh and push.sh. Source it, don't run it.

APP_USER="lookupbot"
APP_DIR="/opt/lookup-bot"
SERVICE_NAME="lookup-bot"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
REQ_STAMP="${APP_DIR}/.venv/.requirements.sha256"

# Never shipped to the server. The leading slash anchors .env to the top level.
EXCLUDES=(
    ".git"
    ".venv"
    "venv"
    "__pycache__"
    "*.pyc"
    "*.patch"
    "/.env"
    ".state"
)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    [[ ${EUID} -eq 0 ]] || die "Run this with sudo: sudo bash $0"
}

# Copy the checkout at $1 into APP_DIR. Code is owned by root and only readable by the
# service group, so the bot can't modify itself. .env and .venv in APP_DIR are excluded
# from both copying and --delete, so they survive.
sync_code() {
    local src="$1"
    local args=()
    local pattern
    for pattern in "${EXCLUDES[@]}"; do
        args+=(--exclude "${pattern}")
    done
    if [[ "$(realpath "${src}")" == "$(realpath "${APP_DIR}")" ]]; then
        log "Code is already in ${APP_DIR}, nothing to copy"
    else
        log "Copying code from ${src} to ${APP_DIR}"
        rsync -a --delete "${args[@]}" \
            --chown=root:"${APP_USER}" --chmod=D750,F640 \
            "${src%/}/" "${APP_DIR}/"
    fi
    chown root:"${APP_USER}" "${APP_DIR}"
    chmod 750 "${APP_DIR}"
}

requirements_hash() {
    sha256sum "${APP_DIR}/requirements.txt" | cut -d' ' -f1
}

install_requirements() {
    log "Installing Python requirements"
    "${APP_DIR}/.venv/bin/python" -m pip install --quiet --upgrade pip
    "${APP_DIR}/.venv/bin/python" -m pip install --quiet -r "${APP_DIR}/requirements.txt"
    requirements_hash > "${REQ_STAMP}"
}

# Install the unit only if it changed. Returns 0 when it was (re)installed.
install_unit() {
    local src="${APP_DIR}/deploy/${SERVICE_NAME}.service"
    if [[ -f "${UNIT_PATH}" ]] && cmp -s "${src}" "${UNIT_PATH}"; then
        return 1
    fi
    log "Installing systemd unit ${UNIT_PATH}"
    install -m 0644 -o root -g root "${src}" "${UNIT_PATH}"
    systemctl daemon-reload
    return 0
}

# True if .env still has the placeholder values from .env.example.
env_unfilled() {
    grep -Eq 'your-discord-bot-token-here|rwd_your_key_here|^OWNER_ID=123456789012345678$' "${APP_DIR}/.env"
}

print_help_commands() {
    cat <<EOF

Useful commands:
  sudo systemctl status ${SERVICE_NAME}
  sudo journalctl -u ${SERVICE_NAME} -f          # follow logs
  sudo journalctl -u ${SERVICE_NAME} -n 100      # last 100 lines
  sudo systemctl restart ${SERVICE_NAME}
  sudo systemctl stop ${SERVICE_NAME}
  sudo systemctl disable --now ${SERVICE_NAME}   # stop and don't start on boot
EOF
}
