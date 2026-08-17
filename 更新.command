#!/bin/bash
# ダブルクリックでセール告知を集め直し、変化があればGitHubへ反映する。
cd "$(dirname "$0")" || exit 1
echo "=== 旅トク 手動更新 ==="
# 自動更新（GitHub Actions）が先にコミットしていることがあるので、まず取り込む。
# これをやらないと docs/deals.json で毎回ぶつかる。
git pull --rebase --quiet origin main 2>/dev/null
python3 collect.py || { echo "取得に失敗しました"; read -r -p "Enterで閉じる"; exit 1; }
if git diff --quiet docs/deals.json 2>/dev/null; then
  echo "新しい告知はありませんでした。"
else
  git add docs/deals.json
  git commit -m "セール告知の手動更新 $(date '+%Y-%m-%d %H:%M')" && git push && echo "公開サイトに反映しました。"
fi
read -r -p "Enterで閉じる"
