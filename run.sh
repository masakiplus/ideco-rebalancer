#!/bin/bash
# iDeCo 月次判定スクリプト（launchd用）
cd /Users/masaki/Documents/notes/tools/ideco-rebalancer
LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_$(date '+%Y%m%d_%H%M%S').log"

# 判定本体は .venv の依存（requests等）が要る。Notion記録はstdlibのみで動く
PY=".venv/bin/python3"
if [ ! -x "$PY" ]; then
  echo "警告: $PY が無いため system python3 を使用" >> "$LOG_FILE"
  PY="python3"
fi

echo "=== iDeCo 月次判定 $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
"$PY" tools/ideco_rebalancer.py >> "$LOG_FILE" 2>&1
REBALANCE_STATUS=$?
echo "=== 判定完了 (exit=$REBALANCE_STATUS) ===" >> "$LOG_FILE"

# 判定が成功したときだけ Notion DB「iDeCo資産推移」へ当月分を1行記録する。
# 同月の行が既にあれば更新するので、再実行しても行は増えない。
NOTION_STATUS=0
if [ $REBALANCE_STATUS -eq 0 ]; then
  echo "=== Notion記録 $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
  "$PY" tools/ideco_notion_sync.py >> "$LOG_FILE" 2>&1
  NOTION_STATUS=$?
  if [ $NOTION_STATUS -ne 0 ]; then
    echo "警告: Notion記録に失敗 (exit=$NOTION_STATUS)。判定結果は output/ に残っている" >> "$LOG_FILE"
  fi
else
  echo "判定が失敗したため Notion記録はスキップ" >> "$LOG_FILE"
fi

echo "=== 完了 ===" >> "$LOG_FILE"

# 古いログを削除（90日以上）
find "$LOG_DIR" -name "run_*.log" -mtime +90 -delete 2>/dev/null

if [ $REBALANCE_STATUS -ne 0 ]; then
  exit $REBALANCE_STATUS
fi
exit $NOTION_STATUS
