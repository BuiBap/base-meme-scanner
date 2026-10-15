#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_paper_trader.py — kiểm thử engine paper trading bằng giá giả lập.

Chạy:  python test_paper_trader.py
Không gọi mạng. Tự dọn file tạm sau khi chạy.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_trader as pt


# --------------------------------------------------------------------------- #
#  Giả lập Candidate của bot (chỉ những field paper trader dùng)
# --------------------------------------------------------------------------- #

@dataclass
class FakeCandidate:
    token_address: str
    pool_address: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    market_cap: float = 500_000.0
    age_hours: float = 5.0
    score: float = 70.0
    reasons: list = field(default_factory=lambda: ["test"])
    url: str = "https://example.test"


class FakeQuotes:
    """price_fetcher giả: trả giá theo dict mình set."""

    def __init__(self):
        self.book: dict[str, dict] = {}
        self.calls = 0

    def set(self, pool: str, price: float, liquidity: float = 100_000.0):
        self.book[pool.lower()] = {"price": price, "liquidity": liquidity}

    def drop(self, pool: str):
        self.book.pop(pool.lower(), None)

    def __call__(self, pools: list) -> dict:
        self.calls += 1
        return {p: self.book[p] for p in pools if p in self.book}


def make_cfg(tmp: str, **over) -> pt.PaperConfig:
    cfg = pt.PaperConfig(
        positions_file=os.path.join(tmp, "pos.json"),
        trades_csv=os.path.join(tmp, "trades.csv"),
        state_file=os.path.join(tmp, "state.json"),
        telegram_on_close=False,
        telegram_daily_report=False,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail else ""))


def cand(sym="TEST", price=1.0, liq=100_000.0, score=70.0, n=1):
    return FakeCandidate(
        token_address=f"0x{sym.lower()}{n:040x}"[:42],
        pool_address=f"0xpool{sym.lower()}{n:040x}"[:66],
        symbol=sym, price_usd=price, liquidity_usd=liq, score=score,
    )


# --------------------------------------------------------------------------- #

def test_open_and_fees():
    print("\n[1] Mở lệnh: slippage + phí + gas được trừ đúng")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        c = cand(price=1.0, liq=100_000.0)
        opened = t.open_from_signals([c])
        check("mở được 1 vị thế", len(opened) == 1)
        p = opened[0]

        # slippage kỳ vọng: 100 / (50000 + 100) * 100 = 0.1996%
        slip = pt.estimate_slippage_pct(100_000.0, 100.0)
        expect_eff = 1.0 * (1 + slip / 100) * (1 + 0.3 / 100)
        check("giá vào hiệu dụng > giá niêm yết",
              p.entry_price_eff > p.entry_price,
              f"{p.entry_price_eff:.6f} vs {p.entry_price}")
        check("giá vào hiệu dụng khớp công thức",
              abs(p.entry_price_eff - expect_eff) < 1e-12,
              f"{p.entry_price_eff:.10f} vs {expect_eff:.10f}")
        # tokens = (100 - gas 0.05) / eff
        check("số token khớp (size - gas)/eff",
              abs(p.tokens - (100 - 0.05) / expect_eff) < 1e-9)

        # đóng ngay ở đúng giá vào -> phải LỖ vì phí 2 chiều
        q = FakeQuotes(); q.set(p.pool_address, 1.0, 100_000.0)
        cfg.stop_loss_pct = 0.0             # ép chốt ngay (pnl 0% <= 0%)
        closed = t.mark_to_market(q)
        check("đóng ở giá vào = lỗ do phí", len(closed) == 1 and closed[0]["pnl_usd"] < 0,
              f"pnl={closed[0]['pnl_usd']:.3f}$" if closed else "không đóng")
        if closed:
            # phí 2 chiều ~ 0.3%*2 + slip*2 + gas*2 ≈ 1.1% của $100
            loss = abs(closed[0]["pnl_usd"])
            check("mức lỗ do phí nằm trong 0.5–3%", 0.5 <= loss <= 3.0,
                  f"{loss:.3f}$")
    finally:
        shutil.rmtree(tmp)


