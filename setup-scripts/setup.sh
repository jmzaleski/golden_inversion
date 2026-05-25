#!/usr/bin/env bash
# setup.sh
# --------
# Defines all configuration variables and install functions for the
# Golden inversion tool.  Can be used two ways:
#
#   1. Run directly to do a full install in sequence:
#        sudo ./setup.sh
#
#   2. Source from a small script to run one step at a time:
#        source ./setup.sh && install_nginx
#
# Individual step scripts (01-system.sh etc.) use pattern 2.

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────

APP_DIR=/opt/golden_inversion
WEB_DIR=/var/www/golden_inversion
LOG_FILE=/var/log/golden-fetcher.log
APP_USER=golden_inversion
NGINX_CONF=/etc/nginx/sites-available/golden_inversion

# Repo-relative paths (resolved from wherever this script lives)
#REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR=$APP_DIR

# ── Helpers ────────────────────────────────────────────────────────────────────

log()  { echo "[$(date '+%H:%M:%S')]  $*"; }
ok()   { echo "  ✓  $*"; }
fail() { echo "  ✗  $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || fail "This step must be run as root (sudo $0)"
	echo got root
}

# ── Step functions ─────────────────────────────────────────────────────────────

install_nginx() {
    require_root
    read -p "don't do this. want to use snap instead" JUNK && exit
    log "Installing system packages..."
    apt-get update -qq
    apt-get install -y -qq nginx curl
    ok "nginx and curl installed"
}


install_uv() {
    require_root
    log "Installing uv packages..."
    snap install astral-uv
    ok "uv installed"
}
create_user() {
    require_root
    log "Creating app user '$APP_USER'..."
    if id -u "$APP_USER" &>/dev/null; then
        ok "User '$APP_USER' already exists -- skipping"
    else
	read -p "useradd $APP_USER ?" JUNK
        useradd -r -m -s /usr/sbin/nologin "$APP_USER"
        ok "User '$APP_USER' created"
    fi

    log "Creating directories..."
    mkdir -p "$APP_DIR" "$WEB_DIR"
    # claude bug? these should NOT be owned by cron  user
    #chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$WEB_DIR"
    ok "$APP_DIR"
    ok "$WEB_DIR"

    log "Creating log file..."
    touch "$LOG_FILE"
    chown "$APP_USER:$APP_USER" "$LOG_FILE"
    ok "$LOG_FILE"
}

deploy_files() {
    require_root
    log "Deploying app files..."
    # below should come from git clone
    #cp "$REPO_DIR/fetcher.py"               "$APP_DIR/fetcher.py"
    #cp "$REPO_DIR/pyproject-fetcher.toml"   "$APP_DIR/pyproject.toml"
    #cp "$REPO_DIR/scripts/cron-fetcher.sh"  "$APP_DIR/cron-fetcher.sh"
    test -f "$APP_DIR/fetcher.py" || fail "fetcher.py"

    test -f "$APP_DIR/cron-fetcher.sh" &&  chmod +x "$APP_DIR/cron-fetcher.sh"
    ok "fetcher.py, pyproject.toml, cron-fetcher.sh -> $APP_DIR"

    cp "$REPO_DIR/golden_inversion_v2.html" "$WEB_DIR/index.html"
    ok "index.html -> $WEB_DIR"

    #chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$WEB_DIR"
    ok "Permissions set"
    log "set up uv"
    cd "$APP_DIR"
    #sudo -u "$APP_USER" uv init || fail "uv init"
    #okay "uv init"
    sudo -u "$APP_USER" uv venv || fail "uv venv"
    ok "uv venv"
    sudo -u "$APP_USER" uv sync || fail "uv sync"
    ok "uv sync"
}

install_nginx() {
    require_root
    log "Installing nginx config..."
    read -p "don't do this. Nick already has" JUNK && exit
    [[ -f "$REPO_DIR/golden.nginx.conf" ]] \
        || fail "golden.nginx.conf not found in $REPO_DIR"

    cp "$REPO_DIR/golden.nginx.conf" "$NGINX_CONF"
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/golden
    rm -f /etc/nginx/sites-enabled/default

    nginx -t || fail "nginx config test failed"
    systemctl reload nginx
    ok "nginx configured and reloaded"
    log "  Next: certbot --nginx -d your-domain.example.com"
}

install_cron() {
    require_root
    log "Installing cron job for '$APP_USER'..."
    local cron_line="*/30 * * * * $APP_DIR/cron-fetcher.sh >> $LOG_FILE 2>&1"
    echo "installing cron line: $cron_line"
    if crontab -u "$APP_USER" -l 2>/dev/null | grep -qF "cron-fetcher.sh"; then
        ok "Cron entry already present -- skipping"
    else
        { crontab -u "$APP_USER" -l 2>/dev/null; echo "$cron_line"; } \
            | crontab -u "$APP_USER" -
        ok "Cron entry added"
    fi

    log "Current crontab for '$APP_USER':"
    crontab -u "$APP_USER" -l | sed 's/^/    /'
}

run_fetcher_once() {
    log "Test run of  cron script cron-fetcher  as '$APP_USER'..."
    (cd $APP_DIR; sudo -u "$APP_USER" uv run "$APP_DIR/cron-fetcher.sh")
    ok "Done -- check $WEB_DIR/sounding.json"
}

show_status() {
    log "Status check:"
    echo ""
    echo "  sounding.json:"
    if [[ -f "$WEB_DIR/sounding.json" ]]; then
        ls -lh "$WEB_DIR/sounding.json"
        python3 -c "import json; d=json.load(open('$WEB_DIR/sounding.json')); print('  generated:', d.get('generated','?'))" 2>/dev/null || true
    else
        echo "  NOT FOUND -- run: source setup.sh && run_fetcher_once"
    fi
    echo ""
    echo "  crontab for $APP_USER:"
    crontab -u "$APP_USER" -l 2>/dev/null | sed 's/^/    /' || echo "    (none)"
    echo ""
    echo "  nginx: $(systemctl is-active nginx)"
    echo ""
    echo "  last log lines:"
    tail -5 "$LOG_FILE" 2>/dev/null | sed 's/^/    /' || echo "    (log empty)"
}

# ── Run all steps if executed directly (not sourced) ──────────────────────────

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
	echo why are we here
	read -p "you probably don't want to run setup.sh like this. It assumes you mean it to install from scratch" JUNK
	exit 1
fi

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    log "=== Full install ==="
    install_system
    create_user
    deploy_files
    install_nginx
    install_cron
    run_fetcher_once
    show_status
    log "=== Done ==="
fi
