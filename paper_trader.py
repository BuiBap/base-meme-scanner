#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_trader.py — Mô phỏng giao dịch (paper trading) cho base-meme-scanner.

MỤC ĐÍCH
    Trả lời câu hỏi "bộ lọc của bot có kiếm được tiền không?" bằng cách mở một
    vị thế ẢO cho mỗi tín hiệu bot bắn ra, rồi theo dõi tới khi chốt.
    KHÔNG có ví, KHÔNG có private key, KHÔNG giao dịch thật.

LUẬT CHỐT LỆNH (mặc định, sửa trong PaperConfig)
    - Take profit : +100%
    - Stop loss   : -30%
    - Trailing    : sau khi đã lãi >= +40%, nếu tụt 25% từ đỉnh -> chốt
    - Timeout     : giữ tối đa 24h
    - Rug         : thanh khoản < $5k hoặc giá = 0 -> chốt ngay theo giá hiện tại

CHI PHÍ MÔ PHỎNG (tính 2 chiều)
    - Slippage : ước tính theo thanh khoản pool (constant-product AMM)
    - Phí DEX  : 0.3% mỗi chiều
    - Gas      : $0.05 mỗi chiều (Base rất rẻ)
    -> Giá vào hiệu dụng cao hơn giá niêm yết, giá ra thấp hơn. PnL vì thế
       bảo thủ hơn "PnL giấy" thuần biến động giá.

ĐỘ CHÍNH XÁC — ĐỌC KỸ
    Bot chạy theo cron 5 phút (GitHub Actions còn hay trễ 10-30'), nên
    mark-to-market là RỜI RẠC: mỗi khoảng thời gian ta chỉ thấy MỘT mức giá.
    Hệ quả:
      - TP/SL khớp ở GIÁ QUAN SÁT ĐƯỢC, không phải đúng giá ngưỡng. Giá nhảy
        qua ngưỡng thì lệnh được ghi ở mức đó (SL -30% có thể thành -60%).
      - Đường đi trong khoảng là VÔ HÌNH. Một cú vọt lên +200% rồi sập về
        -50% trong cùng 5 phút sẽ chỉ được ghi là -50%.
    Thứ tự ưu tiên khi một mức giá thoả nhiều luật cùng lúc:
        rug -> stop loss -> trailing -> take profit -> timeout
    Kết quả nên đọc như ƯỚC LƯỢNG, không phải backtest chính xác.

FILE STATE (đều commit ngược repo qua workflow)
    paper_positions.json : vị thế đang mở
    paper_trades.csv     : lịch sử lệnh đã đóng (1 dòng = 1 lệnh)
    paper_state.json     : mốc thời gian báo cáo cuối

DÙNG ĐỘC LẬP
    python paper_trader.py --report          # in báo cáo hiệu suất
    python paper_trader.py --report --csv X  # đọc file CSV khác
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional


# =========================================================================== #
#  Cấu hình
# =========================================================================== #

@dataclass
class PaperConfig:
    enabled: bool = True

    # ---------- sizing ----------
    position_size_usd: float = 100.0
    starting_equity_usd: float = 1_000.0   # chỉ để vẽ đường equity, không chặn lệnh
    max_open_positions: int = 40
    one_position_per_token: bool = True

    # ---------- luật chốt ----------
    take_profit_pct: float = 100.0         # +100% -> chốt
    stop_loss_pct: float = -30.0           # -30%  -> chốt
    trailing_activate_pct: float = 40.0    # chỉ bật trailing sau khi lãi >= mức này
    trailing_giveback_pct: float = 25.0    # tụt bao nhiêu % từ đỉnh thì chốt
    max_hold_hours: float = 24.0
    rug_liquidity_usd: float = 5_000.0     # liq tụt dưới mức này -> coi như rug, chốt

    # ---------- chi phí ----------
    dex_fee_pct: float = 0.3               # mỗi chiều
    gas_usd: float = 0.05                  # mỗi chiều
    slippage_model: bool = True            # ước tính slippage theo thanh khoản

    # ---------- file ----------
    positions_file: str = "paper_positions.json"
    trades_csv: str = "paper_trades.csv"
    state_file: str = "paper_state.json"

    # ---------- báo cáo ----------
    telegram_on_close: bool = True         # báo mỗi lệnh đóng
    telegram_daily_report: bool = True
    report_interval_hours: float = 24.0


TRADE_FIELDS = [
    "trade_id", "symbol", "token_address", "pool_address", "score",
    "entry_at", "entry_price", "entry_price_eff", "size_usd", "tokens",
    "liquidity_at_entry", "market_cap_at_entry", "age_hours_at_entry",
    "exit_at", "exit_price", "exit_price_eff", "exit_reason",
    "gross_usd", "fees_usd", "pnl_usd", "pnl_pct", "hold_hours",
    "peak_price", "trough_price", "mfe_pct", "mae_pct", "checks",
    "reasons_at_entry", "url",
]


# =========================================================================== #
#  Tiện ích
# =========================================================================== #

def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  tz=timezone.utc).isoformat()