def test_take_profit():
    print("\n[2] Take profit +100%")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()

        q.set(p.pool_address, 1.5)          # +50%: chưa chốt
        check("+50% chưa chốt", len(t.mark_to_market(q)) == 0)

        q.set(p.pool_address, 2.05)         # +105%: chốt
        closed = t.mark_to_market(q)
        check("+105% chốt lời", len(closed) == 1 and closed[0]["exit_reason"] == "take_profit",
              closed[0]["exit_reason"] if closed else "-")
        if closed:
            check("PnL dương và < +105% (đã trừ phí)",
                  0 < closed[0]["pnl_pct"] < 105,
                  f"{closed[0]['pnl_pct']:+.2f}%")
            check("MFE ghi đúng đỉnh 105%",
                  abs(closed[0]["mfe_pct"] - 105.0) < 0.01,
                  f"{closed[0]['mfe_pct']:+.2f}%")
        check("không còn vị thế mở", len(t.positions) == 0)
    finally:
        shutil.rmtree(tmp)


def test_stop_loss():
    print("\n[3] Stop loss −30%")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()

        q.set(p.pool_address, 0.75)         # -25%: giữ
        check("-25% chưa cắt", len(t.mark_to_market(q)) == 0)

        q.set(p.pool_address, 0.68)         # -32%: cắt
        closed = t.mark_to_market(q)
        check("-32% cắt lỗ", len(closed) == 1 and closed[0]["exit_reason"] == "stop_loss")
        if closed:
            check("lỗ tệ hơn -32% (do phí)", closed[0]["pnl_pct"] < -32,
                  f"{closed[0]['pnl_pct']:+.2f}%")
            check("MAE ghi đúng đáy -32%",
                  abs(closed[0]["mae_pct"] - (-32.0)) < 0.01,
                  f"{closed[0]['mae_pct']:+.2f}%")
    finally:
        shutil.rmtree(tmp)


def test_trailing():
    print("\n[4] Trailing stop (bật sau +40%, chốt khi tụt 25% từ đỉnh)")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()

        # lên +30% rồi tụt 25% từ đỉnh -> CHƯA bật trailing (chưa qua +40%)
        q.set(p.pool_address, 1.30); t.mark_to_market(q)
        q.set(p.pool_address, 1.30 * 0.74)   # = 0.962, tức -3.8% so với vào
        check("chưa qua +40% thì trailing không bật", len(t.mark_to_market(q)) == 0,
              f"giá {1.30*0.74:.4f}")

        # lên +80% rồi tụt 26% từ đỉnh -> chốt
        q.set(p.pool_address, 1.80); t.mark_to_market(q)
        check("đỉnh được ghi nhận 1.80", abs(t.positions[p.trade_id].peak_price - 1.80) < 1e-9)
        q.set(p.pool_address, 1.80 * 0.74)   # = 1.332 => +33.2% so với vào
        closed = t.mark_to_market(q)
        check("tụt 26% từ đỉnh -> trailing stop",
              len(closed) == 1 and closed[0]["exit_reason"] == "trailing_stop",
              closed[0]["exit_reason"] if closed else "-")
        if closed:
            check("vẫn chốt có lãi (~+33% trừ phí)", closed[0]["pnl_pct"] > 25,
                  f"{closed[0]['pnl_pct']:+.2f}%")
    finally:
        shutil.rmtree(tmp)


