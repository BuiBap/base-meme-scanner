# Base Meme Scanner

Bot quét memecoin trên **Base** theo bộ tiêu chí K.O + dòng tiền định lượng, gửi cảnh báo qua **Telegram**, và **mô phỏng giao dịch** để đo hiệu suất của chính bộ lọc.

Có hai cách chạy:

| | GitHub Actions | VPS 24/7 (`runner.py`) |
|---|---|---|
| Nhịp quét token mới | 5 phút (thực tế trễ 10–30') | 150 giây |
| Nhịp theo dõi vị thế | 5 phút | **45 giây** |
| Độ chính xác TP/SL mô phỏng | thô | sát hơn nhiều |
| Chi phí | 0đ | 0đ (Oracle free) đến ~$4/tháng |
| Rủi ro | ⚠️ vi phạm ToS của GitHub | không |

**Chạy 24/7 → xem [DEPLOY.md](DEPLOY.md).** Khuyến nghị chuyển hẳn: điều khoản GitHub cấm dùng Actions cho workload không phải CI/CD, và hình phạt là khoá cả tài khoản.

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
- **State** (`base_seen.json`, `base_signals.csv`, `paper_*.json/csv`) được commit ngược lại repo sau mỗi lần chạy — vừa để không báo trùng, vừa giữ workflow không bị tự tắt sau 60 ngày.
- **Đổi tần suất:** sửa dòng `cron` trong `scanner.yml`. Ví dụ mỗi 15 phút: `'*/15 * * * *'` (đỡ tốn phút, GitHub cũng khuyến nghị ≥15' cho public free).
- **Muốn im các dòng "⚪ không có tín hiệu":** đặt `telegram_send_summary = False` trong `Config`.

## 📊 Paper trading — đo hiệu suất của bot

`paper_trader.py` mô phỏng giao dịch để trả lời câu hỏi **"bộ lọc này có kiếm được tiền không?"**. Mỗi tín hiệu bot bắn ra sẽ mở một vị thế **ẢO**; các lần quét sau đó cập nhật giá và chốt lệnh theo luật. **Không có ví, không có private key, không giao dịch thật.**

### Luật chốt lệnh (mặc định)

| Điều kiện | Ngưỡng |
|---|---|
| Take profit | +100% |
| Stop loss | −30% |
| Trailing stop | bật sau khi lãi ≥ +40%, chốt khi tụt 25% từ đỉnh |
| Timeout | giữ tối đa 24h |
| Rug | thanh khoản < $5k hoặc giá = 0 → chốt ngay |

Chi phí mô phỏng, tính **cả hai chiều**: slippage ước tính theo thanh khoản pool (constant-product AMM) + phí DEX 0.3% + gas $0.05. Size mặc định $100/lệnh.

Sửa toàn bộ các con số này trong `PaperConfig` ở đầu `paper_trader.py`.

### Xem báo cáo

```bash
python base_meme_bot.py --paper-report            # bảng thống kê đầy đủ trong terminal
python base_meme_bot.py --paper-report-telegram   # gửi báo cáo qua Telegram ngay
python paper_trader.py --report                   # tương đương, chạy độc lập
```

Báo cáo gồm: win rate, PnL tổng, TB/median mỗi lệnh, expectancy, profit factor, max drawdown, thời gian giữ TB, MFE/MAE trung bình, và **bảng phân tích theo bậc điểm** — đây là phần đáng giá nhất, nó cho biết score 85+ có thực sự tốt hơn score 55–64 hay không, để bạn biết nên kéo `min_score_to_alert` lên bao nhiêu.

Telegram tự động nhận: 1 tin mỗi khi có lệnh đóng, và 1 bản tổng kết mỗi 24h. Tắt bằng `telegram_on_close = False` / `telegram_daily_report = False`.

### File state (đều tự commit ngược repo)

- `paper_positions.json` — vị thế đang mở
- `paper_trades.csv` — lịch sử lệnh đã đóng, 1 dòng/lệnh (đủ cột để tự phân tích bằng Excel/pandas)
- `paper_state.json` — mốc báo cáo cuối

### ⚠️ Giới hạn cần biết trước khi tin số liệu

- **Mark-to-market rời rạc.** Vị thế chỉ được kiểm tra khi scanner chạy (cron 5 phút, Actions còn hay trễ 10–30'), nên mỗi khoảng ta chỉ thấy **một** mức giá. TP/SL khớp ở giá quan sát được, không phải đúng giá ngưỡng — SL đặt −30% hoàn toàn có thể được ghi là −60% nếu giá nhảy qua.
- **Đường đi trong khoảng là vô hình.** Một cú vọt +200% rồi sập về −50% trong cùng 5 phút sẽ chỉ được ghi nhận là −50%. Với memecoin, đây là sai số lớn nhất của mô hình và nó có thể lệch về **cả hai** phía, không chỉ phía thận trọng.
- Khi một mức giá thoả nhiều luật, thứ tự ưu tiên là `rug → stop loss → trailing → take profit → timeout`.
- **Không mô phỏng được**: MEV/sandwich, giao dịch fail, độ sâu pool thật, hay việc lệnh của bạn tự làm dịch giá nhiều hơn ước tính. Cột `mfe_pct` / `mae_pct` trong CSV giúp bạn thấy khoảng dao động đã bỏ lỡ.
- **Sinh tồn thống kê**: dưới ~30 lệnh đóng thì mọi con số đều là nhiễu. Cần vài tuần dữ liệu mới nói được gì. Và mọi kết quả đều là mô phỏng — không phải cam kết lợi nhuận nếu vào tiền thật.

## 🔁 Chạy liên tục 24/7

`runner.py` thay cho việc gọi `base_meme_bot.py` theo cron. Nó chạy **hai nhịp độc lập**:

- **nhịp nhanh (45s)** — cập nhật giá các vị thế ảo, chốt lệnh chạm ngưỡng. Rất rẻ: 1 call cho mỗi 30 pool.
- **nhịp chậm (150s)** — quét pool mới, chấm điểm, gửi cảnh báo. Đắt hơn nhưng không cần dày.

Tách ra như vậy vì TP/SL mô phỏng chỉ chính xác tới mức bạn lấy giá thường xuyên, còn token mới thì không xuất hiện mỗi 45 giây. Gộp chung một nhịp là ép cả hai vào tần suất sai.

```bash
python runner.py                      # nhịp mặc định
python runner.py --fast 30 --slow 120 # nhanh hơn
python runner.py --dry-run            # xem ước tính ngân sách API rồi thoát
```

Kèm theo: tự khởi động lại khi lỗi (lùi dần theo cấp số nhân), báo Telegram khi lỗi dồn và khi hồi phục, heartbeat 6 giờ/lần, tắt êm khi nhận SIGTERM (lưu state xong mới thoát).

**Trần thật là API, không phải máy chủ.** GeckoTerminal free cho 30 call/phút. `rate_limit.py` quản lý ngân sách chung bằng token bucket — cho phép bắn dồn khi cần rồi tự nghỉ bù, và tự phanh khi gặp 429. `--dry-run` sẽ cảnh báo nếu nhịp bạn đặt vượt ngân sách.

**Dashboard.** `dashboard.py` sinh `dashboard.html` tự chứa (equity curve, vị thế đang mở, phân tích theo bậc điểm). `state_backup.py` đẩy nó cùng toàn bộ state lên GitHub mỗi giờ — vừa là sao lưu, vừa cho phép xem hiệu suất qua GitHub Pages mà không cần SSH vào máy chủ.

## Kiểm thử

```bash
python test_paper_trader.py   # 74 assertion — engine mô phỏng
python test_runner.py         # 67 assertion — rate limiter, runner, dashboard, backup
```

Không gọi mạng, không đụng state thật.

## Giới hạn dữ liệu (không bịa)

Các hook `hook_fresh_wallet_ratio`, `hook_smart_money`, `hook_social_score` hiện trả `None` vì không có API miễn phí. Cắm nguồn trả phí vào chỗ `# TODO` trong `base_meme_bot.py` khi sẵn sàng.
