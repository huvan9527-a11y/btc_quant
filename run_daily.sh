#!/bin/bash
cd "$(dirname "$0")"
mkdir -p data output
echo "== btc_position.py =="
python3 btc_position.py 2>&1 | tee output/last_run.log | tail -60
test ${PIPESTATUS[0]} -eq 0 || { echo "!! btc_position.py 失败 (完整日志在上方)"; exit 1; }
echo "== monitor.py =="
python3 monitor.py 2>&1 | tee -a output/last_run.log | tail -20
echo "== notify.py =="
python3 notify.py --daily
echo "[$(date +%F)] done"