def test_timeout():
    print("\n[5] Timeout 24h")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp, max_hold_hours=24.0)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes(); q.set(p.pool_address, 1.10)

        check("mới mở chưa timeout", len(t.mark_to_market(q)) == 0)

        # lùi entry_at về 25h trước
        t.positions[p.trade_id].entry_at = pt._iso(time.time() - 25 * 3600)
        closed = t.mark_to_market(q)
        check("quá 24h -> timeout", len(closed) == 1 and closed[0]["exit_reason"] == "timeout")
        if closed:
            check("hold_hours ~25h", 24.5 < closed[0]["hold_hours"] < 25.5,
                  f"{closed[0]['hold_hours']:.2f}h")
    finally:
        shutil.rmtree(tmp)


def test_rug():
    print("\n[6] Rug: thanh khoản cạn / giá về 0")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        a = t.open_from_signals([cand("AAA", 1.0, n=1)])[0]
        b = t.open_from_signals([cand("BBB", 1.0, n=2)])[0]
        q = FakeQuotes()
        q.set(a.pool_address, 0.9, liquidity=1_000.0)   # liq < $5k
        q.set(b.pool_address, 0.0, liquidity=90_000.0)  # giá 0
        closed = t.mark_to_market(q)
        reasons = sorted(c["exit_reason"] for c in closed)
        check("cả hai bị chốt vì rug", reasons == ["rug_liquidity", "rug_price_zero"],
              str(reasons))
        zero = [c for c in closed if c["exit_reason"] == "rug_price_zero"]
        check("giá 0 -> mất gần hết vốn",
              zero and zero[0]["pnl_pct"] <= -99, f"{zero[0]['pnl_pct']:.1f}%" if zero else "-")
    finally:
        shutil.rmtree(tmp)


def test_missing_quote():
    print("\n[7] Mất dữ liệu giá: KHÔNG được chốt bừa")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()                     # rỗng: pool không trả dữ liệu
        check("không quote -> giữ nguyên vị thế", len(t.mark_to_market(q)) == 0)
        check("vị thế vẫn mở", len(t.positions) == 1)
        check("checks không tăng", t.positions[p.trade_id].checks == 0)

        # nhưng quá hạn giữ thì vẫn phải đóng theo giá cuối biết được
        t.positions[p.trade_id].entry_at = pt._iso(time.time() - 30 * 3600)
        closed = t.mark_to_market(q)
        check("quá hạn + mất quote -> timeout_no_quote",
              len(closed) == 1 and closed[0]["exit_reason"] == "timeout_no_quote")
    finally:
        shutil.rmtree(tmp)


def test_dedup_and_persistence():
    print("\n[8] Không mở trùng token + state đọc/ghi lại được")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        c = cand("DUP", 1.0)
        t.open_from_signals([c])
        t.open_from_signals([c])             # cùng token, lần 2
        check("chặn mở trùng token", len(t.positions) == 1)

        # đọc lại từ file bằng instance mới
        t2 = pt.PaperTrader(cfg, verbose=False)
        check("load lại đúng số vị thế", len(t2.positions) == 1)
        p1 = list(t.positions.values())[0]
        p2 = list(t2.positions.values())[0]
        check("dữ liệu vị thế nguyên vẹn sau reload",
              p1.trade_id == p2.trade_id and abs(p1.tokens - p2.tokens) < 1e-12)

        # cho phép trùng khi tắt cờ
        cfg.one_position_per_token = False
        t2.open_from_signals([c])
        check("tắt one_position_per_token -> cho mở thêm", len(t2.positions) == 2)
    finally:
        shutil.rmtree(tmp)


def test_max_open():
    print("\n[9] Giới hạn số vị thế mở")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp, max_open_positions=3)
        t = pt.PaperTrader(cfg, verbose=False)
        t.open_from_signals([cand(f"T{i}", 1.0, n=i) for i in range(10)])
        check("dừng ở đúng 3 vị thế", len(t.positions) == 3, str(len(t.positions)))
    finally:
        shutil.rmtree(tmp)


