#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard.py — Sinh dashboard.html tự chứa từ dữ liệu paper trading.

VÌ SAO LÀ FILE TĨNH, KHÔNG PHẢI WEB SERVER
    Mở một cổng HTTP trên VPS nghĩa là thêm một thứ để bảo mật, thêm một thứ
    có thể sập, và trên Oracle Cloud còn phải sửa Security List + iptables.
    Một file HTML tĩnh đẩy lên GitHub (Pages) xem được từ mọi nơi, không cần
    SSH, không mở cổng nào cả.

    Đánh đổi: dữ liệu chỉ mới tới lần backup gần nhất (mặc định 1h), và nếu
    repo của bạn PUBLIC thì lịch sử giao dịch mô phỏng cũng public.
    Muốn riêng tư thì để repo private, hoặc chỉ xem file local.

DÙNG
    python dashboard.py                  # sinh dashboard.html
    python dashboard.py -o /tmp/x.html   # chỉ định nơi ghi
"""

from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime, timezone

try:
    import paper_trader as P
except ImportError:
    P = None


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _fmt_usd(x: float) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{sign}${x/1_000:.1f}k"
    return f"{sign}${x:.2f}"


TEMPLATE = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Base Meme Bot — hiệu suất mô phỏng</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
    --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff; --amber:#d29922;
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text);
         font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:1200px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
          gap:12px; margin-bottom:24px; }
  .card { background:var(--panel); border:1px solid var(--border);
          border-radius:8px; padding:14px 16px; }
  .card .label { color:var(--muted); font-size:12px; text-transform:uppercase;
                 letter-spacing:.4px; }
  .card .value { font-size:22px; font-weight:600; margin-top:4px; }
  .pos { color:var(--green); } .neg { color:var(--red); } .neutral { color:var(--text); }
  .panel { background:var(--panel); border:1px solid var(--border);
           border-radius:8px; padding:16px; margin-bottom:20px; }
  .panel h2 { font-size:15px; margin:0 0 12px; font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--muted); font-weight:500; padding:6px 8px;
       border-bottom:1px solid var(--border); font-size:12px; white-space:nowrap; }
  td { padding:6px 8px; border-bottom:1px solid rgba(48,54,61,.5); white-space:nowrap; }
  tr:last-child td { border-bottom:none; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .tag { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
         background:rgba(139,148,158,.15); color:var(--muted); }
  .tag.tp { background:rgba(63,185,80,.15); color:var(--green); }
  .tag.sl { background:rgba(248,81,73,.15); color:var(--red); }
  .tag.tr { background:rgba(88,166,255,.15); color:var(--blue); }
  .tag.to { background:rgba(210,153,34,.15); color:var(--amber); }
  .empty { color:var(--muted); text-align:center; padding:28px; }
  .note { color:var(--muted); font-size:12px; line-height:1.7;
          border-left:2px solid var(--border); padding-left:12px; margin-top:8px; }
  .scroll { overflow-x:auto; }
  canvas { max-height:280px; }
  a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Base Meme Bot — hiệu suất mô phỏng</h1>
  <div class="sub">Cập nhật: __UPDATED__ · Toàn bộ số liệu là <strong>giao dịch mô phỏng</strong>, không phải tiền thật.</div>

  <div class="grid">__CARDS__</div>

  <div class="panel">
    <h2>Đường vốn (equity curve)</h2>
    __EQUITY__
  </div>

  <div class="panel">
    <h2>Vị thế đang mở (__OPEN_N__)</h2>
    <div class="scroll">__OPEN__</div>
  </div>

  <div class="panel">
    <h2>Hiệu suất theo bậc điểm</h2>
    <div class="sub" style="margin:0 0 10px">Điểm cao có thực sự tốt hơn không? Đây là căn cứ để chỉnh <code>min_score_to_alert</code>.</div>
    <div class="scroll">__BYSCORE__</div>
  </div>

  <div class="panel">
    <h2>Lệnh đã đóng (__CLOSED_N__ gần nhất)</h2>
    <div class="scroll">__TRADES__</div>
  </div>

  <div class="panel">
    <h2>Đọc số liệu này thế nào</h2>
    <div class="note">
      Bot chỉ lấy giá theo nhịp (mặc định 45 giây), nên đường đi của giá giữa hai lần
      quan sát là vô hình. Một cú vọt +200% rồi sập −50% trong cùng một khoảng sẽ chỉ
      được ghi là −50%. TP/SL vì thế khớp ở giá quan sát được, không phải đúng giá ngưỡng.<br><br>
      PnL đã trừ slippage ước tính + phí DEX 0.3% + gas, cả hai chiều. Chưa mô phỏng
      được MEV/sandwich, giao dịch fail, và độ sâu pool thật.<br><br>
      Dưới khoảng 30 lệnh đóng thì mọi con số ở trên đều là nhiễu thống kê.
    </div>
  </div>
</div>
<script>__SCRIPT__</script>
</body>
</html>
"""


def _card(label: str, value: str, cls: str = "neutral") -> str:
    return (f'<div class="card"><div class="label">{_esc(label)}</div>'
            f'<div class="value {cls}">{value}</div></div>')


