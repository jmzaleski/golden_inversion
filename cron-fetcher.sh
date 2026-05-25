#!/usr/bin/env bash
# cron-fetcher.sh
# ---------------
# Deployed to /opt/golden/ on the VPS and called directly by cron.
# Does NOT source setup.sh (which lives in the repo, not on the VPS).
#
# Crontab entry (installed by 05-cron.sh):
#   */30 * * * * /opt/golden/cron-fetcher.sh >> /var/log/golden-fetcher.log 2>&1
#
# Test manually:
#   sudo -u golden /opt/golden/cron-fetcher.sh
#   source ./setup.sh && run_fetcher_once   # (from repo, does same thing)

set -euo pipefail

APP_DIR=/opt/golden_inversion

# cron's PATH is minimal — find uv wherever it might be installed
export PATH="/root/.local/bin:/home/golden/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

echo "[$(date '+%Y-%m-%d %H:%M:%S')]  starting fetch"

#really i'd like to push this magic value in from the setup scripts.
#cant face struggling with m4 of cpp right now

export GOLDEN_OUTPUT_PATH="/var/www/golden_inversion/sounding.json"  
###test export GOLDEN_OUTPUT_PATH="/tmp/sounding.json"  

cd "$APP_DIR"
exec uv run python fetcher.py
