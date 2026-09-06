#!/usr/bin/env bash
#
# install.sh — Cài base-meme-bot thành dịch vụ systemd chạy 24/7.
#
# Chạy trên VPS Ubuntu/Debian (Oracle Cloud, Hetzner, ...):
#     git clone <repo> ~/base-meme-scanner
#     cd ~/base-meme-scanner
#     bash deploy/install.sh
#
# Script này cố ý IDEMPOTENT: chạy lại nhiều lần không hỏng gì, dùng để cập
# nhật sau khi git pull.

set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="base-meme-bot"
UNIT_SRC="$APPDIR/deploy/$SERVICE_NAME.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"
RUN_USER="${SUDO_USER:-$USER}"

c_ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
c_info() { printf '\033[34m→\033[0m %s\n' "$1"; }
c_warn() { printf '\033[33m!\033[0m %s\n' "$1"; }
c_err()  { printf '\033[31m✗\033[0m %s\n' "$1" >&2; }

echo "======================================================"
echo "  Cài base-meme-bot chạy 24/7"
echo "======================================================"
echo "  Thư mục : $APPDIR"
echo "  Chạy bởi: $RUN_USER"
echo

# ---------------------------------------------------------------- kiểm tra
if [[ ! -f "$APPDIR/runner.py" ]]; then
  c_err "Không thấy runner.py trong $APPDIR. Chạy script từ trong repo."
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  c_err "Máy này không có systemd. Trên macOS dùng deploy/com.memebot.plist thay thế."
  exit 1
fi

# ---------------------------------------------------------------- gói hệ thống
c_info "Cài gói hệ thống cần thiết..."
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y -q python3 python3-pip git ca-certificates
else
  c_warn "Không nhận ra trình quản lý gói — tự cài python3, python3-venv, git."
fi
c_ok "Gói hệ thống sẵn sàng ($(python3 --version))"

# ---------------------------------------------------------------- venv
c_info "Tạo môi trường ảo Python..."
if [[ ! -d "$APPDIR/venv" ]]; then
  python3 -m venv "$APPDIR/venv"
fi
"$APPDIR/venv/bin/pip" install --quiet --upgrade pip
"$APPDIR/venv/bin/pip" install --quiet -r "$APPDIR/requirements.txt"
c_ok "venv sẵn sàng"

# ---------------------------------------------------------------- .env
if [[ ! -f "$APPDIR/.env" ]]; then
  c_info "Tạo file .env mẫu..."
  cat > "$APPDIR/.env" <<'EOF'
# Điền token rồi lưu lại. KHÔNG commit file này lên git.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Tuỳ chọn: để bot tự đẩy state + dashboard lên GitHub.
# Personal Access Token quyền `repo` (classic) hoặc `contents: write` (fine-grained).
GITHUB_TOKEN=

# Tuỳ chọn: các nguồn dữ liệu trả phí.
BASESCAN_API_KEY=
BLOCKSCOUT_API_KEY=
EOF
  chmod 600 "$APPDIR/.env"
  c_warn "Đã tạo $APPDIR/.env — HÃY ĐIỀN TOKEN TELEGRAM trước khi bot chạy có ích."
else
  chmod 600 "$APPDIR/.env"
  c_ok ".env đã có (quyền 600)"
fi

# .env không bao giờ được lên git
if ! grep -qx '.env' "$APPDIR/.gitignore" 2>/dev/null; then
  echo '.env' >> "$APPDIR/.gitignore"
  c_ok "Đã thêm .env vào .gitignore"
fi

# ---------------------------------------------------------------- kiểm tra nhanh
c_info "Kiểm tra cấu hình (không gọi mạng)..."
if "$APPDIR/venv/bin/python" "$APPDIR/runner.py" --dry-run; then
  c_ok "Cấu hình hợp lệ"
else
  c_err "runner.py --dry-run thất bại. Dừng lại."
  exit 1
fi

# ---------------------------------------------------------------- systemd
c_info "Cài dịch vụ systemd..."
sudo sed -e "s|__APPDIR__|$APPDIR|g" -e "s|__USER__|$RUN_USER|g" \
     "$UNIT_SRC" | sudo tee "$UNIT_DST" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
sudo systemctl restart "$SERVICE_NAME"
sleep 3

if systemctl is-active --quiet "$SERVICE_NAME"; then
  c_ok "Dịch vụ đang chạy"
else
  c_err "Dịch vụ không khởi động được. Xem log:"
  sudo journalctl -u "$SERVICE_NAME" -n 30 --no-pager
  exit 1
fi

echo
echo "======================================================"
c_ok "Xong."
echo "======================================================"
cat <<EOF

  Xem log trực tiếp   : sudo journalctl -u $SERVICE_NAME -f
  Trạng thái          : sudo systemctl status $SERVICE_NAME
  Khởi động lại       : sudo systemctl restart $SERVICE_NAME
  Dừng                : sudo systemctl stop $SERVICE_NAME
  Báo cáo hiệu suất   : $APPDIR/venv/bin/python $APPDIR/base_meme_bot.py --paper-report

  Cập nhật code sau này:
      cd $APPDIR && git pull && bash deploy/install.sh

EOF

if ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$APPDIR/.env" 2>/dev/null; then
  c_warn "Chưa có TELEGRAM_BOT_TOKEN trong .env — bot vẫn quét nhưng không gửi được"
  c_warn "cảnh báo nào. Điền vào rồi: sudo systemctl restart $SERVICE_NAME"
fi
