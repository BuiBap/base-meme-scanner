#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
free_sources.py — Các nguồn dữ liệu MIỄN PHÍ bổ sung cho base_meme_bot.py (chain Base).

Module này lấp 3 chỗ trống mà bot đang để None:
  1) fresh_wallet_ratio  -> Blockscout Base (holder list + tuổi ví)
  2) social_score        -> GeckoTerminal token info + DexScreener boosts
  3) (mới) gt_score      -> điểm chất lượng token của GeckoTerminal
  4) (mới) holder_count  -> số holder THẬT (GeckoTerminal/DexScreener không trả field này)

SỰ THẬT VỀ TỪNG NGUỒN (đã kiểm chứng 7/2026, không bịa):
  - GeckoTerminal /tokens/{addr}/info : MIỄN PHÍ, không cần key. Trả gt_score,
    twitter_handle, telegram_handle, websites, description.
  - Blockscout Base : MIỄN PHÍ nhưng TỪ 01/07/2026 mọi traffic chuyển sang Pro API
    và cần API key (free tier vẫn đủ dùng). Lấy key tại https://dev.blockscout.com
    Key cũ dạng UUID per-instance đã bị vô hiệu hoá.
  - DexScreener /token-boosts : MIỄN PHÍ, không cần key.

KHÔNG dùng được (đã kiểm chứng, đừng mất thời gian):
  - Kaito Yaps Open Protocol : ĐÃ KHAI TỬ 15/01/2026 (X thu hồi quyền API).
    Trang docs giờ trả 404. Không xây gì trên đó.
  - Helius : chỉ hỗ trợ Solana, KHÔNG dùng được cho Base.
  - Smart money chất lượng cao : không có nguồn miễn phí nào. Rẻ nhất là
    Cielo Whale $199/tháng (API chỉ mở ở tier này). Vẫn để hook rỗng.

