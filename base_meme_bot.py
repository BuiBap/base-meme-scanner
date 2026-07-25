#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
base_meme_bot.py — Bot lọc memecoin trên BASE theo bộ tiêu chí K.O + dòng tiền định lượng.

BACKBONE dữ liệu (tất cả miễn phí):
  - GeckoTerminal  : khám phá pool mới/trending + core metrics + buyers/sellers (maker ratio) + trades (net buy vol)
  - GoPlus (8453)  : buy/sell tax, honeypot, cannot-sell, top-holder concentration

KHÔNG lấy được free (đã để HOOK, KHÔNG bịa số):
  - Fresh Wallet Ratio (>35% ví <24h)   -> cần enumerate holder + tuổi ví (Basescan, rất nặng)
  - Smart Money chất lượng (WinRate/PnL) -> GMGN/Arkham gated/trả phí
  - Social (TweetScout X-Score / Kaito)  -> API trả phí

Luồng (funnel để tiết kiệm rate-limit 30 req/phút của GeckoTerminal):
  1) Discovery + core metrics  : new_pools + trending_pools (base)   [rẻ, gộp sẵn metrics]
  2) K.O tầng 1 (không tốn call): LP/MC, liquidity, age, maker ratio, buy/sell count, slippage est.
  3) K.O tầng 2 (GoPlus 1 call) : tax, honeypot, holder concentration
  4) Xác nhận net-buy-volume    : trades endpoint (chỉ cho shortlist)
  5) Chấm điểm + báo + ghi CSV

Chạy:
    pip install requests
    python base_meme_bot.py                 # quét 1 lần
    python base_meme_bot.py --loop          # quét liên tục
    python base_meme_bot.py --min-score 60 --loop
"""

import argparse, csv, json, os, time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests

# =========================================================================== #
#  CẤU HÌNH — chỉnh đúng ngưỡng của anh ở đây
# =========================================================================== #

@dataclass
class Config:
    network: str = "base"          # GeckoTerminal slug
    goplus_chain_id: str = "8453"  # Base

    # ---------- TẦNG K.O (trượt bất kỳ điều nào -> loại ngay) ----------
    # LP / Market Cap ratio: <8% (dưới 1M MC) hoặc <5% (trên 1M MC)
    lp_mc_min_under_1m: float = 0.08
    lp_mc_min_over_1m: float = 0.05
    mc_threshold: float = 1_000_000

    # Buy/Sell tax (Base): >3% -> loại
    max_tax_pct: float = 3.0

    # Slippage: lệnh $1000 gây trượt > 3% -> loại (ước tính AMM)
    slippage_trade_size_usd: float = 1_000
    max_slippage_pct: float = 3.0

    # Insider/Cluster: top-20 ví (đã trừ LP/locked/burn) > 12% -> loại (PROXY qua GoPlus)
    max_top20_holder_pct: float = 12.0

    # Fresh Wallet Ratio > 35% -> loại  (HOOK — mặc định TẮT vì không có data free)
    enforce_fresh_wallet_ko: bool = False
    max_fresh_wallet_pct: float = 35.0

    # ---------- ngưỡng lọc cơ bản để thu hẹp danh sách ----------
    min_liquidity_usd: float = 40_000
    min_volume_24h_usd: float = 80_000
    min_age_hours: float = 1.0
    max_age_hours: float = 120.0
    min_txns_24h: int = 250

    # ---------- CHỈ BÁO DÒNG TIỀN ĐỊNH LƯỢNG ----------
    # A. Txs/Maker ratio
    wash_ratio_alert: float = 15.0      # > 15 -> nghi wash trading (K.O)
    healthy_ratio_lo: float = 2.5       # vùng tự nhiên [2.5, 6.0]
    healthy_ratio_hi: float = 6.0

    # B. Net Buy Volume
    buy_sell_vol_ratio_min: float = 1.4     # buy vol / sell vol (5m/15m)
    net_buy_vs_liq_min_pct: float = 15.0    # 1h: net buy vol >= +15% liquidity

    # ---------- điểm & vận hành ----------
    min_score_to_alert: float = 55.0
    discovery_pages: int = 2              # số trang new_pools (mỗi trang ~20 pool)
    interval_seconds: int = 300
    request_timeout: int = 15
    max_retries: int = 5
    gt_min_interval: float = 2.1          # giãn cách call GeckoTerminal (30/phút -> ~2s)
    output_csv: str = "base_signals.csv"
    seen_file: str = "base_seen.json"

    # ---------- keys optional cho các HOOK trả phí ----------
    basescan_api_key: str = os.getenv("BASESCAN_API_KEY", "")
    tweetscout_api_key: str = os.getenv("TWEETSCOUT_API_KEY", "")
    smart_money_api_key: str = os.getenv("SMART_MONEY_API_KEY", "")

    # ---------- Telegram ----------
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_send_summary: bool = True   # gửi 1 dòng tổng kết mỗi lần quét (kể cả 0 hit)
    max_alerts_per_run: int = int(os.getenv("MAX_ALERTS_PER_RUN", "8"))  # chống flood/lần


# =========================================================================== #
#  Telegram notifier
# =========================================================================== #

def _esc(s) -> str:
    """Escape ký tự đặc biệt cho parse_mode=HTML của Telegram."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _fmt_usd(x: float) -> str:
    x = float(x or 0)
    if x >= 1_000_000: return f"${x/1_000_000:.2f}M"
    if x >= 1_000:     return f"${x/1_000:.0f}k"
    return f"${x:.0f}"

class TelegramNotifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.telegram_bot_token and cfg.telegram_chat_id)
        self.s = requests.Session()
        if not self.enabled:
            print("  [telegram] chưa cấu hình TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID -> tắt gửi.")

    def _api(self, method: str, payload: dict) -> Optional[dict]:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/{method}"
        for attempt in range(1, 4):
            try:
                r = self.s.post(url, json=payload, timeout=self.cfg.request_timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    retry = 2
                    try:
                        retry = int(r.json().get("parameters", {}).get("retry_after", 2))
                    except Exception:
                        pass
                    print(f"    [telegram 429] chờ {retry}s")
                    time.sleep(retry); continue
                # 400/403: token/chat_id sai hoặc bot chưa được /start
                try:
                    print(f"    [telegram {r.status_code}] {r.json().get('description','')}")
                except Exception:
                    pass
                return None
            except requests.RequestException:
                time.sleep(2)
        return None

    def send(self, text: str, buttons: Optional[list] = None) -> bool:
        if not self.enabled:
            return False
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        res = self._api("sendMessage", payload)
        # Telegram: tối đa ~1 msg/giây cho mỗi chat -> giãn nhẹ
        time.sleep(1.1)
        return bool(res and res.get("ok"))

    def send_alert(self, c: "Candidate") -> bool:
        goplus_url = f"https://gopluslabs.io/token-security/{self.cfg.goplus_chain_id}/{c.token_address}"
        # số liệu an toàn khi None
        bv5 = f"{c.buy_sell_vol_ratio_5m:.2f}" if c.buy_sell_vol_ratio_5m else "—"
        bv15 = f"{c.buy_sell_vol_ratio_15m:.2f}" if c.buy_sell_vol_ratio_15m else "—"
        nb = f"{c.net_buy_vs_liq_1h_pct:+.0f}%" if c.net_buy_vs_liq_1h_pct is not None else "—"
        tax = f"{(c.buy_tax or 0):.0f}/{(c.sell_tax or 0):.0f}%"
        top20 = f"{c.top20_holder_pct:.0f}%" if c.top20_holder_pct is not None else "—"

        lines = [
            f"⭐ <b>${_esc(c.symbol)}</b>  ·  điểm <b>{c.score}</b>",
            f"<i>{_esc(c.name)}</i> · Base",
            "",
            f"💰 MC {_fmt_usd(c.market_cap)} · Liq {_fmt_usd(c.liquidity_usd)} "
            f"(LP/MC {c.lp_mc_ratio*100:.1f}%)",
            f"📊 Vol24h {_fmt_usd(c.volume_24h)} · tuổi {c.age_hours:.0f}h",
            f"🔁 Txs/Maker {c.txs_maker_ratio:.1f} · tax {tax} · top20 {top20} "
            f"· slip$1k≈{c.slippage_est_pct:.1f}%",
            f"📈 buy/sell vol 5m {bv5} · 15m {bv15} · net buy 1h {nb}",
            "",
            f"✅ {_esc('; '.join(c.reasons))}",
            "",
            f"<code>{c.token_address}</code>",
        ]
        buttons = [[
            {"text": "📈 Chart", "url": c.url},
            {"text": "🔍 GoPlus", "url": goplus_url},
        ]]
        return self.send("\n".join(lines), buttons)


# =========================================================================== #
#  HTTP client: retry + exponential backoff + rate-limit theo host
# =========================================================================== #

class HttpClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json",
                               "User-Agent": "base-meme-bot/1.0 (+research)"})
        self._last_call = {}  # host -> timestamp, để giãn cách

    def _throttle(self, host: str, min_interval: float):
        last = self._last_call.get(host, 0)
        wait = min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.time()

    def get(self, url: str, headers=None, min_interval: float = 0.0) -> Optional[dict]:
        host = url.split("/")[2]
        if min_interval:
            self._throttle(host, min_interval)
        delay = 1.0
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                r = self.s.get(url, headers=headers, timeout=self.cfg.request_timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    w = delay * (2 ** (attempt - 1))
                    print(f"    [429] {host} rate-limited, chờ {w:.0f}s")
                    time.sleep(w); continue
                if r.status_code in (401, 403):
                    return None  # thường là cần key -> bỏ qua
                if 500 <= r.status_code < 600:
                    time.sleep(delay * (2 ** (attempt - 1))); continue
                return None
            except (requests.RequestException, json.JSONDecodeError):
                time.sleep(delay * (2 ** (attempt - 1)))
        return None


# =========================================================================== #
#  GeckoTerminal API
# =========================================================================== #

GT = "https://api.geckoterminal.com/api/v2"

class GeckoTerminal:
    def __init__(self, http: HttpClient, cfg: Config):
        self.http, self.cfg = http, cfg

    def _get(self, path: str):
        return self.http.get(f"{GT}{path}", min_interval=self.cfg.gt_min_interval)

    def new_pools(self, page: int = 1) -> list:
        d = self._get(f"/networks/{self.cfg.network}/new_pools?page={page}")
        return (d or {}).get("data", []) if isinstance(d, dict) else []

    def trending_pools(self, page: int = 1) -> list:
        d = self._get(f"/networks/{self.cfg.network}/trending_pools?page={page}")
        return (d or {}).get("data", []) if isinstance(d, dict) else []

    def pool_trades(self, pool_address: str) -> list:
        """Tối đa 300 trade gần nhất trong 24h, có volume_in_usd + kind (buy/sell) + timestamp."""
        d = self._get(f"/networks/{self.cfg.network}/pools/{pool_address}/trades")
        return (d or {}).get("data", []) if isinstance(d, dict) else []


# =========================================================================== #
#  GoPlus Token Security (Base = 8453) — miễn phí
# =========================================================================== #

GOPLUS = "https://api.gopluslabs.io/api/v1"

class GoPlus:
    def __init__(self, http: HttpClient, cfg: Config):
        self.http, self.cfg = http, cfg

    def token_security(self, token_address: str) -> Optional[dict]:
        addr = token_address.lower()
        url = f"{GOPLUS}/token_security/{self.cfg.goplus_chain_id}?contract_addresses={addr}"
        d = self.http.get(url, min_interval=0.5)
        if not d or d.get("code") != 1:
            return None
        return (d.get("result") or {}).get(addr)


# =========================================================================== #
#  Mô hình ứng viên
# =========================================================================== #

@dataclass
class Candidate:
    token_address: str
    pool_address: str
    symbol: str
    name: str
    price_usd: float
    liquidity_usd: float
    market_cap: float
    lp_mc_ratio: float
    volume_24h: float
    age_hours: float
    txns_24h: int
    makers_24h: int
    txs_maker_ratio: float
    buy_sell_count_5m: float
    buy_sell_count_15m: float
    # tax / safety (GoPlus)
    buy_tax: Optional[float] = None
    sell_tax: Optional[float] = None
    is_honeypot: Optional[bool] = None
    top20_holder_pct: Optional[float] = None
    slippage_est_pct: float = 0.0
    # net buy volume (trades endpoint)
    buy_sell_vol_ratio_5m: Optional[float] = None
    buy_sell_vol_ratio_15m: Optional[float] = None
    net_buy_vs_liq_1h_pct: Optional[float] = None
    # hooks
    fresh_wallet_pct: Optional[float] = None
    smart_money_signal: Optional[dict] = None
    social_score: Optional[float] = None
    # kết quả
    score: float = 0.0
    reasons: list = field(default_factory=list)
    ko_reason: str = ""
    url: str = ""


# =========================================================================== #
#  Tiện ích
# =========================================================================== #

def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def gt_pool_to_candidate(pool: dict, cfg: Config) -> Optional[Candidate]:
    a = pool.get("attributes", {})
    rel = pool.get("relationships", {})
    base_tok = rel.get("base_token", {}).get("data", {}) or {}
    token_id = base_tok.get("id", "")  # dạng "base_0x..."
    token_addr = token_id.split("_", 1)[1] if "_" in token_id else ""
    pool_addr = a.get("address", "")

    liq = f(a.get("reserve_in_usd"))
    mc = f(a.get("market_cap_usd")) or f(a.get("fdv_usd"))
    vol24 = f((a.get("volume_usd") or {}).get("h24"))

    created = a.get("pool_created_at")
    age_h = 9999.0
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            pass

    tx = a.get("transactions", {}) or {}
    h24 = tx.get("h24", {}) or {}
    buys24, sells24 = int(h24.get("buys", 0)), int(h24.get("sells", 0))
    buyers24, sellers24 = int(h24.get("buyers", 0)), int(h24.get("sellers", 0))
    txns24 = buys24 + sells24
    makers24 = buyers24 + sellers24
    maker_ratio = (txns24 / makers24) if makers24 > 0 else 0.0

    def bs_count(win):
        w = tx.get(win, {}) or {}
        b, s = int(w.get("buys", 0)), int(w.get("sells", 0))
        return (b / s) if s > 0 else (b if b else 0.0)

    name = a.get("name", "?")
    symbol = name.split("/")[0].strip() if "/" in name else name

    return Candidate(
        token_address=token_addr, pool_address=pool_addr, symbol=symbol, name=name,
        price_usd=f(a.get("base_token_price_usd")),
        liquidity_usd=liq, market_cap=mc,
        lp_mc_ratio=(liq / mc) if mc > 0 else 0.0,
        volume_24h=vol24, age_hours=age_h, txns_24h=txns24,
        makers_24h=makers24, txs_maker_ratio=maker_ratio,
        buy_sell_count_5m=bs_count("m5"), buy_sell_count_15m=bs_count("m15"),
        url=f"https://www.geckoterminal.com/base/pools/{pool_addr}",
    )


def estimate_slippage_pct(liquidity_usd: float, trade_usd: float) -> float:
    """Ước tính trượt giá 1 chiều theo constant-product AMM (thô).
    Giả định ~1/2 thanh khoản nằm ở phía quote. impact ≈ trade / (quote_reserve + trade)."""
    quote_reserve = max(liquidity_usd / 2.0, 1.0)
    return (trade_usd / (quote_reserve + trade_usd)) * 100.0


# =========================================================================== #
#  TẦNG K.O
# =========================================================================== #

def ko_stage1(c: Candidate, cfg: Config) -> Optional[str]:
    """K.O không tốn thêm call. Trả lý do loại, hoặc None nếu qua."""
    if c.liquidity_usd < cfg.min_liquidity_usd:
        return f"liquidity ${c.liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}"
    if c.volume_24h < cfg.min_volume_24h_usd:
        return f"vol24h ${c.volume_24h:,.0f} thấp"
    if not (cfg.min_age_hours <= c.age_hours <= cfg.max_age_hours):
        return f"tuổi {c.age_hours:.1f}h ngoài khoảng"
    if c.txns_24h < cfg.min_txns_24h:
        return f"txns24h {c.txns_24h} < {cfg.min_txns_24h}"
    # LP/MC ratio (tiered)
    if c.market_cap > 0:
        need = cfg.lp_mc_min_over_1m if c.market_cap >= cfg.mc_threshold else cfg.lp_mc_min_under_1m
        if c.lp_mc_ratio < need:
            return f"LP/MC {c.lp_mc_ratio*100:.1f}% < {need*100:.0f}% (thanh khoản mỏng)"
    # Slippage ước tính
    c.slippage_est_pct = estimate_slippage_pct(c.liquidity_usd, cfg.slippage_trade_size_usd)
    if c.slippage_est_pct > cfg.max_slippage_pct:
        return f"slippage ${cfg.slippage_trade_size_usd:.0f}≈{c.slippage_est_pct:.1f}% > {cfg.max_slippage_pct}%"
    # Wash trading (Txs/Maker)
    if c.txs_maker_ratio > cfg.wash_ratio_alert:
        return f"Txs/Maker {c.txs_maker_ratio:.1f} > {cfg.wash_ratio_alert} (nghi wash trading)"
    return None


def ko_stage2_goplus(c: Candidate, cfg: Config, gp: GoPlus) -> Optional[str]:
    """K.O dùng GoPlus (tax/honeypot/holder). Trả lý do loại, hoặc None."""
    sec = gp.token_security(c.token_address)
    if sec is None:
        return "goplus_no_data"  # không có data -> loại thận trọng (memecoin mới đôi khi chưa index)

    c.is_honeypot = str(sec.get("is_honeypot", "0")) == "1"
    if c.is_honeypot:
        return "HONEYPOT (GoPlus)"
    if str(sec.get("cannot_sell_all", "0")) == "1":
        return "cannot_sell_all"
    if str(sec.get("transfer_pausable", "0")) == "1":
        return "transfer_pausable (có thể khóa bán)"

    c.buy_tax = f(sec.get("buy_tax")) * 100
    c.sell_tax = f(sec.get("sell_tax")) * 100
    if c.buy_tax > cfg.max_tax_pct or c.sell_tax > cfg.max_tax_pct:
        return f"tax buy/sell {c.buy_tax:.1f}%/{c.sell_tax:.1f}% > {cfg.max_tax_pct}%"

    # Top-holder concentration (proxy insider/cluster): trừ LP/locked/burn
    holders = sec.get("holders", []) or []
    burn_tags = ("lp", "burn", "null", "dead", "lock")
    conc = 0.0
    for h in holders[:20]:
        if str(h.get("is_locked", "0")) == "1":
            continue
        tag = (h.get("tag") or "").lower()
        if any(b in tag for b in burn_tags):
            continue
        conc += f(h.get("percent")) * 100
    c.top20_holder_pct = round(conc, 2)
    if c.top20_holder_pct > cfg.max_top20_holder_pct:
        return f"top20 holders {c.top20_holder_pct:.1f}% > {cfg.max_top20_holder_pct}% (insider/cluster)"

    # Fresh wallet KO (HOOK) — chỉ chạy nếu bật + có nguồn
    if cfg.enforce_fresh_wallet_ko:
        fw = hook_fresh_wallet_ratio(c.token_address, cfg)
        c.fresh_wallet_pct = fw
        if fw is not None and fw > cfg.max_fresh_wallet_pct:
            return f"fresh wallets {fw:.0f}% > {cfg.max_fresh_wallet_pct}%"
    return None


# =========================================================================== #
#  Net Buy Volume thật (trades endpoint) — cho shortlist
# =========================================================================== #

def compute_net_buy_volume(c: Candidate, cfg: Config, gt: GeckoTerminal):
    trades = gt.pool_trades(c.pool_address)
    if not trades:
        return
    now = time.time()
    win = {"5m": 300, "15m": 900, "1h": 3600}
    agg = {k: {"buy": 0.0, "sell": 0.0} for k in win}
    for t in trades:
        a = t.get("attributes", {})
        kind = a.get("kind", "")  # 'buy' hoặc 'sell'
        vol = f(a.get("volume_in_usd"))
        ts = a.get("block_timestamp")
        try:
            tsec = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else now
        except Exception:
            tsec = now
        age = now - tsec
        for k, span in win.items():
            if age <= span and kind in ("buy", "sell"):
                agg[k][kind] += vol

    def ratio(k):
        b, s = agg[k]["buy"], agg[k]["sell"]
        return (b / s) if s > 0 else (b if b else None)

    c.buy_sell_vol_ratio_5m = ratio("5m")
    c.buy_sell_vol_ratio_15m = ratio("15m")
    net_1h = agg["1h"]["buy"] - agg["1h"]["sell"]
    c.net_buy_vs_liq_1h_pct = (net_1h / c.liquidity_usd * 100) if c.liquidity_usd > 0 else None


# =========================================================================== #
#  HOOKS — KHÔNG có API free. Cắm nguồn trả phí của anh vào đây.
# =========================================================================== #

def hook_fresh_wallet_ratio(token_address: str, cfg: Config) -> Optional[float]:
    """% supply nằm ở ví tạo <24h. Cần enumerate holders (Basescan) + first-tx của từng ví.
    Rất nặng call -> tự triển khai nếu có BASESCAN_API_KEY và chấp nhận chi phí."""
    if not cfg.basescan_api_key:
        return None
    # TODO: Etherscan V2 multichain (chainid=8453): tokenholderlist -> với mỗi ví lấy txlist sớm nhất.
    return None

def hook_smart_money(token_address: str, cfg: Config) -> Optional[dict]:
    """Tín hiệu smart-money chất lượng cao (WinRate>60%, PnL30d>$50k, >=3 ví độc lập gom trong 30').
    GMGN không có API public; Arkham/Nansen/Cielo cần key trả phí. Trả None = bỏ qua."""
    if not cfg.smart_money_api_key:
        return None
    # TODO: cắm provider của anh, trả {"wallets": n, "usd_bought": x, "avg_winrate": y}
    return None

def hook_social_score(token_address: str, symbol: str, cfg: Config) -> Optional[float]:
    """TweetScout X-Score / Kaito Mindshare / KOL bluecheck %. Cần key trả phí."""
    if not cfg.tweetscout_api_key:
        return None
    # TODO: cắm TweetScout/Kaito, trả điểm chuẩn hoá 0-100
    return None


# =========================================================================== #
#  Chấm điểm (chỉ cho ứng viên đã qua toàn bộ K.O)
# =========================================================================== #

def score_candidate(c: Candidate, cfg: Config):
    score, reasons = 0.0, []

    # 1) Txs/Maker trong vùng lành mạnh [2.5, 6.0] (tối đa 22)
    r = c.txs_maker_ratio
    if cfg.healthy_ratio_lo <= r <= cfg.healthy_ratio_hi:
        score += 22; reasons.append(f"dòng tiền tự nhiên (Txs/Maker {r:.1f})")
    elif r < cfg.healthy_ratio_lo:
        score += 12; reasons.append(f"Txs/Maker {r:.1f} (ít giao dịch/ví)")
    else:  # 6..15
        score += 6; reasons.append(f"Txs/Maker {r:.1f} (hơi cao)")

    # 2) Net Buy Volume 5m/15m (tối đa 22)
    hits = 0
    for lbl, val in (("5m", c.buy_sell_vol_ratio_5m), ("15m", c.buy_sell_vol_ratio_15m)):
        if val is not None and val >= cfg.buy_sell_vol_ratio_min:
            hits += 1; reasons.append(f"buy/sell vol {lbl} {val:.2f}")
    score += hits * 11

    # 3) Net buy 1h vs liquidity >= +15% (tối đa 14)
    nb = c.net_buy_vs_liq_1h_pct
    if nb is not None:
        if nb >= cfg.net_buy_vs_liq_min_pct:
            score += 14; reasons.append(f"net buy 1h +{nb:.0f}% liq")
        elif nb > 0:
            score += 6

    # 4) LP/MC dày (an toàn thoát lệnh) (tối đa 12)
    if c.lp_mc_ratio >= 0.15:
        score += 12; reasons.append(f"LP/MC {c.lp_mc_ratio*100:.0f}% (dày)")
    elif c.lp_mc_ratio >= 0.08:
        score += 7

    # 5) Holder concentration thấp = an toàn (tối đa 10)
    if c.top20_holder_pct is not None:
        if c.top20_holder_pct <= 6:
            score += 10; reasons.append(f"phân phối tốt (top20 {c.top20_holder_pct:.0f}%)")
        elif c.top20_holder_pct <= 12:
            score += 5

    # 6) Tax = 0 (tối đa 6)
    if (c.buy_tax or 0) == 0 and (c.sell_tax or 0) == 0:
        score += 6; reasons.append("tax 0/0")

    # 7) MC vào sớm (tối đa 8)
    if c.market_cap <= 1_000_000:
        score += 8; reasons.append("MC <1M (sớm)")
    elif c.market_cap <= 3_000_000:
        score += 5

    # 8) HOOKS nếu có (smart money +15 / social +10)
    if c.smart_money_signal:
        n = c.smart_money_signal.get("wallets", 0)
        if n >= 3:
            score += 15; reasons.append(f"smart money: {n} ví gom")
    if c.social_score is not None and c.social_score >= 50:
        score += 10; reasons.append(f"social score {c.social_score:.0f}")

    c.score = round(max(0, min(100, score)), 1)
    c.reasons = reasons


# =========================================================================== #
#  Scanner
# =========================================================================== #

class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = HttpClient(cfg)
        self.gt = GeckoTerminal(self.http, cfg)
        self.gp = GoPlus(self.http, cfg)
        self.tg = TelegramNotifier(cfg)
        self.seen = self._load_seen()

    def _load_seen(self) -> set:
        if os.path.exists(self.cfg.seen_file):
            try:
                return set(json.load(open(self.cfg.seen_file)))
            except Exception:
                return set()
        return set()

    def _save_seen(self):
        try:
            json.dump(sorted(self.seen), open(self.cfg.seen_file, "w"))
        except Exception:
            pass

    def discover(self) -> list[Candidate]:
        pools = []
        for p in range(1, self.cfg.discovery_pages + 1):
            pools += self.gt.new_pools(p)
        pools += self.gt.trending_pools(1)
        cands, seen_pool = [], set()
        for pool in pools:
            c = gt_pool_to_candidate(pool, self.cfg)
            if c and c.token_address and c.pool_address not in seen_pool:
                seen_pool.add(c.pool_address)
                cands.append(c)
        return cands

    def run_once(self) -> list[Candidate]:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n=== BASE scan {stamp} ===")
        cands = self.discover()
        print(f"  Khám phá: {len(cands)} pool")

        # K.O tầng 1
        survivors = []
        for c in cands:
            ko = ko_stage1(c, self.cfg)
            if ko:
                continue
            survivors.append(c)
        print(f"  Qua K.O tầng 1 (LP/MC, slippage, wash...): {len(survivors)}")

        # K.O tầng 2 (GoPlus)
        safe = []
        for c in survivors:
            ko = ko_stage2_goplus(c, self.cfg, self.gp)
            if ko:
                c.ko_reason = ko
                continue
            safe.append(c)
        print(f"  Qua K.O tầng 2 (tax/honeypot/holders): {len(safe)}")

        # Net buy volume + hooks + score cho shortlist
        hits = []
        for c in safe:
            compute_net_buy_volume(c, self.cfg, self.gt)
            c.smart_money_signal = hook_smart_money(c.token_address, self.cfg)
            c.social_score = hook_social_score(c.token_address, c.symbol, self.cfg)
            score_candidate(c, self.cfg)
            if c.score >= self.cfg.min_score_to_alert:
                hits.append(c)
        hits.sort(key=lambda x: x.score, reverse=True)

        fresh = [c for c in hits if c.token_address not in self.seen]
        cap = self.cfg.max_alerts_per_run
        to_send = fresh[:cap]  # chỉ báo top N/lần -> chống flood (nhất là lần chạy đầu)
        held = len(fresh) - len(to_send)
        print(f"  Đạt điểm & MỚI: {len(fresh)} (gửi {len(to_send)}, "
              f"giữ lại {held} cho lần sau)")

        for c in to_send:
            self._print(c)
            self._csv(c)
            self.tg.send_alert(c)          # <-- gửi cảnh báo Telegram
            self.seen.add(c.token_address)  # chỉ đánh dấu seen cái đã gửi
        self._save_seen()

        # dòng tổng kết (tùy chọn) để anh biết bot còn sống dù 0 hit
        if self.cfg.telegram_send_summary and self.tg.enabled:
            stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
            if to_send:
                top = to_send[0]
                extra = f" (+{held} chờ lượt sau)" if held else ""
                self.tg.send(f"🟢 <b>{len(to_send)}</b> tín hiệu mới lúc {stamp}{extra} "
                             f"· cao nhất ${_esc(top.symbol)} ({top.score})")
            else:
                self.tg.send(f"⚪ Không có tín hiệu đạt ngưỡng lúc {stamp}")
        return to_send

    def _print(self, c: Candidate):
        print(f"\n  ⭐ [{c.score}] {c.symbol} — {c.name}")
        print(f"     MC ${c.market_cap:,.0f} | Liq ${c.liquidity_usd:,.0f} "
              f"(LP/MC {c.lp_mc_ratio*100:.1f}%) | Vol24h ${c.volume_24h:,.0f} | tuổi {c.age_hours:.0f}h")
        print(f"     Txs/Maker {c.txs_maker_ratio:.1f} | tax {c.buy_tax:.1f}/{c.sell_tax:.1f}% "
              f"| top20 {c.top20_holder_pct}% | slip$1k≈{c.slippage_est_pct:.1f}%")
        bv5 = f"{c.buy_sell_vol_ratio_5m:.2f}" if c.buy_sell_vol_ratio_5m else "-"
        bv15 = f"{c.buy_sell_vol_ratio_15m:.2f}" if c.buy_sell_vol_ratio_15m else "-"
        nb = f"{c.net_buy_vs_liq_1h_pct:+.0f}%" if c.net_buy_vs_liq_1h_pct is not None else "-"
        print(f"     buy/sell vol 5m/15m {bv5}/{bv15} | net buy 1h vs liq {nb}")
        print(f"     Lý do: {'; '.join(c.reasons)}")
        print(f"     {c.url}")
        print(f"     CA: {c.token_address}")

    def _csv(self, c: Candidate):
        path = self.cfg.output_csv
        new = not os.path.exists(path)
        row = asdict(c)
        row["reasons"] = " | ".join(c.reasons)
        row["smart_money_signal"] = json.dumps(c.smart_money_signal) if c.smart_money_signal else ""
        row["scanned_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="Base memecoin scanner (K.O + on-chain flow)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int)
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--pages", type=int, help="số trang new_pools để quét")
    ap.add_argument("--test-telegram", action="store_true", help="gửi 1 tin thử rồi thoát")
    args = ap.parse_args()

    cfg = Config()

    if args.test_telegram:
        tg = TelegramNotifier(cfg)
        ok = tg.send("🤖 <b>Base meme bot</b> đã kết nối Telegram thành công.\n"
                     "Nếu anh thấy tin này, cấu hình token + chat_id đã đúng.")
        print("Gửi thử:", "OK ✅" if ok else "THẤT BẠI ❌ (kiểm tra token/chat_id & đã /start bot chưa)")
        return
    if args.interval: cfg.interval_seconds = args.interval
    if args.min_score is not None: cfg.min_score_to_alert = args.min_score
    if args.pages: cfg.discovery_pages = args.pages

    sc = Scanner(cfg)
    if args.loop:
        print(f"Loop mỗi {cfg.interval_seconds}s. Ctrl+C để dừng.")
        try:
            while True:
                sc.run_once()
                print(f"  ...nghỉ {cfg.interval_seconds}s")
                time.sleep(cfg.interval_seconds)
        except KeyboardInterrupt:
            print("\nDừng.")
    else:
        sc.run_once()


if __name__ == "__main__":
    main()
