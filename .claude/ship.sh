#!/usr/bin/env bash
# ship.sh — bản an toàn của "git add -A && git commit && git push".
#   .claude/ship.sh "feat: mô tả thay đổi"
# Chạy test → commit (trừ file state do CI quản lý) → rebase lên origin/main → push.
set -euo pipefail
cd "$(dirname "$0")/.."

MSG="${1:?Thiếu commit message. Ví dụ: .claude/ship.sh \"feat: thêm bộ lọc thanh khoản\"}"
BRANCH=main
# GitHub Actions commit các file này mỗi 5 phút — để CI làm chủ, tránh conflict.
STATE_FILES="base_seen.json base_signals.csv base_heartbeat.json paper_positions.json paper_trades.csv paper_state.json"

remote=$(git remote get-url origin)
case "$remote" in
  *github.com[:/][Bb]ui[Bb]ap/*) ;;
  *) echo "✗ origin = $remote — không thuộc github.com/BuiBap, dừng."; exit 1 ;;
esac

echo "▶ Chạy test…"
python3 test_runner.py        | tail -2
python3 test_paper_trader.py  | tail -2

git add -A
for f in $STATE_FILES; do
  git ls-files --error-unmatch "$f" >/dev/null 2>&1 && git reset -q -- "$f"
done

if git diff --cached --quiet; then
  echo "• Không có thay đổi code nào để commit."
else
  git commit -q -m "$MSG"
  echo "✓ Commit $(git rev-parse --short HEAD)"
fi

echo "▶ Rebase lên origin/$BRANCH…"
if ! git pull --rebase --autostash -q origin "$BRANCH"; then
  echo "✗ Rebase conflict. Xem 'git status'; sửa xong chạy 'git rebase --continue', hoặc 'git rebase --abort' để huỷ."
  exit 1
fi

git push -q origin "HEAD:$BRANCH"
echo "✓ Đã push: https://github.com/BuiBap/base-meme-scanner/commit/$(git rev-parse HEAD)"