CÁCH DÙNG: xem hướng dẫn ở cuối file.
"""

import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
#  Cấu hình
# --------------------------------------------------------------------------- #

# Blockscout đã chuyển sang PRO API hợp nhất đa chain (từ dev.blockscout.com).
# Có 2 kiểu URL khác nhau cho 2 họ endpoint (đã xác nhận qua docs.blockscout.com):
#   - REST v2 (holders, counters...) : path-based -> api.blockscout.com/{chain_id}/api/v2/...
#   - Legacy Etherscan-compat (txlist): query-based -> api.blockscout.com/v2/api?chain_id=...
BLOCKSCOUT_PRO = "https://api.blockscout.com"
BLOCKSCOUT_CHAIN_ID = os.getenv("BLOCKSCOUT_CHAIN_ID", "8453")   # Base
BLOCKSCOUT_KEY = os.getenv("BLOCKSCOUT_API_KEY", "")             # free key: dev.blockscout.com (proapi_...)
GT_API = "https://api.geckoterminal.com/api/v2"
DEXSCREENER = "https://api.dexscreener.com"

# Ngưỡng cho fresh wallet
FRESH_WALLET_MAX_AGE_H = 24.0      # ví tạo < 24h = "fresh"
FRESH_WALLET_TOP_N = 15            # chỉ soi top N holder (giới hạn số call)
FRESH_WALLET_MIN_CHECKED = 5       # dưới số này thì coi như không đủ dữ liệu -> None

_session = requests.Session()
_session.headers.update({"Accept": "application/json",
                         "User-Agent": "base-meme-bot-free-sources/1.0"})
_last_call = {}


def _throttle(host: str, min_interval: float):
    last = _last_call.get(host, 0)
    wait = min_interval - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def _get(url: str, min_interval: float = 0.3, timeout: int = 15) -> Optional[dict]:
    """GET có throttle + backoff. Lỗi -> None (bot tự bỏ qua, không sập)."""
    host = url.split("/")[2]
    _throttle(host, min_interval)
    delay = 1.0
    for attempt in range(1, 4):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(delay * (2 ** (attempt - 1)))
                continue
            if r.status_code in (401, 403):
                if "blockscout" in host and not BLOCKSCOUT_KEY:
                    print("    [blockscout] cần API key -> lấy free tại https://dev.blockscout.com "
                          "rồi set BLOCKSCOUT_API_KEY")
                elif "blockscout" in host:
                    print("    [blockscout] key có thể sai/hết credit -> kiểm tra dev.blockscout.com/dashboard")
                return None
            if 500 <= r.status_code < 600:
                time.sleep(delay * (2 ** (attempt - 1)))
                continue
            return None
        except (requests.RequestException, ValueError):
            time.sleep(delay * (2 ** (attempt - 1)))
    return None


def _bs_rest_url(path: str) -> str:
    """URL cho họ REST v2 (path-based): /{chain_id}/api/v2/tokens/{addr}/holders..."""
    url = f"{BLOCKSCOUT_PRO}/{BLOCKSCOUT_CHAIN_ID}/api/v2{path}"
    if BLOCKSCOUT_KEY:
        sep = "&" if "?" in path else "?"
        url += f"{sep}apikey={BLOCKSCOUT_KEY}"
    return url


def _bs_legacy_url(module: str, action: str, extra: str = "") -> str:
    """URL cho họ Etherscan-compat cũ (query-based): /v2/api?chain_id=...&module=...&action=..."""
    url = f"{BLOCKSCOUT_PRO}/v2/api?chain_id={BLOCKSCOUT_CHAIN_ID}&module={module}&action={action}"
    if extra:
        url += f"&{extra}"
    if BLOCKSCOUT_KEY:
        url += f"&apikey={BLOCKSCOUT_KEY}"
    return url


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
#  1) GeckoTerminal token info — MIỄN PHÍ, không cần key
# --------------------------------------------------------------------------- #

def gt_token_info(token_address: str, network: str = "base") -> Optional[dict]:
    """Trả dict gồm gt_score + các link social. None nếu không lấy được."""
    d = _get(f"{GT_API}/networks/{network}/tokens/{token_address}/info", min_interval=2.1)
    if not isinstance(d, dict):
        return None
    attr = ((d.get("data") or {}).get("attributes")) or {}
    return {
        "gt_score": _f(attr.get("gt_score"), 0.0),
        "twitter_handle": attr.get("twitter_handle"),
        "telegram_handle": attr.get("telegram_handle"),
        "websites": attr.get("websites") or [],
        "description": attr.get("description") or "",
    }


# --------------------------------------------------------------------------- #
#  2) DexScreener boosts — MIỄN PHÍ, không cần key
# --------------------------------------------------------------------------- #

def dexscreener_boost(token_address: str, chain_id: str = "base") -> int:
    """Token có đang được trả tiền boost trên DexScreener không.
    Boost KHÔNG phải tín hiệu tốt/xấu tuyệt đối — nó chỉ cho biết có người
    bỏ tiền quảng bá. Dev scam cũng boost, dự án thật cũng boost."""
    total = 0
    for path in ("/token-boosts/latest/v1", "/token-boosts/top/v1"):
        d = _get(f"{DEXSCREENER}{path}", min_interval=1.0)
        items = d if isinstance(d, list) else (d.get("data", []) if isinstance(d, dict) else [])
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if it.get("chainId") == chain_id and \
               (it.get("tokenAddress") or "").lower() == token_address.lower():
                total = max(total, int(_f(it.get("totalAmount") or it.get("amount"))))
    return total


# --------------------------------------------------------------------------- #
#  3) Blockscout Base — holder count + fresh wallet ratio
# --------------------------------------------------------------------------- #

def blockscout_holder_count(token_address: str) -> Optional[int]:
    """Số holder THẬT của token. GeckoTerminal/DexScreener không trả field này."""
    d = _get(_bs_rest_url(f"/tokens/{token_address}/counters"), min_interval=0.3)
    if isinstance(d, dict) and d.get("token_holders_count") is not None:
        try:
            return int(d["token_holders_count"])
        except (TypeError, ValueError):
            return None
    # fallback: endpoint token info
    d = _get(_bs_rest_url(f"/tokens/{token_address}"), min_interval=0.3)
    if isinstance(d, dict) and d.get("holders") is not None:
        try:
            return int(d["holders"])
        except (TypeError, ValueError):
            return None
    return None


def _wallet_first_tx_age_hours(address: str) -> Optional[float]:
    """Tuổi ví = thời gian từ giao dịch ĐẦU TIÊN đến giờ.
    Dùng endpoint Etherscan-compat với sort=asc&offset=1 -> lấy tx cũ nhất trong 1 call
    (REST v2 trả mới-nhất-trước nên phải phân trang rất tốn, không dùng)."""
    url = _bs_legacy_url("account", "txlist",
                        f"address={address}&sort=asc&page=1&offset=1")
    d = _get(url, min_interval=0.25)
    if not isinstance(d, dict):
        return None
    result = d.get("result")
    if not isinstance(result, list) or not result:
        return None
    ts = result[0].get("timeStamp")
    try:
        first = float(ts)
    except (TypeError, ValueError):
        return None
    return max(0.0, (time.time() - first) / 3600.0)


def fresh_wallet_ratio(token_address: str,
                       top_n: int = FRESH_WALLET_TOP_N,
                       max_age_h: float = FRESH_WALLET_MAX_AGE_H) -> Optional[float]:
    """% supply nằm ở ví MỚI TẠO (<24h), tính trên top N holder.

    ĐÂY LÀ PROXY, KHÔNG PHẢI SỐ TUYỆT ĐỐI — chỉ soi top N ví thay vì toàn bộ
    holder (soi hết thì tốn hàng nghìn call). Đã loại contract (LP pool, router,
    burn) khỏi phép tính vì chúng không phải ví người thật.

    Trả None nếu không đủ dữ liệu (thà không biết còn hơn báo số sai).
    """
    d = _get(_bs_rest_url(f"/tokens/{token_address}/holders"), min_interval=0.3)
    if not isinstance(d, dict):
        return None
    items = d.get("items")
    if not isinstance(items, list) or not items:
        return None

    fresh_pct = 0.0
    checked = 0
    for h in items[:top_n * 2]:      # lấy dư rồi lọc contract
        if checked >= top_n:
            break
        if not isinstance(h, dict):
            continue
        addr_obj = h.get("address") or {}
        addr = addr_obj.get("hash") if isinstance(addr_obj, dict) else None
        if not addr:
            continue
        # bỏ qua contract: LP pool, router, burn... không phải ví người thật
        if isinstance(addr_obj, dict) and addr_obj.get("is_contract"):
            continue

        # % supply ví này nắm
        pct = None
        for key in ("percentage", "share", "percent"):
            if h.get(key) is not None:
                pct = _f(h.get(key))
                break
        if pct is None:
            continue
        if 0 < pct <= 1:             # có instance trả dạng phân số
            pct *= 100.0

        age_h = _wallet_first_tx_age_hours(addr)
        checked += 1
        if age_h is not None and age_h < max_age_h:
            fresh_pct += pct

    if checked < FRESH_WALLET_MIN_CHECKED:
        return None                  # không đủ mẫu -> không kết luận
    return round(fresh_pct, 2)


# --------------------------------------------------------------------------- #
#  4) Gộp thành social score 0-100
# --------------------------------------------------------------------------- #

def social_presence_score(token_address: str, symbol: str = "",
                          network: str = "base") -> Optional[float]:
    """Điểm 'hiện diện xã hội' 0-100 từ nguồn MIỄN PHÍ.

    LƯU Ý VỀ CHẤT LƯỢNG TÍN HIỆU: đây KHÔNG phải TweetScout X-Score hay Kaito
    mindshare. Nó chỉ đo token CÓ hạ tầng social hay không (twitter/telegram/web,
    điểm gt_score của GeckoTerminal, có được boost không) — không đo được chất
    lượng follower hay % tương tác từ KOL tích xanh. Coi là bộ lọc thô.
    """
    info = gt_token_info(token_address, network)
    if info is None:
        return None

    score = 0.0
    # gt_score của GeckoTerminal (0-100): trọng số lớn nhất vì đây là đánh giá
    # tổng hợp có sẵn (thanh khoản, độ tin cậy pool, thông tin token...)
    score += min(50.0, _f(info.get("gt_score")) * 0.5)

    if info.get("twitter_handle"):
        score += 20
    if info.get("telegram_handle"):
        score += 12
    if info.get("websites"):
        score += 10
    if len(info.get("description") or "") >= 40:
        score += 3

    # boost: cộng nhẹ thôi, vì scam cũng boost được
    if dexscreener_boost(token_address, network) > 0:
        score += 5

    return round(min(100.0, score), 1)


# --------------------------------------------------------------------------- #
#  HƯỚNG DẪN CẮM VÀO base_meme_bot.py
# --------------------------------------------------------------------------- #
"""
BƯỚC 1 — Upload file này (free_sources.py) vào repo, cùng cấp base_meme_bot.py