def test_bad_input():
    print("\n[10] Đầu vào rác không làm sập")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        bad = [
            cand("ZERO", 0.0),                              # giá 0
            FakeCandidate("", "", "EMPTY", 1.0, 1000.0),    # thiếu địa chỉ
        ]
        opened = t.open_from_signals(bad)
        check("bỏ qua tín hiệu giá 0 / thiếu địa chỉ", len(opened) == 0)

        # price_fetcher ném exception
        def boom(_):
            raise RuntimeError("API chết")
        t.open_from_signals([cand("OK", 1.0)])
        check("price_fetcher lỗi -> không sập, không chốt bừa",
              t.mark_to_market(boom) == [] and len(t.positions) == 1)

        # file positions hỏng
        with open(cfg.positions_file, "w") as fp:
            fp.write("{ đây không phải json")
        t3 = pt.PaperTrader(cfg, verbose=False)
        check("file state hỏng -> khởi động lại rỗng, không sập", len(t3.positions) == 0)
    finally:
        shutil.rmtree(tmp)


def test_stats_math():
    print("\n[11] Số liệu thống kê tính đúng (đối chiếu tay)")
    # 5 lệnh, size $100: +60, -30, +20, -30, -10  => tổng +10
    trades = []
    for i, (pnl, sym, score, reason) in enumerate([
        (60.0, "A", 90.0, "take_profit"),
        (-30.0, "B", 60.0, "stop_loss"),
        (20.0, "C", 80.0, "trailing_stop"),
        (-30.0, "D", 60.0, "stop_loss"),
        (-10.0, "E", 70.0, "timeout"),
    ]):
        trades.append({
            "trade_id": f"id{i}", "symbol": sym, "score": score,
            "size_usd": 100.0, "pnl_usd": pnl, "pnl_pct": pnl,
            "exit_reason": reason, "hold_hours": 2.0, "fees_usd": 1.0,
            "mfe_pct": max(pnl, 0), "mae_pct": min(pnl, 0),
            "exit_at": pt._iso(time.time() + i * 60),
        })
    s = pt.compute_stats(trades, starting_equity=1000.0)

    check("n = 5", s["n"] == 5)
    check("wins = 2, losses = 3", s["wins"] == 2 and s["losses"] == 3)
    check("win rate = 40%", s["win_rate"] == 40.0, str(s["win_rate"]))
    check("PnL tổng = +$10", abs(s["pnl_usd"] - 10.0) < 1e-9, str(s["pnl_usd"]))
    check("vốn triển khai = $500", abs(s["deployed_usd"] - 500.0) < 1e-9)
    check("ROI = +2%", abs(s["roi_pct"] - 2.0) < 1e-9, str(s["roi_pct"]))
    check("expectancy = +$2/lệnh", abs(s["expectancy_usd"] - 2.0) < 1e-9)
    check("TB/lệnh = +2%", abs(s["avg_pnl_pct"] - 2.0) < 1e-9)
    check("median = -10%", abs(s["median_pnl_pct"] - (-10.0)) < 1e-9, str(s["median_pnl_pct"]))
    check("avg win = +40%", abs(s["avg_win_pct"] - 40.0) < 1e-9)
    check("avg loss = -23.33%", abs(s["avg_loss_pct"] - (-23.33)) < 0.01)
    # profit factor = 80 / 70 = 1.142857
    check("profit factor = 1.14", abs(s["profit_factor"] - 1.14) < 0.01, str(s["profit_factor"]))
    # equity: 1060, 1030, 1050, 1020, 1010 -> peak 1060, đáy sau peak 1010 -> DD 50
    check("max drawdown = $50", abs(s["max_drawdown_usd"] - 50.0) < 1e-9,
          str(s["max_drawdown_usd"]))
    check("equity curve có 5 điểm, cuối = 1010",
          len(s["equity_curve"]) == 5 and abs(s["equity_curve"][-1]["equity"] - 1010) < 1e-9)
    check("best = A (+60%)", s["best"]["symbol"] == "A")
    check("worst là lệnh -30%", s["worst"]["pnl_pct"] == -30.0)
    check("nhóm theo lý do: stop_loss n=2",
          s["by_reason"]["stop_loss"]["n"] == 2)
    check("nhóm theo điểm: bậc <65 n=2, win 0%",
          s["by_score"]["<65"]["n"] == 2 and s["by_score"]["<65"]["win_rate"] == 0.0)
    check("nhóm theo điểm: bậc 85+ n=1, win 100%",
          s["by_score"]["85+"]["n"] == 1 and s["by_score"]["85+"]["win_rate"] == 100.0)

    e = pt.compute_stats([], 1000.0)
    check("danh sách rỗng -> stats rỗng an toàn", e["n"] == 0 and e["profit_factor"] is None)

    nolose = pt.compute_stats([trades[0]], 1000.0)
    check("không có lệnh lỗ -> profit factor = ∞",
          nolose["profit_factor"] is None and "∞" in nolose["profit_factor_str"])