def _sign_cls(x: float) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "neutral"
    return "pos" if x > 0 else ("neg" if x < 0 else "neutral")


REASON_TAG = {
    "take_profit": ("tp", "chốt lời"),
    "stop_loss": ("sl", "cắt lỗ"),
    "trailing_stop": ("tr", "trailing"),
    "timeout": ("to", "hết hạn"),
    "timeout_no_quote": ("to", "hết hạn (mất giá)"),
    "rug_liquidity": ("sl", "rút thanh khoản"),
    "rug_price_zero": ("sl", "giá về 0"),
}


def _tag(reason: str) -> str:
    cls, label = REASON_TAG.get(reason, ("", reason))
    return f'<span class="tag {cls}">{_esc(label)}</span>'


def build(paper_cfg=None, out_path: str | None = None) -> str | None:
    """Sinh dashboard.html. Trả đường dẫn file, hoặc None nếu không làm được."""
    if P is None:
        print("dashboard: thiếu paper_trader.py")
        return None

    cfg = paper_cfg or P.PaperConfig()
    out_path = out_path or "dashboard.html"

    trades = P.load_trades(cfg.trades_csv)
    stats = P.compute_stats(trades, cfg.starting_equity_usd)

    pt = P.PaperTrader(cfg, verbose=False)
    un = pt.unrealized()

    # ------------------------------ thẻ số liệu ------------------------------
    if stats["n"] == 0:
        cards = (_card("Lệnh đã đóng", "0")
                 + _card("Vị thế đang mở", str(un["open_count"]))
                 + _card("Trạng thái", "chờ dữ liệu"))
    else:
        cards = "".join([
            _card("Lệnh đã đóng", str(stats["n"])),
            _card("Win rate", f"{stats['win_rate']:.0f}%",
                  "pos" if stats["win_rate"] >= 50 else "neg"),
            _card("PnL tổng", _fmt_usd(stats["pnl_usd"]), _sign_cls(stats["pnl_usd"])),
            _card("TB mỗi lệnh", f"{stats['avg_pnl_pct']:+.1f}%",
                  _sign_cls(stats["avg_pnl_pct"])),
            _card("Profit factor", stats["profit_factor_str"].split()[0],
                  "pos" if (stats["profit_factor"] or 99) > 1 else "neg"),
            _card("Max drawdown", _fmt_usd(-abs(stats["max_drawdown_usd"])), "neg"),
            _card("Giữ lệnh TB", f"{stats['avg_hold_hours']:.1f}h"),
            _card("Đang mở", f"{un['open_count']}"
                  + (f" ({un['pnl_pct']:+.1f}%)" if un["open_count"] else ""),
                  _sign_cls(un["pnl_pct"]) if un["open_count"] else "neutral"),
        ])

    # ------------------------------ equity curve ------------------------------
    curve = stats["equity_curve"]
    if curve:
        labels = [c["exit_at"][:16].replace("T", " ") for c in curve]
        values = [c["equity"] for c in curve]
        syms = [c["symbol"] for c in curve]
        equity_html = '<canvas id="eq"></canvas>'
        script = f"""
const EQ_LABELS = {json.dumps(labels)};
const EQ_VALUES = {json.dumps(values)};
const EQ_SYMS   = {json.dumps(syms)};
const START     = {json.dumps(stats['starting_equity'])};
const ctx = document.getElementById('eq');
if (ctx) new Chart(ctx, {{
  type:'line',
  data:{{ labels:EQ_LABELS, datasets:[{{
      label:'Vốn ảo (USD)', data:EQ_VALUES,
      borderColor:'#58a6ff', backgroundColor:'rgba(88,166,255,.1)',
      borderWidth:2, pointRadius:2, pointHoverRadius:5, fill:true, tension:.15
  }}]}},
  options:{{
    responsive:true, maintainAspectRatio:false,
    interaction:{{ intersect:false, mode:'index' }},
    plugins:{{
      legend:{{ display:false }},
      tooltip:{{ callbacks:{{ afterLabel: c => 'Lệnh: $' + EQ_SYMS[c.dataIndex] }} }}
    }},
    scales:{{
      x:{{ ticks:{{ color:'#8b949e', maxTicksLimit:8 }}, grid:{{ color:'rgba(48,54,61,.4)' }} }},
      y:{{ ticks:{{ color:'#8b949e' }}, grid:{{ color:'rgba(48,54,61,.4)' }} }}
    }}
  }}
}});
"""
    else:
        equity_html = '<div class="empty">Chưa có lệnh nào đóng.</div>'
        script = ""

    # ------------------------------ vị thế mở ------------------------------
    if pt.positions:
        rows = []
        for p in sorted(pt.positions.values(), key=lambda x: P._parse_iso(x.entry_at)):
            cur = ((p.last_price / p.entry_price - 1) * 100
                   if p.entry_price and p.last_price else 0.0)
            peak = ((p.peak_price / p.entry_price - 1) * 100) if p.entry_price else 0.0
            held = (P._now() - P._parse_iso(p.entry_at)) / 3600.0
            link = (f'<a href="{_esc(p.url)}" target="_blank" rel="noopener">'
                    f'${_esc(p.symbol)}</a>' if p.url else f"${_esc(p.symbol)}")
            rows.append(
                f"<tr><td>{link}</td>"
                f'<td class="num">{p.score:g}</td>'
                f'<td class="num {_sign_cls(cur)}">{cur:+.1f}%</td>'
                f'<td class="num">{peak:+.1f}%</td>'
                f'<td class="num">{held:.1f}h</td>'
                f'<td class="num">{_fmt_usd(p.size_usd)}</td>'
                f'<td class="num">{_fmt_usd(p.last_liquidity)}</td></tr>')
        open_html = (
            "<table><thead><tr><th>Token</th><th class='num'>Điểm</th>"
            "<th class='num'>Hiện tại</th><th class='num'>Đỉnh</th>"
            "<th class='num'>Đã giữ</th><th class='num'>Size</th>"
            "<th class='num'>Thanh khoản</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")
    else:
        open_html = '<div class="empty">Không có vị thế nào đang mở.</div>'

    # ------------------------------ theo bậc điểm ------------------------------
    if stats["by_score"]:
        rows = []
        for k, v in stats["by_score"].items():
            rows.append(
                f"<tr><td>{_esc(k)}</td>"
                f'<td class="num">{v["n"]}</td>'
                f'<td class="num {"pos" if v["win_rate"] >= 50 else "neg"}">{v["win_rate"]:.0f}%</td>'
                f'<td class="num {_sign_cls(v["avg_pnl_pct"])}">{v["avg_pnl_pct"]:+.1f}%</td>'
                f'<td class="num {_sign_cls(v["pnl_usd"])}">{_fmt_usd(v["pnl_usd"])}</td></tr>')
        byscore_html = (
            "<table><thead><tr><th>Bậc điểm</th><th class='num'>Số lệnh</th>"
            "<th class='num'>Win rate</th><th class='num'>TB/lệnh</th>"
            "<th class='num'>PnL</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")
    else:
        byscore_html = '<div class="empty">Chưa đủ dữ liệu.</div>'

    # ------------------------------ lệnh đã đóng ------------------------------
    recent = sorted(trades, key=lambda t: P._parse_iso(t.get("exit_at", "")),
                    reverse=True)[:60]
    if recent:
        rows = []
        for t in recent:
            pnl_pct = P._f(t.get("pnl_pct"))
            link = (f'<a href="{_esc(t.get("url"))}" target="_blank" rel="noopener">'
                    f'${_esc(t.get("symbol"))}</a>' if t.get("url")
                    else f"${_esc(t.get('symbol'))}")
            rows.append(
                f"<tr><td>{_esc(str(t.get('exit_at',''))[:16].replace('T',' '))}</td>"
                f"<td>{link}</td>"
                f'<td class="num">{_esc(t.get("score"))}</td>'
                f"<td>{_tag(t.get('exit_reason',''))}</td>"
                f'<td class="num {_sign_cls(pnl_pct)}">{pnl_pct:+.1f}%</td>'
                f'<td class="num {_sign_cls(t.get("pnl_usd"))}">{_fmt_usd(t.get("pnl_usd"))}</td>'
                f'<td class="num">{P._f(t.get("mfe_pct")):+.0f}%</td>'
                f'<td class="num">{P._f(t.get("mae_pct")):+.0f}%</td>'
                f'<td class="num">{P._f(t.get("hold_hours")):.1f}h</td></tr>')
        trades_html = (
            "<table><thead><tr><th>Đóng lúc</th><th>Token</th><th class='num'>Điểm</th>"
            "<th>Lý do</th><th class='num'>PnL %</th><th class='num'>PnL $</th>"
            "<th class='num'>Đỉnh</th><th class='num'>Đáy</th>"
            "<th class='num'>Giữ</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")
    else:
        trades_html = '<div class="empty">Chưa có lệnh nào đóng.</div>'

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = (TEMPLATE
           .replace("__UPDATED__", _esc(updated))
           .replace("__CARDS__", cards)
           .replace("__EQUITY__", equity_html)
           .replace("__OPEN_N__", str(len(pt.positions)))
           .replace("__OPEN__", open_html)
           .replace("__BYSCORE__", byscore_html)
           .replace("__CLOSED_N__", str(len(recent)))
           .replace("__TRADES__", trades_html)
           .replace("__SCRIPT__", script))

    try:
        with open(out_path, "w", encoding="utf-8") as fp:
            fp.write(out)
        return os.path.abspath(out_path)
    except Exception as e:
        print(f"dashboard: không ghi được {out_path}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser(description="Sinh dashboard.html từ dữ liệu paper trading")
    ap.add_argument("-o", "--out", default="dashboard.html")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    cfg = P.PaperConfig() if P else None
    if cfg and args.csv:
        cfg.trades_csv = args.csv
    path = build(cfg, args.out)
    print(f"đã ghi: {path}" if path else "thất bại")


if __name__ == "__main__":
    main()