BƯỚC 2 — Trong base_meme_bot.py, THAY TOÀN BỘ 3 hàm hook cũ bằng đoạn dưới đây
         (tìm khối bắt đầu bằng "def hook_fresh_wallet_ratio" và kết thúc ở
          "return None" của hook_social_score):

# --- nguồn miễn phí (module free_sources.py) ---
try:
    import free_sources
except ImportError:
    free_sources = None

def hook_fresh_wallet_ratio(token_address: str, cfg: Config) -> Optional[float]:
    if free_sources is None:
        return None
    return free_sources.fresh_wallet_ratio(token_address)

def hook_smart_money(token_address: str, cfg: Config) -> Optional[dict]:
    # Không có nguồn miễn phí. Rẻ nhất là Cielo Whale $199/thang (API chi mo o tier nay).
    if not cfg.smart_money_api_key:
        return None
    return None

def hook_social_score(token_address: str, symbol: str, cfg: Config) -> Optional[float]:
    if free_sources is None:
        return None
    return free_sources.social_presence_score(token_address, symbol)


BƯỚC 3 — Bật K.O fresh-wallet (tuỳ chọn). Trong class Config, đổi:
             enforce_fresh_wallet_ko: bool = False
         thành True nếu muốn LOẠI token có >35% supply ở ví mới.
         Khuyến nghị: chạy False vài ngày trước để xem số liệu có hợp lý không.

BƯỚC 4 — Lấy API key Blockscout MIỄN PHÍ tại https://dev.blockscout.com
         rồi thêm vào GitHub Secrets tên BLOCKSCOUT_API_KEY, và thêm dòng này
         vào phần env: của workflow scanner.yml:
             BLOCKSCOUT_API_KEY: ${{ secrets.BLOCKSCOUT_API_KEY }}

LƯU Ý CHI PHÍ THỜI GIAN: fresh_wallet_ratio tốn ~15-30 call/token nên CHỈ chạy
ở tầng cuối (shortlist đã qua hết K.O). Nếu 1 lần quét có 5 token vào shortlist
thì khoảng 150 call -> vẫn nằm trong free tier, nhưng job sẽ lâu hơn ~30-60 giây.
"""