def test_csv_roundtrip():
    print("\n[12] CSV ghi/đọc lại khớp, report chạy không lỗi")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        for i in range(3):
            p = t.open_from_signals([cand(f"S{i}", 1.0, n=i)])[0]
            q = FakeQuotes(); q.set(p.pool_address, 2.2)
            t.mark_to_market(q)

        rows = pt.load_trades(cfg.trades_csv)
        check("đọc lại 3 dòng CSV", len(rows) == 3, str(len(rows)))
        check("đủ cột", all(set(pt.TRADE_FIELDS) <= set(r.keys()) for r in rows))
        check("header chỉ ghi 1 lần",
              open(cfg.trades_csv, encoding="utf-8").read().count("trade_id,symbol") == 1)

        s = pt.compute_stats(rows, cfg.starting_equity_usd)
        check("stats từ CSV: 3 lệnh, win rate 100%", s["n"] == 3 and s["win_rate"] == 100.0)

        txt = t.telegram_report_text(rows)
        check("text Telegram sinh được", "Hiệu suất mô phỏng" in txt and len(txt) < 4000)
        pt.print_report(rows, cfg)           # không được ném exception
        check("print_report chạy xong", True)
    finally:
        shutil.rmtree(tmp)


def test_disabled():
    print("\n[13] Tắt paper trading -> không làm gì")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp, enabled=False)
        t = pt.PaperTrader(cfg, verbose=False)
        check("không mở lệnh khi enabled=False", t.open_from_signals([cand()]) == [])
        check("không ghi file positions", not os.path.exists(cfg.positions_file))
    finally:
        shutil.rmtree(tmp)


