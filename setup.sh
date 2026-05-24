#!/usr/bin/env bash
# setup.sh  —  one-time VPS setup for the Golden inversion tool
# Run as root or with sudo on a fresh Debian/Ubuntu VPS (e.g. Hetzner CX22)
#
# After this script: edit /etc/nginx/sites-available/golden to set your domain,
# then run certbot to get a TLS cert.

set -euo pipefail

APP_DIR=/opt/golden
WEB_DIR=/var/www/golden
LOG_FILE=/var/log/golden-fetcher.log
CRON_USER=golden                  # unprivileged user that runs the fetcher

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq nginx python3 python3-pip curl

# Install uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# ── App user ──────────────────────────────────────────────────────────────────
id -u $CRON_USER &>/dev/null || useradd -r -s /usr/sbin/nologin $CRON_USER

# ── App directory ─────────────────────────────────────────────────────────────
mkdir -p $APP_DIR $WEB_DIR
cp fetcher.py requirements-fetcher.txt $APP_DIR/
cp golden_inversion.html $WEB_DIR/index.html    # the iframe target page
chown -R $CRON_USER:$CRON_USER $APP_DIR $WEB_DIR

# ── Python environment ────────────────────────────────────────────────────────
cd $APP_DIR
sudo -u $CRON_USER uv venv .venv
sudo -u $CRON_USER uv pip install -r requirements-fetcher.txt

# ── Log file ──────────────────────────────────────────────────────────────────
touch $LOG_FILE
chown $CRON_USER:$CRON_USER $LOG_FILE

# ── nginx ─────────────────────────────────────────────────────────────────────
cp golden.nginx.conf /etc/nginx/sites-available/golden
ln -sf /etc/nginx/sites-available/golden /etc/nginx/sites-enabled/golden
# Remove default site if it's there
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── cron ──────────────────────────────────────────────────────────────────────
# Runs every 30 minutes; first run happens at next :00 or :30
CRON_LINE="*/30 * * * * cd $APP_DIR && .venv/bin/python fetcher.py >> $LOG_FILE 2>&1"

# Add to cron only if not already there
(crontab -u $CRON_USER -l 2>/dev/null | grep -qF "fetcher.py") || \
    (crontab -u $CRON_USER -l 2>/dev/null; echo "$CRON_LINE") | \
    crontab -u $CRON_USER -

echo ""
echo "✓  Setup complete."
echo ""
echo "Next steps:"
echo "  1. Edit /etc/nginx/sites-available/golden  — set server_name to your domain"
echo "  2. sudo certbot --nginx -d your-domain.example.com"
echo "  3. Run a first fetch manually to confirm:"
echo "       sudo -u $CRON_USER $APP_DIR/.venv/bin/python $APP_DIR/fetcher.py"
echo "  4. Check $WEB_DIR/sounding.json exists and looks right"
echo "  5. Point your WordPress iframe at https://your-domain.example.com/"
echo ""
echo "WordPress embed (paste into an HTML block in Elementor):"
echo '  <iframe src="https://your-domain.example.com/"'
echo '          style="width:100%;height:700px;border:none;"'
echo '          loading="lazy"></iframe>'
echo ""
echo "To tail the fetcher log:"
echo "  tail -f $LOG_FILE"
