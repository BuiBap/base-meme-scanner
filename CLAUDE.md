# Base Meme Scanner — định tuyến dự án

Bot quét memecoin trên Base, cảnh báo Telegram, paper trading. Remote: `github.com/BuiBap/base-meme-scanner` (nhánh `main`). GitHub Actions tự commit file state mỗi 5 phút.

## Đội agent (`.claude/agents/`)

| Agent | Model | Lane | Khi nào gọi | Sửa code? |
|---|---|---|---|---|
| `fable-advisor` | Fable | Cố vấn | Thẩm định mô hình, soát cổng G1/G2/G3, kiến trúc, viết SPEC BÀN GIAO | Không |
| `quant-researcher` | Sonnet | R1 + R2 | Tin tức, on-chain, nguồn dữ liệu; biến ý tưởng thành luật | Không |
| `backtester` | Opus | R3 | Kiểm định luật đã rõ; chỉ ghi vào `research/` | Chỉ `research/` |
| `coder` | Opus | R4 + code | Spec đã chốt → viết code, test, push GitHub | **Có, duy nhất** |

## Luồng chuẩn

```
ý tưởng ─► quant-researcher (R2) ─G1─► backtester (R3) ─G2─► fable-advisor duyệt ─► coder ─► GitHub
```

- Người dùng nói "giao coder", "viết code", "đẩy lên GitHub" → gọi thẳng `coder` với spec đã có.
- Spec chưa rõ → gọi `fable-advisor` trước, không để `coder` đoán.
- Chỉ `coder` được sửa code bot và push. Agent khác đề xuất, `coder` thực thi.
- Mỗi cổng chặn một kiểu mất tiền: G1 = luật đủ rõ để code; G2 = có out-of-sample sau phí; G3 = paper đủ lâu + kill switch trước khi chạy tiền thật.

## Đẩy code

Luôn dùng `.claude/ship.sh "<message mô tả>"` thay cho `git add -A && git commit -m "update" && git push`. Lệnh trơn sẽ bị từ chối khi CI vừa commit state, và dễ gây conflict ở `base_seen.json` / `base_signals.csv`.

## Test

`python3 test_runner.py` và `python3 test_paper_trader.py` — không gọi mạng, phải pass hết trước khi push.