def test_exit_priority():
    print("\n[14] Thứ tự ưu tiên khi 1 giá quan sát thoả nhiều luật")

    # (a) SL và trailing cùng thoả -> chọn SL (mô tả đúng mức lỗ thực tế)
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()
        q.set(p.pool_address, 1.90)          # +90%: bật trailing, chưa chạm TP
        t.mark_to_market(q)
        q.set(p.pool_address, 0.50)          # -50%: cả SL và trailing đều thoả
        closed = t.mark_to_market(q)
        check("SL thắng trailing khi cả hai thoả",
              len(closed) == 1 and closed[0]["exit_reason"] == "stop_loss",
              closed[0]["exit_reason"] if closed else "-")
        if closed:
            check("MFE vẫn ghi +90% để phân tích sau",
                  abs(closed[0]["mfe_pct"] - 90.0) < 0.01,
                  f"{closed[0]['mfe_pct']:+.1f}%")
            check("lỗ ghi nhận tệ hơn ngưỡng SL -30% (giá nhảy qua ngưỡng)",
                  closed[0]["pnl_pct"] < -49,
                  f"{closed[0]['pnl_pct']:+.1f}%")
    finally:
        shutil.rmtree(tmp)

    # (b) TP và trailing cùng thoả -> chọn trailing (mô tả đúng: đã rời đỉnh)
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()
        q.set(p.pool_address, 3.00)          # +200% -> TP chốt ngay ở lần này
        closed = t.mark_to_market(q)
        check("giá vọt qua TP thì chốt ngay lần quan sát đó",
              len(closed) == 1 and closed[0]["exit_reason"] == "take_profit",
              closed[0]["exit_reason"] if closed else "-")

        # dựng lại tình huống đỉnh cao hơn TP nhưng bot bỏ lỡ nhịp quan sát:
        # đặt TP cao để nhịp đầu không chốt, đỉnh vẫn được ghi.
        cfg2 = make_cfg(tmp, take_profit_pct=500.0)
        t2 = pt.PaperTrader(cfg2, verbose=False)
        p2 = t2.open_from_signals([cand("XX", 1.0, n=9)])[0]
        q2 = FakeQuotes()
        q2.set(p2.pool_address, 3.00)        # đỉnh +200%, chưa tới TP 500%
        t2.mark_to_market(q2)
        q2.set(p2.pool_address, 2.10)        # tụt 30% từ đỉnh -> trailing
        closed2 = t2.mark_to_market(q2)
        check("rời đỉnh 30% -> trailing chốt, vẫn lãi lớn",
              len(closed2) == 1 and closed2[0]["exit_reason"] == "trailing_stop"
              and closed2[0]["pnl_pct"] > 100,
              f"{closed2[0]['exit_reason']} {closed2[0]['pnl_pct']:+.0f}%" if closed2 else "-")
    finally:
        shutil.rmtree(tmp)

    # (c) rug đứng trên tất cả
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        p = t.open_from_signals([cand(price=1.0)])[0]
        q = FakeQuotes()
        q.set(p.pool_address, 2.50, liquidity=800.0)   # giá thoả TP nhưng liq cạn
        closed = t.mark_to_market(q)
        check("thanh khoản cạn -> rug thắng cả take_profit",
              len(closed) == 1 and closed[0]["exit_reason"] == "rug_liquidity",
              closed[0]["exit_reason"] if closed else "-")
        if closed:
            check("thoát pool cạn bị slippage ăn nặng",
                  closed[0]["pnl_pct"] < 100,
                  f"{closed[0]['pnl_pct']:+.1f}% (danh nghĩa +150%)")
    finally:
        shutil.rmtree(tmp)


def test_unrealized():
    print("\n[15] PnL tạm tính của lệnh đang mở")
    tmp = tempfile.mkdtemp()
    try:
        cfg = make_cfg(tmp)
        t = pt.PaperTrader(cfg, verbose=False)
        a = t.open_from_signals([cand("AA", 1.0, n=1)])[0]
        b = t.open_from_signals([cand("BB", 1.0, n=2)])[0]
        q = FakeQuotes()
        q.set(a.pool_address, 1.5)           # +50%
        q.set(b.pool_address, 0.9)           # -10%
        t.mark_to_market(q)
        u = t.unrealized()
        check("2 lệnh mở", u["open_count"] == 2)
        # giá vốn $200, giá trị ~ 99.95/eff*1.5 + 99.95/eff*0.9 ≈ 239
        check("PnL tạm tính ~ +20%", 17 < u["pnl_pct"] < 20, f"{u['pnl_pct']:+.2f}%")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    print("=" * 62)
    print("KIỂM THỬ PAPER TRADER (giá giả lập, không gọi mạng)".center(62))
    print("=" * 62)
    for fn in [test_open_and_fees, test_take_profit, test_stop_loss, test_trailing,
               test_timeout, test_rug, test_missing_quote, test_dedup_and_persistence,
               test_max_open, test_bad_input, test_stats_math, test_csv_roundtrip,
               test_disabled, test_exit_priority, test_unrealized]:
        fn()
    print("\n" + "=" * 62)
    print(f"KẾT QUẢ: {len(PASS)} pass · {len(FAIL)} fail")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
