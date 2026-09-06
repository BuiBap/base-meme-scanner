# Chạy bot 24/7 trên Oracle Cloud Always Free

Hướng dẫn này đưa bot từ GitHub Actions (cron 5 phút, hay trễ 10–30') sang một VPS chạy liên tục với nhịp theo dõi vị thế **45 giây**.

---

## ⚠️ Ba điều phải biết trước khi bắt đầu

**1. GitHub Actions không dành cho việc này.** [Điều khoản của GitHub](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) cấm dùng Actions cho "hoạt động không liên quan đến việc phát triển, kiểm thử, triển khai hoặc phát hành phần mềm", và liệt kê cụ thể cả *serverless computing*. Một bot quét thị trường mỗi 5 phút, 24/7 rơi đúng vào vùng cấm này. Đã có tài khoản bị khoá vì workload tương tự. Rủi ro không phải mất con bot — mà là **mất cả tài khoản GitHub**. Sau khi chuyển sang VPS, nên tắt workflow cũ đi (xem bước 8).

**2. Oracle sẽ thu hồi instance "nhàn rỗi".** Oracle coi một instance là nhàn rỗi nếu **CPU percentile 95 dưới 20% trong 7 ngày liên tục**, và sẽ dừng nó. Bot này chỉ ăn 1–2% CPU nên **chắc chắn** bị đánh dấu.

  Cách xử lý đúng: **chuyển tài khoản sang Pay As You Go**. Instance Always Free sẽ không bị thu hồi nữa, và bạn **vẫn không bị tính tiền** miễn là chỉ dùng trong hạn mức Always Free. Cần thẻ tín dụng để xác minh. Việc này làm ở Console → Billing → Upgrade to Paid Account.

  (Có những công cụ tạo tải CPU giả để "trông bận rộn". Tôi không khuyến nghị: nó đốt điện của máy khác để lách một chính sách, và Oracle vẫn có thể siết tiếp bất cứ lúc nào.)

**3. Oracle vừa siết hạn mức.** Từ 15/06/2026 Ampere A1 miễn phí giảm từ 4 OCPU/24GB xuống **2 OCPU/12GB**, và các instance vượt hạn mức mới **bị xoá từ 18/08/2026**. Bot này chỉ cần ~50MB RAM nên không ảnh hưởng, nhưng đừng dựng instance to hơn mức cần.

**Sao lưu là bắt buộc, không phải tuỳ chọn.** Vì các lý do trên, hãy bật `GITHUB_TOKEN` ở bước 5 để bot tự đẩy `paper_trades.csv` lên GitHub mỗi giờ. Mất VPS thì vẫn còn dữ liệu hiệu suất.

---

## Bước 1 — Tạo instance

Console Oracle Cloud → **Compute → Instances → Create instance**.

| Mục | Chọn |
|---|---|
| Image | Canonical Ubuntu 24.04 |
| Shape | `VM.Standard.A1.Flex` với **1 OCPU / 6 GB** — hoặc `VM.Standard.E2.1.Micro` nếu A1 hết chỗ |
| Networking | mặc định, **có** public IPv4 |
| SSH keys | Upload khoá công khai của bạn (hoặc để Oracle sinh rồi tải file private về) |

Hai lưu ý:

- **"Out of host capacity"** là lỗi rất hay gặp với A1. Thử Availability Domain khác, hoặc đổi sang `E2.1.Micro` (yếu hơn nhiều nhưng vẫn thừa cho bot này), hoặc thử lại vào giờ khác.
- **Không cần mở cổng vào (ingress) nào cả.** Bot chỉ gọi ra ngoài. Ít cổng mở = ít thứ phải lo.

## Bước 2 — SSH vào máy

```bash
chmod 600 ~/Downloads/ssh-key.key          # nếu tải private key từ Oracle
ssh -i ~/Downloads/ssh-key.key ubuntu@<IP_PUBLIC>
```

## Bước 3 — Lấy code về

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/BuiBap/base-meme-scanner.git ~/base-meme-scanner
cd ~/base-meme-scanner
```

## Bước 4 — Cài đặt bằng một lệnh

```bash
bash deploy/install.sh
```

Script sẽ: cài Python + venv, cài thư viện, tạo `.env` mẫu, kiểm tra cấu hình, rồi cài và bật dịch vụ systemd. Chạy lại được nhiều lần — dùng chính lệnh này để cập nhật sau khi `git pull`.

## Bước 5 — Điền token

```bash
nano ~/base-meme-scanner/.env
```

```ini
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=987654321

# Tuỳ chọn nhưng nên có: để bot tự sao lưu state + dashboard lên GitHub.
GITHUB_TOKEN=ghp_...
```

`GITHUB_TOKEN` là Personal Access Token: GitHub → Settings → Developer settings → Personal access tokens. Classic thì tick scope `repo`; fine-grained thì chọn đúng repo này với quyền **Contents: Read and write**.

```bash
sudo systemctl restart base-meme-bot
```

## Bước 6 — Kiểm tra bot đang sống

```bash
sudo journalctl -u base-meme-bot -f
```

Bạn sẽ thấy log kiểu:

```
[06:12:03 UTC] khởi động · nhịp nhanh 45s · nhịp quét 150s
[06:12:03 UTC] ước tính dùng ~4.1 call/phút GeckoTerminal (trần đặt 25)
=== BASE scan 2026-08-09 06:12:05 UTC ===
  Khám phá: 47 pool
  Qua K.O tầng 1: 6
```

Và một tin Telegram "🚀 Bot khởi động lúc...". Không thấy tin đó nghĩa là token sai.

## Bước 7 — Kiểm tra tự khởi động lại

Đáng bỏ 30 giây để chắc chắn, vì đây là toàn bộ lý do dùng systemd:

```bash
sudo systemctl kill -s SIGKILL base-meme-bot   # giả lập bot chết đột ngột
sleep 20
systemctl is-active base-meme-bot              # phải in "active"
```

Rồi thử reboot máy: `sudo reboot`, đợi 1 phút, SSH lại và kiểm tra lần nữa.

## Bước 8 — Tắt workflow GitHub Actions cũ

Chạy song song sẽ khiến hai bên tranh nhau ghi state và bắn cảnh báo trùng.

```bash
# trên máy của bạn, trong repo local
git rm .github/workflows/scanner.yml
git commit -m "chuyển sang chạy 24/7 trên VPS"
git push
```

Hoặc giữ file lại nhưng vào tab Actions → workflow → **Disable workflow**.

## Bước 9 — Xem dashboard từ xa

Bot tự sinh `dashboard.html` và đẩy lên repo mỗi giờ. Để xem qua trình duyệt: repo → **Settings → Pages → Source: Deploy from a branch → main / (root)**. Sau vài phút, mở:

```
https://<tên-github>.github.io/base-meme-scanner/dashboard.html
```

**Repo public thì lịch sử giao dịch mô phỏng của bạn cũng public.** Không muốn thì để repo private (GitHub Pages cho repo private cần tài khoản trả phí), hoặc bỏ `dashboard.html` khỏi `STATE_FILES` trong `state_backup.py` và chỉ xem file trên VPS.

---

## Điều chỉnh nhịp quét

Sửa dòng `ExecStart` trong `/etc/systemd/system/base-meme-bot.service`:

```ini
ExecStart=/home/ubuntu/base-meme-scanner/venv/bin/python -u /home/ubuntu/base-meme-scanner/runner.py --fast 30 --slow 120 --min-score 65
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart base-meme-bot
```

Trước khi đổi, kiểm tra ngân sách API:

```bash
cd ~/base-meme-scanner && venv/bin/python runner.py --dry-run --fast 30 --slow 120
```

**Trần thật là GeckoTerminal, không phải VPS.** API free cho 30 call/phút; bot đặt trần 25 để chừa biên. Đặt nhịp quá dày thì bot không lỗi — nó chỉ tự xếp hàng chờ, và nhịp theo dõi vị thế bị trễ. Mà nhịp đó chính là thứ quyết định TP/SL khớp sát tới đâu, nên đừng hy sinh nó để quét token mới dày hơn.

| Nhịp nhanh | Nhịp quét | Call/phút | Ghi chú |
|---|---|---|---|
| 60s | 300s | ~2.4 | Rất nhẹ nhàng |
| **45s** | **150s** | **~4.1** | **Mặc định** |
| 30s | 120s | ~5.5 | Nhanh hơn, vẫn dư quota |
| 15s | 60s | ~11 | Sát hơn, chỉ nên dùng khi giữ nhiều vị thế |
| 5s | 30s | ~26 | Quá trần — bot sẽ tự chờ |

---

## Vận hành hằng ngày

```bash
# xem log
sudo journalctl -u base-meme-bot -f
sudo journalctl -u base-meme-bot --since "1 hour ago" | grep -i "lỗi\|429"

# báo cáo hiệu suất
cd ~/base-meme-scanner
venv/bin/python base_meme_bot.py --paper-report
venv/bin/python base_meme_bot.py --paper-report-telegram   # gửi vào Telegram

# sao lưu ngay, không đợi nhịp 1h
venv/bin/python state_backup.py
venv/bin/python state_backup.py --status                   # kiểm tra cấu hình

# cập nhật code
cd ~/base-meme-scanner && git pull && bash deploy/install.sh
```

Log do systemd journal quản lý nên tự xoay vòng, không lo đầy ổ. Muốn chặn cứng:

```bash
sudo journalctl --vacuum-size=200M
```

---

## Khi có sự cố

**Bot không khởi động**

```bash
sudo systemctl status base-meme-bot
sudo journalctl -u base-meme-bot -n 50 --no-pager
```

Nguyên nhân hay gặp: sai đường dẫn trong unit file (chạy lại `install.sh`), thiếu venv, hoặc `.env` có ký tự lạ — file này phải là `KEY=value` thuần, không có dấu nháy, không có khoảng trắng quanh dấu `=`.

**Bot khởi động rồi chết lặp lại**

systemd sẽ bỏ cuộc sau 5 lần sập trong 10 phút. Gỡ chốt bằng:

```bash
sudo systemctl reset-failed base-meme-bot
```

Nhưng phải đọc log tìm nguyên nhân trước — nếu lỗi do cấu hình thì restart bao nhiêu lần cũng vô ích.

**Telegram im lặng** — bot heartbeat 6 giờ một lần. Quá lâu không thấy gì nghĩa là bot chết hoặc token sai:

```bash
cd ~/base-meme-scanner && venv/bin/python base_meme_bot.py --test-telegram
```

**Log đầy `[429]`** — đang bị rate limit. Tăng `--fast` và `--slow`. Kiểm tra ngân sách hiện tại trong log heartbeat (dòng "ngân sách API").

**Instance Oracle biến mất** — nhiều khả năng bị thu hồi vì nhàn rỗi. Dựng instance mới, `git clone` lại (state đã được sao lưu trên GitHub), chạy `install.sh`. Và lần này chuyển tài khoản sang Pay As You Go.

---

## Chạy trên máy tính cá nhân thay vì VPS

Bot chỉ cần Python và mạng, nên máy nào cũng chạy được — đổi lại máy phải luôn bật.

**Linux / Raspberry Pi:** giống hệt hướng dẫn trên, chạy `bash deploy/install.sh`.

**macOS:** systemd không có, dùng launchd:

```bash
cd ~/base-meme-scanner
python3 -m venv venv && venv/bin/pip install -r requirements.txt
sed -e "s|__APPDIR__|$PWD|g" deploy/com.memebot.plist \
    > ~/Library/LaunchAgents/com.memebot.plist
launchctl load ~/Library/LaunchAgents/com.memebot.plist
tail -f ~/base-meme-scanner/bot.log
```

Nhớ tắt chế độ ngủ (System Settings → Energy), nếu không máy ngủ là bot ngừng.

**Windows:** dùng WSL2 rồi làm theo hướng dẫn Linux, hoặc Task Scheduler với trigger "At startup".

---

## Chi phí ước tính

| Phương án | Chi phí | Ghi chú |
|---|---|---|
| Oracle Always Free | 0đ | Cần thẻ để xác minh; nên nâng lên PAYG để khỏi bị thu hồi (vẫn 0đ trong hạn mức) |
| Hetzner CX22 | ~€4/tháng | Ổn định, không phải lo capacity |
| RackNerd | ~$1–2/tháng | Trả theo năm, giá khuyến mãi |
| Máy tại nhà / Pi | Tiền điện | Pi 4 chạy 24/7 tốn khoảng 15–25 kWh/năm |

Bot dùng ~50MB RAM và 1–2% CPU, nên gói rẻ nhất ở đâu cũng thừa.
