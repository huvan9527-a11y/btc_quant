#!/bin/bash
# 每日运行: 北京时间 08:10 (日线 UTC 00:00 收盘后)
#   crontab -e 加一行:   10 8 * * * /绝对路径/btc_quant/run_daily.sh >> /绝对路径/btc_quant/output/cron.log 2>&1
#   (若服务器是 UTC 时区, 改为  10 0 * * *)
cd "$(dirname "$0")"
python3 btc_position.py > output/last_run.log 2>&1 || { echo "[$(date +%F)] btc_position.py 失败, 见 output/last_run.log"; exit 1; }
python3 monitor.py >> output/last_run.log 2>&1
# 推送 (微信Server酱/Telegram): 默认只在需要操作 / 监控非 GREEN 时推送; 想每天都收到摘要, 把 --daily 改成 --daily --always
python3 notify.py --daily
echo "[$(date +%F)] done"