def _parse_iso(s: str) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return _now()


def _f(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if v == v else default          # loại NaN
    except (TypeError, ValueError):
        return default


def _fmt_usd(x: float) -> str:
    x = _f(x)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{sign}${x/1_000:.1f}k"
    return f"{sign}${x:.2f}"


def estimate_slippage_pct(liquidity_usd: float, trade_usd: float) -> float:
    """Trượt giá 1 chiều theo constant-product AMM (thô).
    Giả định ~1/2 thanh khoản ở phía quote: impact ≈ trade / (quote + trade)."""
    quote_reserve = max(_f(liquidity_usd) / 2.0, 1.0)
    return (trade_usd / (quote_reserve + trade_usd)) * 100.0


# =========================================================================== #
#  Vị thế
# =========================================================================== #

@dataclass
class Position:
    trade_id: str
    symbol: str
    token_address: str
    pool_address: str
    score: float
    entry_at: str
    entry_price: float          # giá niêm yết lúc vào
    entry_price_eff: float      # giá vào sau slippage + phí
    size_usd: float
    tokens: float
    liquidity_at_entry: float
    market_cap_at_entry: float = 0.0
    age_hours_at_entry: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0
    last_price: float = 0.0
    last_liquidity: float = 0.0
    last_seen_at: str = ""
    checks: int = 0             # số lần mark-to-market
    entry_fees_usd: float = 0.0
    reasons_at_entry: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "Position":
        known = {k: d.get(k) for k in Position.__dataclass_fields__ if k in d}
        return Position(**known)


# =========================================================================== #
#  Engine
# =========================================================================== #

class PaperTrader:
    """Mô phỏng vào/ra lệnh. Không tự gọi mạng — nhận sẵn hàm lấy giá."""

    def __init__(self, cfg: PaperConfig, notifier=None, verbose: bool = True):
        self.cfg = cfg
        self.tg = notifier                     # đối tượng có .send(text) -> bool
        self.verbose = verbose
        self.positions: dict[str, Position] = self._load_positions()
        self.state: dict = self._load_state()

    # ---------------------- state I/O ---------------------- #

    def _load_positions(self) -> dict[str, Position]:
        path = self.cfg.positions_file
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fp:
                raw = json.load(fp)
        except Exception:
            return {}
        out = {}
        for d in (raw if isinstance(raw, list) else raw.get("positions", [])):
            try:
                p = Position.from_dict(d)
                out[p.trade_id] = p
            except Exception:
                continue
        return out

    def _save_positions(self):
        try:
            payload = {
                "updated_at": _iso(),
                "open_count": len(self.positions),
                "positions": [p.to_dict() for p in self.positions.values()],
            }
            with open(self.cfg.positions_file, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=1)
        except Exception as e:
            self._log(f"  [paper] không ghi được positions: {e}")

    def _load_state(self) -> dict:
        if not os.path.exists(self.cfg.state_file):
            return {"last_report": 0.0}
        try:
            with open(self.cfg.state_file, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return {"last_report": 0.0}

    def _save_state(self):
        try:
            with open(self.cfg.state_file, "w", encoding="utf-8") as fp:
                json.dump(self.state, fp, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    # ---------------------- mở lệnh ---------------------- #

    def open_from_signals(self, candidates: list) -> list[Position]:
        """Mở vị thế ảo cho các tín hiệu vừa được bot báo.
        `candidates` là list Candidate của base_meme_bot (duck-typed)."""
        if not self.cfg.enabled:
            return []
        opened = []
        held_tokens = {p.token_address.lower() for p in self.positions.values()}

        for c in candidates:
            if len(self.positions) >= self.cfg.max_open_positions:
                self._log(f"  [paper] đã đủ {self.cfg.max_open_positions} vị thế mở, bỏ qua phần còn lại")
                break
            token = (getattr(c, "token_address", "") or "").lower()
            price = _f(getattr(c, "price_usd", 0))
            liq = _f(getattr(c, "liquidity_usd", 0))
            if not token or price <= 0:
                continue
            if self.cfg.one_position_per_token and token in held_tokens:
                continue

            size = self.position_size_for(getattr(c, "score", 0))
            slip = estimate_slippage_pct(liq, size) if self.cfg.slippage_model else 0.0
            # giá vào hiệu dụng: trượt giá + phí DEX đẩy giá mua lên
            entry_eff = price * (1 + slip / 100.0) * (1 + self.cfg.dex_fee_pct / 100.0)
            usable = max(size - self.cfg.gas_usd, 0.0)
            if entry_eff <= 0 or usable <= 0:
                continue
            tokens = usable / entry_eff

            p = Position(
                trade_id=uuid.uuid4().hex[:12],
                symbol=str(getattr(c, "symbol", "?")),
                token_address=token,
                pool_address=(getattr(c, "pool_address", "") or "").lower(),
                score=_f(getattr(c, "score", 0)),
                entry_at=_iso(),
                entry_price=price,
                entry_price_eff=entry_eff,
                size_usd=size,
                tokens=tokens,
                liquidity_at_entry=liq,
                market_cap_at_entry=_f(getattr(c, "market_cap", 0)),
                age_hours_at_entry=_f(getattr(c, "age_hours", 0)),
                peak_price=price,
                trough_price=price,
                last_price=price,
                last_liquidity=liq,
                last_seen_at=_iso(),
                entry_fees_usd=(size - usable) + (size * self.cfg.dex_fee_pct / 100.0),
                reasons_at_entry=" | ".join(getattr(c, "reasons", []) or []),
                url=str(getattr(c, "url", "")),
            )
            self.positions[p.trade_id] = p
            held_tokens.add(token)
            opened.append(p)
            self._log(f"  [paper] MỞ ${p.symbol} @ {p.entry_price:.10g} "
                      f"(eff {p.entry_price_eff:.10g}, slip {slip:.2f}%, size {_fmt_usd(size)})")

        if opened:
            self._save_positions()
        return opened

    def position_size_for(self, score: float) -> float:
        """Sizing cố định. Muốn size theo điểm thì sửa hàm này."""
        return self.cfg.position_size_usd

    # ---------------------- mark-to-market ---------------------- #

    def mark_to_market(self, price_fetcher: Callable[[list], dict]) -> list[dict]:
        """Cập nhật mọi vị thế mở, chốt cái nào chạm luật.

        price_fetcher(pool_addresses) -> {pool_address_lower: {"price": float, "liquidity": float}}
        Pool không trả về dữ liệu -> bỏ qua lần này (không chốt bừa).
        """
        if not self.cfg.enabled or not self.positions:
            return []

        pools = sorted({p.pool_address for p in self.positions.values() if p.pool_address})
        quotes = {}
        if pools:
            try:
                quotes = price_fetcher(pools) or {}
            except Exception as e:
                self._log(f"  [paper] lỗi lấy giá: {e} -> bỏ qua lần mark-to-market này")
                return []

        closed = []
        for tid in list(self.positions.keys()):
            p = self.positions[tid]
            q = quotes.get(p.pool_address)
            hold_h = (_now() - _parse_iso(p.entry_at)) / 3600.0

            if q is None:
                # Không có quote: chỉ xử lý timeout (dựa trên giá cuối biết được)
                if hold_h >= self.cfg.max_hold_hours and p.last_price > 0:
                    closed.append(self._close(p, p.last_price, p.last_liquidity,
                                              "timeout_no_quote"))
                continue

            price = _f(q.get("price"))
            liq = _f(q.get("liquidity"))
            p.checks += 1
            p.last_seen_at = _iso()
            p.last_liquidity = liq
            if price > 0:
                p.last_price = price
                p.peak_price = max(p.peak_price or price, price)
                p.trough_price = min(p.trough_price or price, price) if p.trough_price else price

            reason = self._exit_reason(p, price, liq, hold_h)
            if reason:
                closed.append(self._close(p, price, liq, reason))

        self._save_positions()

        if closed and self.cfg.telegram_on_close and self.tg is not None:
            for t in closed:
                self._notify_close(t)
        return closed

    def _exit_reason(self, p: Position, price: float, liq: float,
                     hold_h: float) -> Optional[str]:
        """Thứ tự kiểm tra có chủ đích: rug -> SL -> trailing -> TP -> timeout.

        Chỉ có MỘT mức giá cho mỗi lần quan sát, nên thứ tự này chỉ quyết định
        khi cùng mức giá đó thoả nhiều luật:
          - SL trước trailing : mô tả đúng rằng lệnh đang lỗ nặng, không phải
                                "chốt lời trailing".
          - trailing trước TP : nếu giá đã rời đỉnh đủ xa thì lý do thoát thực
                                sự là trailing, dù vẫn còn trên ngưỡng TP.
          - rug trên tất cả   : pool cạn thì lãi danh nghĩa vô nghĩa."""
        if price <= 0:
            return "rug_price_zero"
        if liq > 0 and liq < self.cfg.rug_liquidity_usd:
            return "rug_liquidity"

        pnl_pct = (price / p.entry_price - 1) * 100.0
        if pnl_pct <= self.cfg.stop_loss_pct:
            return "stop_loss"

        peak_pct = (p.peak_price / p.entry_price - 1) * 100.0
        if peak_pct >= self.cfg.trailing_activate_pct and p.peak_price > 0:
            giveback = (1 - price / p.peak_price) * 100.0
            if giveback >= self.cfg.trailing_giveback_pct:
                return "trailing_stop"

        if pnl_pct >= self.cfg.take_profit_pct:
            return "take_profit"
        if hold_h >= self.cfg.max_hold_hours:
            return "timeout"
        return None

    def _close(self, p: Position, price: float, liq: float, reason: str) -> dict:
        """Tính PnL sau phí và ghi 1 dòng vào CSV."""
        exit_slip = (estimate_slippage_pct(liq or p.liquidity_at_entry,
                                          p.tokens * price)
                     if self.cfg.slippage_model else 0.0)
        exit_eff = price * (1 - exit_slip / 100.0) * (1 - self.cfg.dex_fee_pct / 100.0)
        exit_eff = max(exit_eff, 0.0)
        gross = p.tokens * exit_eff
        proceeds = max(gross - self.cfg.gas_usd, 0.0)
        pnl_usd = proceeds - p.size_usd
        pnl_pct = (pnl_usd / p.size_usd * 100.0) if p.size_usd else 0.0
        hold_h = (_now() - _parse_iso(p.entry_at)) / 3600.0

        exit_fees = (p.tokens * price - gross) + self.cfg.gas_usd

        row = {
            "trade_id": p.trade_id,
            "symbol": p.symbol,
            "token_address": p.token_address,
            "pool_address": p.pool_address,
            "score": p.score,
            "entry_at": p.entry_at,
            "entry_price": p.entry_price,
            "entry_price_eff": p.entry_price_eff,
            "size_usd": round(p.size_usd, 4),
            "tokens": p.tokens,
            "liquidity_at_entry": round(p.liquidity_at_entry, 2),
            "market_cap_at_entry": round(p.market_cap_at_entry, 2),
            "age_hours_at_entry": round(p.age_hours_at_entry, 2),
            "exit_at": _iso(),
            "exit_price": price,
            "exit_price_eff": exit_eff,
            "exit_reason": reason,
            "gross_usd": round(gross, 4),
            "fees_usd": round(p.entry_fees_usd + exit_fees, 4),
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 2),
            "hold_hours": round(hold_h, 3),
            "peak_price": p.peak_price,
            "trough_price": p.trough_price,
            "mfe_pct": round((p.peak_price / p.entry_price - 1) * 100.0, 2) if p.entry_price else 0.0,
            "mae_pct": round((p.trough_price / p.entry_price - 1) * 100.0, 2) if p.entry_price and p.trough_price else 0.0,
            "checks": p.checks,
            "reasons_at_entry": p.reasons_at_entry,
            "url": p.url,
        }
        self._append_trade(row)
        self.positions.pop(p.trade_id, None)
        self._log(f"  [paper] ĐÓNG ${p.symbol} {reason} · "
                  f"{pnl_pct:+.1f}% ({_fmt_usd(pnl_usd)}) sau {hold_h:.1f}h")
        return row

    def _append_trade(self, row: dict):
        path = self.cfg.trades_csv
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        try:
            with open(path, "a", newline="", encoding="utf-8") as fp:
                w = csv.DictWriter(fp, fieldnames=TRADE_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception as e:
            self._log(f"  [paper] không ghi được CSV: {e}")

    # ---------------------- báo cáo ---------------------- #

    def unrealized(self) -> dict:
        """PnL tạm tính của các vị thế đang mở (theo giá quan sát gần nhất)."""
        total_cost = total_now = 0.0
        for p in self.positions.values():
            if p.last_price <= 0:
                continue
            total_cost += p.size_usd
            total_now += p.tokens * p.last_price
        return {
            "open_count": len(self.positions),
            "cost_usd": round(total_cost, 2),
            "value_usd": round(total_now, 2),
            "pnl_usd": round(total_now - total_cost, 2),
            "pnl_pct": round((total_now / total_cost - 1) * 100.0, 2) if total_cost else 0.0,
        }

    def load_trades(self) -> list[dict]:
        return load_trades(self.cfg.trades_csv)

    def maybe_daily_report(self) -> Optional[str]:
        """Gửi tổng kết Telegram nếu đã qua report_interval_hours."""
        if not (self.cfg.enabled and self.cfg.telegram_daily_report and self.tg):
            return None
        last = _f(self.state.get("last_report", 0))
        if (_now() - last) / 3600.0 < self.cfg.report_interval_hours:
            return None
        trades = self.load_trades()
        if not trades:
            return None
        text = self.telegram_report_text(trades)
        if self.tg.send(text):
            self.state["last_report"] = _now()
            self._save_state()
        return text

    def telegram_report_text(self, trades: Optional[list] = None) -> str:
        trades = trades if trades is not None else self.load_trades()
        s = compute_stats(trades, self.cfg.starting_equity_usd)
        u = self.unrealized()

        def esc(x):
            return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = [
            "📊 <b>Hiệu suất mô phỏng (paper)</b>",
            f"Lệnh đã đóng: <b>{s['n']}</b> · win rate <b>{s['win_rate']:.0f}%</b>",
            f"PnL tổng: <b>{_fmt_usd(s['pnl_usd'])}</b> ({s['roi_pct']:+.1f}% vốn triển khai)",
            f"TB/lệnh: {s['avg_pnl_pct']:+.1f}% · median {s['median_pnl_pct']:+.1f}%",
            f"Expectancy: {_fmt_usd(s['expectancy_usd'])}/lệnh · "
            f"profit factor {s['profit_factor_str']}",
            f"Max drawdown: {_fmt_usd(-abs(s['max_drawdown_usd']))} · "
            f"giữ TB {s['avg_hold_hours']:.1f}h",
        ]
        if s["best"]:
            lines.append(f"🏆 tốt nhất ${esc(s['best']['symbol'])} {_f(s['best']['pnl_pct']):+.0f}%")
        if s["worst"]:
            lines.append(f"🩸 tệ nhất ${esc(s['worst']['symbol'])} {_f(s['worst']['pnl_pct']):+.0f}%")
        if s["by_reason"]:
            parts = [f"{k} {v['n']}({v['avg_pnl_pct']:+.0f}%)"
                     for k, v in s["by_reason"].items()]
            lines.append("Lý do chốt: " + " · ".join(parts))
        if s["by_score"]:
            parts = [f"{k}: {v['n']}L {v['avg_pnl_pct']:+.0f}%"
                     for k, v in s["by_score"].items()]
            lines.append("Theo điểm: " + " · ".join(parts))
        if u["open_count"]:
            lines.append(f"⏳ đang mở {u['open_count']} lệnh · tạm tính {u['pnl_pct']:+.1f}% "
                         f"({_fmt_usd(u['pnl_usd'])})")
        return "\n".join(lines)

    def _notify_close(self, t: dict):
        icon = "🟢" if _f(t["pnl_usd"]) > 0 else "🔴"
        reason_vi = {
            "take_profit": "chốt lời",
            "stop_loss": "cắt lỗ",
            "trailing_stop": "trailing stop",
            "timeout": "hết hạn giữ",
            "timeout_no_quote": "hết hạn (mất dữ liệu giá)",
            "rug_liquidity": "rút thanh khoản",
            "rug_price_zero": "giá về 0",
        }.get(t["exit_reason"], t["exit_reason"])
        sym = str(t["symbol"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = (
            f"{icon} <b>${sym}</b> {reason_vi} · <b>{_f(t['pnl_pct']):+.1f}%</b> "
            f"({_fmt_usd(t['pnl_usd'])})\n"
            f"vào {_f(t['entry_price']):.8g} → ra {_f(t['exit_price']):.8g} · "
            f"giữ {_f(t['hold_hours']):.1f}h · điểm {t['score']}\n"
            f"đỉnh {_f(t['mfe_pct']):+.0f}% · đáy {_f(t['mae_pct']):+.0f}% · "
            f"phí {_fmt_usd(t['fees_usd'])}"
        )
        try:
            self.tg.send(text)
        except Exception:
            pass


# =========================================================================== #
#  Thống kê
# =========================================================================== #

def load_trades(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fp:
            return [r for r in csv.DictReader(fp) if r.get("trade_id")]
    except Exception:
        return []


def _score_bucket(score: float) -> str:
    if score >= 85:
        return "85+"
    if score >= 75:
        return "75-84"
    if score >= 65:
        return "65-74"
    return "<65"


def compute_stats(trades: list[dict], starting_equity: float = 1_000.0) -> dict:
    """Thống kê hiệu suất từ danh sách lệnh đã đóng."""
    empty = {
        "n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
        "pnl_usd": 0.0, "deployed_usd": 0.0, "roi_pct": 0.0,
        "avg_pnl_pct": 0.0, "median_pnl_pct": 0.0, "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0, "expectancy_usd": 0.0, "expectancy_pct": 0.0,
        "profit_factor": None, "profit_factor_str": "—",
        "max_drawdown_usd": 0.0, "max_drawdown_pct": 0.0,
        "avg_hold_hours": 0.0, "total_fees_usd": 0.0,
        "avg_mfe_pct": 0.0, "avg_mae_pct": 0.0,
        "best": None, "worst": None, "by_reason": {}, "by_score": {},
        "equity_curve": [], "starting_equity": starting_equity,
    }
    if not trades:
        return empty

    trades = sorted(trades, key=lambda t: _parse_iso(t.get("exit_at", "")))
    pnl_usd = [_f(t.get("pnl_usd")) for t in trades]
    pnl_pct = [_f(t.get("pnl_pct")) for t in trades]
    sizes = [_f(t.get("size_usd")) for t in trades]

    wins = [x for x in pnl_usd if x > 0]
    losses = [x for x in pnl_usd if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    # equity curve + max drawdown
    equity, eq = [], starting_equity
    peak, max_dd = eq, 0.0
    for t, x in zip(trades, pnl_usd):
        eq += x
        equity.append({"exit_at": t.get("exit_at", ""), "equity": round(eq, 2),
                       "symbol": t.get("symbol", ""), "pnl_usd": round(x, 2)})
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    def group(key_fn) -> dict:
        buckets: dict[str, list[dict]] = {}
        for t in trades:
            buckets.setdefault(key_fn(t), []).append(t)
        out = {}
        for k in sorted(buckets):
            g = buckets[k]
            gp = [_f(x.get("pnl_pct")) for x in g]
            gu = [_f(x.get("pnl_usd")) for x in g]
            out[k] = {
                "n": len(g),
                "win_rate": round(sum(1 for x in gu if x > 0) / len(g) * 100, 1),
                "avg_pnl_pct": round(sum(gp) / len(gp), 2),
                "pnl_usd": round(sum(gu), 2),
            }
        return out

    best = max(trades, key=lambda t: _f(t.get("pnl_pct")))
    worst = min(trades, key=lambda t: _f(t.get("pnl_pct")))
    deployed = sum(sizes)
    pf = (gross_win / gross_loss) if gross_loss > 0 else None

    return {
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "pnl_usd": round(sum(pnl_usd), 2),
        "deployed_usd": round(deployed, 2),
        "roi_pct": round(sum(pnl_usd) / deployed * 100, 2) if deployed else 0.0,
        "avg_pnl_pct": round(sum(pnl_pct) / len(pnl_pct), 2),
        "median_pnl_pct": round(statistics.median(pnl_pct), 2),
        "avg_win_pct": round(sum(p for p in pnl_pct if p > 0) / max(len(wins), 1), 2),
        "avg_loss_pct": round(sum(p for p in pnl_pct if p <= 0) / max(len(losses), 1), 2),
        "expectancy_usd": round(sum(pnl_usd) / len(trades), 2),
        "expectancy_pct": round(sum(pnl_pct) / len(pnl_pct), 2),
        "profit_factor": round(pf, 2) if pf is not None else None,
        "profit_factor_str": f"{pf:.2f}" if pf is not None else "∞ (chưa có lệnh lỗ)",
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / starting_equity * 100, 2) if starting_equity else 0.0,
        "avg_hold_hours": round(sum(_f(t.get("hold_hours")) for t in trades) / len(trades), 2),
        "total_fees_usd": round(sum(_f(t.get("fees_usd")) for t in trades), 2),
        "avg_mfe_pct": round(sum(_f(t.get("mfe_pct")) for t in trades) / len(trades), 2),
        "avg_mae_pct": round(sum(_f(t.get("mae_pct")) for t in trades) / len(trades), 2),
        "best": best,
        "worst": worst,
        "by_reason": group(lambda t: t.get("exit_reason", "?")),
        "by_score": group(lambda t: _score_bucket(_f(t.get("score")))),
        "equity_curve": equity,
        "starting_equity": starting_equity,
    }


def print_report(trades: list[dict], cfg: Optional[PaperConfig] = None,
                 positions_file: Optional[str] = None):
    cfg = cfg or PaperConfig()
    s = compute_stats(trades, cfg.starting_equity_usd)
    W = 62
    print("=" * W)
    print("BÁO CÁO HIỆU SUẤT MÔ PHỎNG (PAPER TRADING)".center(W))
    print("=" * W)
    if s["n"] == 0:
        print("Chưa có lệnh nào đóng. Bot cần chạy vài ngày để có dữ liệu.")
    else:
        print(f"  Số lệnh đã đóng     : {s['n']}  (thắng {s['wins']} / lỗ {s['losses']})")
        print(f"  Win rate            : {s['win_rate']:.1f}%")
        print(f"  PnL tổng            : {_fmt_usd(s['pnl_usd'])}  "
              f"trên {_fmt_usd(s['deployed_usd'])} vốn triển khai ({s['roi_pct']:+.2f}%)")
        print(f"  TB / lệnh           : {s['avg_pnl_pct']:+.2f}%   "
              f"(median {s['median_pnl_pct']:+.2f}%)")
        print(f"  Lệnh thắng TB       : {s['avg_win_pct']:+.2f}%")
        print(f"  Lệnh lỗ TB          : {s['avg_loss_pct']:+.2f}%")
        print(f"  Expectancy          : {_fmt_usd(s['expectancy_usd'])} / lệnh")
        print(f"  Profit factor       : {s['profit_factor_str']}")
        print(f"  Max drawdown        : {_fmt_usd(s['max_drawdown_usd'])} "
              f"({s['max_drawdown_pct']:.1f}% vốn ảo ban đầu)")
        print(f"  Giữ lệnh TB         : {s['avg_hold_hours']:.2f}h")
        print(f"  Phí đã trả (mô phỏng): {_fmt_usd(s['total_fees_usd'])}")
        print(f"  MFE TB (đỉnh)       : {s['avg_mfe_pct']:+.1f}%   "
              f"MAE TB (đáy): {s['avg_mae_pct']:+.1f}%")
        print("-" * W)
        print("  THEO LÝ DO CHỐT")
        for k, v in s["by_reason"].items():
            print(f"    {k:<20} n={v['n']:<4} win {v['win_rate']:>5.1f}%  "
                  f"TB {v['avg_pnl_pct']:+7.2f}%  PnL {_fmt_usd(v['pnl_usd'])}")
        print("-" * W)
        print("  THEO BẬC ĐIỂM  (điểm cao có thực sự tốt hơn không?)")
        for k, v in s["by_score"].items():
            print(f"    {k:<20} n={v['n']:<4} win {v['win_rate']:>5.1f}%  "
                  f"TB {v['avg_pnl_pct']:+7.2f}%  PnL {_fmt_usd(v['pnl_usd'])}")
        print("-" * W)
        b, w = s["best"], s["worst"]
        print(f"  Tốt nhất : ${b['symbol']:<10} {_f(b['pnl_pct']):+8.1f}%  "
              f"({b['exit_reason']}, điểm {b['score']})")
        print(f"  Tệ nhất  : ${w['symbol']:<10} {_f(w['pnl_pct']):+8.1f}%  "
              f"({w['exit_reason']}, điểm {w['score']})")

    pf = positions_file or cfg.positions_file
    if os.path.exists(pf):
        pt = PaperTrader(cfg, verbose=False)
        u = pt.unrealized()
        if u["open_count"]:
            print("-" * W)
            print(f"  Đang mở: {u['open_count']} lệnh · giá vốn {_fmt_usd(u['cost_usd'])} "
                  f"· giá trị {_fmt_usd(u['value_usd'])} "
                  f"({u['pnl_pct']:+.2f}%, {_fmt_usd(u['pnl_usd'])})")
            for p in sorted(pt.positions.values(),
                            key=lambda x: _parse_iso(x.entry_at)):
                cur = (p.last_price / p.entry_price - 1) * 100 if p.entry_price and p.last_price else 0.0
                held = (_now() - _parse_iso(p.entry_at)) / 3600.0
                print(f"    ${p.symbol:<10} {cur:+7.1f}%  giữ {held:5.1f}h  "
                      f"điểm {p.score:<5} đỉnh "
                      f"{((p.peak_price/p.entry_price-1)*100 if p.entry_price else 0):+.0f}%")
    print("=" * W)
    print("Lưu ý: mark-to-market rời rạc theo nhịp cron (~5 phút, Actions hay trễ),")
    print("nên TP/SL khớp ở giá quan sát được, không phải đúng giá ngưỡng.")
    print("=" * W)


def main():
    ap = argparse.ArgumentParser(description="Báo cáo paper trading")
    ap.add_argument("--report", action="store_true", help="in báo cáo hiệu suất")
    ap.add_argument("--csv", default=None, help="đường dẫn paper_trades.csv")
    ap.add_argument("--positions", default=None, help="đường dẫn paper_positions.json")
    args = ap.parse_args()

    cfg = PaperConfig()
    if args.csv:
        cfg.trades_csv = args.csv
    if args.positions:
        cfg.positions_file = args.positions
    print_report(load_trades(cfg.trades_csv), cfg)


if __name__ == "__main__":
    main()
