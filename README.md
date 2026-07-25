# Base Meme Scanner — chạy trên GitHub Actions

Bot quét memecoin trên **Base** theo bộ tiêu chí K.O + dòng tiền định lượng, gửi cảnh báo qua **Telegram**. Chạy theo lịch bằng GitHub Actions, không cần server.

## ⚠️ Đọc trước khi deploy

- **Dùng repo PUBLIC.** Public = phút Actions miễn phí không giới hạn. Private chỉ ~2000 phút/tháng → chạy mỗi 5' sẽ cháy quota trong vài ngày. Token Telegram vẫn an toàn trong Secrets (không lộ ra ngoài).
- **Cron không real-time.** Tối thiểu 5 phút, thường trễ 10–30 phút lúc GitHub tải cao. Hợp bắt trend định kỳ, KHÔNG hợp snipe tính bằng giây.
- **Lịch chỉ chạy trên nhánh mặc định** (`main`).
- GitHub **không báo khi run lỗi** — thỉnh thoảng tự vào tab Actions xem.

## Cài đặt (5 bước)

1. **Tạo repo public** và đẩy toàn bộ file này lên (`base_meme_bot.py`, `requirements.txt`, `.github/workflows/scanner.yml`, `base_seen.json`).

2. **Tạo Telegram bot:** nhắn `/newbot` cho [@BotFather](https://t.me/BotFather) → lấy token. Nhắn gì đó cho bot của bạn trước (bắt buộc), rồi mở `https://api.telegram.org/bot<TOKEN>/getUpdates` để lấy `chat.id`.

3. **Thêm Secrets:** repo → **Settings → Secrets and variables → Actions → New repository secret**, tạo:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - (tuỳ chọn) `BASESCAN_API_KEY`, `TWEETSCOUT_API_KEY`, `SMART_MONEY_API_KEY` nếu cắm hook trả phí.

4. **Bật Actions:** tab **Actions** → nếu được hỏi thì bấm cho phép chạy workflow.

5. **Test tay:** tab **Actions → base-meme-scanner → Run workflow** (nút từ `workflow_dispatch`). Xem log + kiểm tra Telegram có nhận tin không. Ổn rồi thì lịch tự chạy mỗi 5 phút.

## Lưu ý vận hành

- **Lần chạy đầu** có thể bắn nhiều token cùng lúc (vì `base_seen.json` rỗng). Bot đã giới hạn `MAX_ALERTS_PER_RUN=8`/lần để chống flood; phần dư sẽ được gửi dần ở các lần sau. Muốn im hơn nữa thì tăng `--min-score` bằng cách sửa lệnh chạy trong workflow: `run: python base_meme_bot.py --min-score 65`.
- **State** (`base_seen.json`, `base_signals.csv`) được commit ngược lại repo sau mỗi lần chạy — vừa để không báo trùng, vừa giữ workflow không bị tự tắt sau 60 ngày.
- **Đổi tần suất:** sửa dòng `cron` trong `scanner.yml`. Ví dụ mỗi 15 phút: `'*/15 * * * *'` (đỡ tốn phút, GitHub cũng khuyến nghị ≥15' cho public free).
- **Muốn im các dòng "⚪ không có tín hiệu":** đặt `telegram_send_summary = False` trong `Config`.

## Giới hạn dữ liệu (không bịa)

Các hook `hook_fresh_wallet_ratio`, `hook_smart_money`, `hook_social_score` hiện trả `None` vì không có API miễn phí. Cắm nguồn trả phí vào chỗ `# TODO` trong `base_meme_bot.py` khi sẵn sàng.
