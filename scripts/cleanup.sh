#!/bin/bash
# cleanup.sh — удаление старых логов, запускается из cron раз в сутки

BASE="/root/bybit_sniper_v11/test_run"
LOG="/root/bybit_sniper_v11/test_run/logs/cleanup.log"

echo "[$(date +%Y-%m-%d\ %H:%M:%S)] cleanup started" >> "$LOG"

# Удаляем ротированные бэкапы логов старше 7 дней
# RotatingFileHandler пишет файлы вида agent_01.log.1, agent_01.log.2 и т.д.
find "$BASE/logs" -type f -name "*.log.*" -mtime +7 -delete 2>/dev/null

# Удаляем старые отчёты analyze.py старше 7 дней
find "$BASE/reports" -type f -name "*.txt" -mtime +7 -delete 2>/dev/null
find "$BASE/reports" -type f -name "*.json" -mtime +7 -delete 2>/dev/null

# Статистика
COUNT=$(ls -1 "$BASE/logs" 2>/dev/null | wc -l)
SIZE=$(du -sh "$BASE" 2>/dev/null | cut -f1)
echo "[$(date +%Y-%m-%d\ %H:%M:%S)] cleanup done. files_in_logs=$COUNT total_size=$SIZE" >> "$LOG"
