#!/usr/bin/env bash
# Unix-Cron-Wrapper. In crontab eintragen:
#   5 * * * * /pfad/zu/run_local_hourly.sh
set -euo pipefail
cd "$(dirname "$0")"
python3 sync.py --live >> logs/cron.log 2>&1
