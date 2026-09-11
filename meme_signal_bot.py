#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meme_signal_bot.py — Bản đồ nhiệt meme đa chain (tự động mở rộng)

Quét GeckoTerminal trending_pools cho các chain đã biết + tự động khám phá
chain mới. Chain lạ xuất hiện >= 3 lần được tự động thêm vào danh sách cố định.

Chạy thủ công:   python meme_signal_bot.py
Test không push: python meme_signal_bot.py --dry
"""

import os, sys, time, json, math, base64, datetime, statistics, argparse
import urllib.request, urllib.error
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests as _requests
    _USE_REQUESTS = True
except ImportError:
    _USE_REQUESTS = False

# ─── Chains cố định ────────────────────────────────────────────────────────────

CHAINS = [
    ("bsc",           "BNB Chain"),
    ("robinhood",     "Robinhood"),
    ("solana",        "Solana"),
    ("hyperliquid",   "HyperEVM"),
    ("base",          "Base"),
    ("avax",          "Avalanche"),
    ("sonic",         "Sonic"),
    ("sui-network",   "Sui"),
    ("plasma",        "Plasma"),
    ("sei-evm",       "Sei"),
    ("monad",         "Monad"),
    ("eth",           "Ethereum"),
    ("polygon_pos",   "Polygon"),
    ("arbitrum",      "Arbitrum"),
    ("berachain",     "Berachain"),
    ("abstract",      "Abstract"),
    ("unichain",      "Unichain"),
    ("tron",          "Tron"),
]

INFRA_SYMBOLS = {
    "USDC","USDT","BUSD","DAI","FRAX","TUSD","FDUSD","USDE","USDS","USDP",
    "GUSD","LUSD","SUSD","USDD","AUSD","CUSD","MUSD","PYUSD","EURC","USDB",
    "USDX","USD+","DOLA","USDV","USDC.E","USDT.E","USDC.e","USDT.e",
    "WETH","WBNB","WBTC","WMATIC","WAVAX","WSOL","WFTM","WONE","WCELO",
    "WROSE","WKLAY","WMON","WSEI","WHYPE","WETH.E","WBTC.E",
    "ETH","BNB","BTC","MATIC","AVAX","SOL","FTM","CRO",
    "STETH","WSTETH","RETH","CBETH","FRXETH","SFRXETH","WEETH","SWETH",
    "CAKE","UNI","AAVE","COMP","MKR","SNX","CRV","LDO","BTCB",
    "LINK","ARB","OP","SHIB",
    "HYPE","UBTC","UETH","USOL","UBNB","UAVAX","UMATIC","UATOM",
}

GT_BASE    = "https://api.geckoterminal.com/api/v2"
GT_GAP     = 12     # giây giữa các API call
NEW_HOURS  = 48

GITHUB_REPO       = "BuiBap/base-meme-scanner"
GITHUB_BRANCH     = "gh-pages"
GITHUB_FILE       = "heatmap.html"
CANDIDATES_FILE   = "candidates.json"

# Auto-discovery settings
MAX_DISCOVER      = 5    # số chain lạ thử tối đa mỗi lần chạy
MIN_MEME_POOLS    = 3    # cần >= N pool meme mới tính là chain hoạt động
PROMOTE_THRESHOLD = 3    # xuất hiện >= N lần → tự động vào danh sách

# ─── Env ───────────────────────────────────────────────────────────────────────

def load_dotenv(path: str = ".env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PoolInfo:
    symbol:    str
    age_hours: float
    lp_usd:    float
    vol_24h:   float
    h24_pct:   float
    h1_pct:    float

@dataclass
class ChainStats:
    slug:        str
    name:        str
    raw_count:   int   = 0
    meme_count:  int   = 0
    liq_usd:     float = 0.0
    vol_24h:     float = 0.0
    median_lp:   float = 0.0
    median_h24:  float = 0.0
    turnover:    float = 0.0
    blast_count: int   = 0
    new_count:   int   = 0
    heat_score:  float = 0.0
    pools:       list  = field(default_factory=list)
    error:       Optional[str] = None
    is_new:      bool  = False   # True nếu là chain mới khám phá lần này

# ─── HTTP helper ───────────────────────────────────────────────────────────────

def _http_get(url: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "meme-signal-bot/2.0"}
    if _USE_REQUESTS:
        for attempt in range(4):
            r = _requests.get(url, headers=headers, timeout=20)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"    429 rate limit, chờ {wait}s ...", end=" ", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def _http_get_silent(url: str) -> Optional[dict]:
    try:
        return _http_get(url)
    except Exception:
        return None

def _token_map(included: list) -> dict:
    m = {}
    for item in (included or []):
        if item.get("type") == "token":
            m[item["id"]] = item.get("attributes", {}).get("symbol", "?").upper()
    return m

def _age_hours(created_at: str) -> float:
    if not created_at:
        return 9999.0
    try:
        ts = created_at.rstrip("Z")
        if "." in ts:
            ts = ts[:ts.index(".")]
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        return 9999.0

# ─── GeckoTerminal fetch ───────────────────────────────────────────────────────

def fetch_chain(slug: str, name: str, is_new: bool = False) -> ChainStats:
    stats = ChainStats(slug=slug, name=name, is_new=is_new)
    url = f"{GT_BASE}/networks/{slug}/trending_pools?include=base_token,quote_token&page=1"
    try:
        data = _http_get(url)
    except Exception as e:
        stats.error = str(e)
        return stats

    items = data.get("data", [])
    tok   = _token_map(data.get("included", []))
    stats.raw_count = len(items)

    pools = []
    for pool in items:
        attrs = pool.get("attributes", {})
        rels  = pool.get("relationships", {})
        base_id = (rels.get("base_token", {}).get("data") or {}).get("id", "")
        sym     = tok.get(base_id, "?").upper()

        if sym in INFRA_SYMBOLS or sym == "?":
            continue

        try:
            reserve = attrs.get("reserve_in_usd")
            if reserve is None:
                mc = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
                lp = mc * 0.03 if mc > 0 else 0
            else:
                lp = float(reserve)
            vol   = float((attrs.get("volume_usd") or {}).get("h24") or 0)
            h24   = float((attrs.get("price_change_percentage") or {}).get("h24") or 0)
            h1    = float((attrs.get("price_change_percentage") or {}).get("h1") or 0)
            age_h = _age_hours(attrs.get("pool_created_at", ""))
        except (TypeError, ValueError):
            continue

        if lp < 500:
            continue

        pools.append(PoolInfo(sym, age_h, lp, vol, h24, h1))

    stats.pools      = pools
    stats.meme_count = len(pools)

    if not pools:
        return stats

    lps  = [p.lp_usd  for p in pools]
    h24s = [p.h24_pct for p in pools]

    stats.liq_usd    = sum(lps)
    stats.vol_24h    = sum(p.vol_24h for p in pools)
    stats.median_lp  = statistics.median(lps)
    stats.median_h24 = statistics.median(h24s)
    stats.turnover   = stats.vol_24h / stats.liq_usd if stats.liq_usd > 0 else 0.0
    stats.blast_count = sum(1 for p in pools if p.h24_pct > 100)
    stats.new_count   = sum(1 for p in pools if p.age_hours < NEW_HOURS)

    stats.heat_score = round(
        stats.turnover * (1 + stats.blast_count * 0.4) * (1 + stats.new_count * 0.15),
        2
    )
    return stats

def scan_chains(chain_list: list) -> list:
    results = []
    n = len(chain_list)
    for i, (slug, name, *rest) in enumerate(chain_list):
        is_new = bool(rest and rest[0])
        label  = f"🆕 {name}" if is_new else name
        print(f"  [{i+1}/{n}] {label} ...", end=" ", flush=True)
        s = fetch_chain(slug, name, is_new=is_new)
        if s.error:
            print(f"LỖI: {s.error}")
        else:
            print(f"ok · {s.meme_count} pool meme · turnover {s.turnover:.2f}× · điểm {s.heat_score}")
        results.append(s)
        if i < n - 1:
            time.sleep(GT_GAP)
    return results

# ─── Auto-discovery ────────────────────────────────────────────────────────────

def fetch_all_networks() -> list:
    """Trả về list (slug, name) từ GeckoTerminal (~150+ chain)."""
    networks = []
    for page in range(1, 8):  # tối đa 7 trang (~175 chain)
        data = _http_get_silent(f"{GT_BASE}/networks?page={page}")
        if not data:
            break
        items = data.get("data", [])
        if not items:
            break
        for item in items:
            slug = item.get("id", "")
            name = (item.get("attributes") or {}).get("name", slug)
            if slug:
                networks.append((slug, name))
        time.sleep(2)  # nhẹ nhàng, không cần GT_GAP đầy đủ
    return networks


def load_candidates(token: str) -> dict:
    """Đọc candidates.json từ gh-pages. Trả về {} nếu chưa có."""
    if not token:
        return {}
    owner, repo = GITHUB_REPO.split("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{CANDIDATES_FILE}?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "meme-signal-bot/2.0",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            content = base64.b64decode(resp["content"]).decode()
            return json.loads(content)
    except Exception:
        return {}


def save_candidates(candidates: dict, token: str, dry: bool = False) -> None:
    if not token or dry:
        if dry:
            promoted = [s for s, v in candidates.items() if v.get("seen", 0) >= PROMOTE_THRESHOLD]
            print(f"  [DRY] candidates.json: {len(candidates)} chain ứng viên, {len(promoted)} đã promote")
        return

    owner, repo = GITHUB_REPO.split("/")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{CANDIDATES_FILE}"
    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "meme-signal-bot/2.0",
    }

    sha = None
    try:
        req = urllib.request.Request(api_url + f"?ref={GITHUB_BRANCH}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            sha = json.loads(r.read()).get("sha")
    except Exception:
        pass

    content_b64 = base64.b64encode(json.dumps(candidates, ensure_ascii=False, indent=2).encode()).decode()
    payload = {
        "message": f"candidates: update {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(api_url, data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"  [OK] candidates.json đã lưu → {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  [ERR] candidates.json lỗi {e.code}: {e.read().decode()[:200]}")


def discover_new_chains(known_slugs: set, candidates: dict, token: str, dry: bool = False) -> list:
    """
    Khám phá chain mới từ GeckoTerminal.
    Cập nhật candidates dict in-place.
    Trả về list ChainStats của chain mới có đủ pool meme.
    """
    print(f"\nKhám phá chain mới (tối đa {MAX_DISCOVER} chain) ...")
    all_networks = fetch_all_networks()
    if not all_networks:
        print("  Không lấy được danh sách networks.")
        return []

    # Lọc chain chưa biết
    unknown = [(s, n) for s, n in all_networks if s not in known_slugs]
    print(f"  Tìm thấy {len(unknown)} chain chưa quét trong {len(all_networks)} networks.")

    if not unknown:
        return []

    # Ưu tiên: chain đã từng thấy (seen > 0) trước, rồi mới hoàn toàn
    def priority(item):
        slug = item[0]
        seen = candidates.get(slug, {}).get("seen", 0)
        return -seen  # âm để sort tăng dần = seen cao lên trước

    unknown.sort(key=priority)
    to_test = unknown[:MAX_DISCOVER]

    discovered = []
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")

    for i, (slug, name) in enumerate(to_test):
        print(f"  Thử [{i+1}/{len(to_test)}] {name} ({slug}) ...", end=" ", flush=True)
        s = fetch_chain(slug, name, is_new=True)

        if s.error:
            print(f"lỗi: {s.error}")
            time.sleep(GT_GAP)
            continue

        if s.meme_count < MIN_MEME_POOLS:
            print(f"bỏ qua ({s.meme_count} pool meme < {MIN_MEME_POOLS})")
            time.sleep(GT_GAP)
            continue

        print(f"ĐẠT · {s.meme_count} pool meme · điểm {s.heat_score}")

        # Cập nhật candidates
        entry = candidates.get(slug, {"name": name, "seen": 0, "first_seen": today})
        entry["seen"]       = entry.get("seen", 0) + 1
        entry["last_score"] = s.heat_score
        entry["last_seen"]  = today
        entry["name"]       = name
        candidates[slug]    = entry

        discovered.append(s)
        if i < len(to_test) - 1:
            time.sleep(GT_GAP)

    # Tăng seen cho chain đã biết trong candidates (để theo dõi trend)
    # (không cần làm gì thêm vì ta chỉ tăng khi gặp mới)

    promoted = [(s, v["name"]) for s, v in candidates.items()
                if v.get("seen", 0) >= PROMOTE_THRESHOLD and s not in known_slugs]
    if promoted:
        print(f"\n  ⭐ {len(promoted)} chain đã đủ điều kiện promote: "
              + ", ".join(n for _, n in promoted))

    return discovered

# ─── Formatters ────────────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    if v >= 1e9:  return f"${v/1e9:.1f}B"
    if v >= 1e6:  return f"${v/1e6:.1f}M"
    if v >= 1e3:  return f"${v/1e3:.0f}k"
    return f"${v:.0f}"

def fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"

def fmt_age(h: float) -> str:
    if h >= 9000: return "?"
    if h >= 24*7: return f"{h/24:.0f}d"
    if h >= 24:   return f"{h/24:.0f}d {h%24:.0f}h"
    return f"{h:.0f}h"

def heat_label(score: float) -> tuple:
    if score >= 15: return "rất nóng", "l-hot",  "f-hot",  "hot"
    if score >= 4:  return "nóng",     "l-warm",  "f-warm", "warm"
    if score >= 1:  return "ấm",       "l-cool",  "f-cool", "cool"
    if score > 0:   return "nguội",    "l-cold",  "f-cold", "cold"
    return "chết", "l-cold", "f-cold", "cold"

# ─── HTML generation ───────────────────────────────────────────────────────────

CSS = """
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Chivo:wght@500;600;700;900&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#f6f6f4;--surface:#ffffff;--surface-2:#eeeff1;--surface-3:#f9f9f8;
  --ink:#14161b;--muted:#5f6773;--line:#e0e2e7;--accent:#1f6feb;
  --cold:#5b8dc4;--cool:#4a9b8e;--warm:#d98f2b;--hot:#d1452f;
  --good:#16794a;--bad:#c33f2a;
}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0d0f13;--surface:#161920;--surface-2:#1e222a;--surface-3:#12151b;
  --ink:#e9ebef;--muted:#8b93a1;--line:#252932;--accent:#5aa2ff;
  --cold:#6f9fd4;--cool:#4fb3a2;--warm:#e8a743;--hot:#e8604a;
  --good:#4dc98d;--bad:#e8604a;
}}
:root[data-theme="dark"]{
  --bg:#0d0f13;--surface:#161920;--surface-2:#1e222a;--surface-3:#12151b;
  --ink:#e9ebef;--muted:#8b93a1;--line:#252932;--accent:#5aa2ff;
  --cold:#6f9fd4;--cool:#4fb3a2;--warm:#e8a743;--hot:#e8604a;
  --good:#4dc98d;--bad:#e8604a;
}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:"IBM Plex Sans",-apple-system,sans-serif;
  font-size:15px;line-height:1.6;padding:0 20px 80px}
.wrap{max-width:980px;margin:0 auto}
h1,h2,h3{font-family:"Chivo",-apple-system,sans-serif;text-wrap:balance;margin:0}
.mono,.n,td.n,th.n{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
header{padding:46px 0 28px;border-bottom:2px solid var(--ink)}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:11px;font-weight:500;
  letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin-bottom:13px}
h1{font-size:clamp(29px,5vw,45px);font-weight:900;letter-spacing:-.025em;line-height:1.03}
.sub{color:var(--muted);margin-top:12px;max-width:64ch}
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:7px;overflow:hidden;margin-top:28px}
.vc{background:var(--surface);padding:17px 19px}
.vc .k{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}
.vc .v{font-size:21px;font-weight:700;font-family:"Chivo",sans-serif;margin-top:5px;letter-spacing:-.01em}
.vc .h{font-size:12.5px;color:var(--muted);margin-top:4px;line-height:1.45}
section{margin-top:50px}
.shead{display:flex;align-items:baseline;gap:13px;padding-bottom:11px;
  border-bottom:1px solid var(--line);margin-bottom:20px}
.shead .idx{font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:600;color:var(--accent)}
.shead h2{font-size:21px;font-weight:700;letter-spacing:-.015em}
.shead .note{margin-left:auto;font-size:12px;color:var(--muted);font-family:"IBM Plex Mono",monospace}
.rank{display:flex;flex-direction:column;gap:2px}
.rk{display:grid;grid-template-columns:26px 160px 1fr 66px;align-items:center;gap:11px;
  padding:8px 12px;background:var(--surface);border:1px solid var(--line);border-radius:5px}
.rk.new-chain{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 5%,var(--surface))}
.rk .p{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);text-align:right}
.rk .nm{font-weight:600;font-size:14.5px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.rk .track{height:19px;background:var(--surface-2);border-radius:3px;overflow:hidden}
.rk .fill{height:100%;border-radius:3px}
.rk .sc{font-family:"IBM Plex Mono",monospace;font-size:13px;font-weight:600;text-align:right}
.f-hot{background:linear-gradient(90deg,var(--warm),var(--hot))}
.f-warm{background:linear-gradient(90deg,var(--cool),var(--warm))}
.f-cool{background:var(--cool)}
.f-cold{background:var(--cold);opacity:.55}
.lab{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.07em;
  text-transform:uppercase;padding:2px 7px;border-radius:3px;font-weight:600}
.l-hot{background:color-mix(in srgb,var(--hot) 16%,transparent);color:var(--hot)}
.l-warm{background:color-mix(in srgb,var(--warm) 18%,transparent);color:var(--warm)}
.l-cool{background:color-mix(in srgb,var(--cool) 16%,transparent);color:var(--cool)}
.l-cold{background:var(--surface-2);color:var(--muted)}
.l-new{background:color-mix(in srgb,var(--accent) 15%,transparent);color:var(--accent)}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
table{border-collapse:collapse;width:100%;min-width:720px;font-size:13px}
th{text-align:left;padding:10px 11px;font-family:"IBM Plex Mono",monospace;font-size:10px;
  letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:500;
  border-bottom:1px solid var(--line);background:var(--surface-2);white-space:nowrap}
th.n,td.n{text-align:right}
td{padding:9px 11px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
td.sym{font-weight:600;font-family:"IBM Plex Mono",monospace}
tr.hi{background:color-mix(in srgb,var(--hot) 7%,transparent)}
tr.dim td{color:var(--muted)}
tr.new-row{background:color-mix(in srgb,var(--accent) 5%,transparent)}
.pos{color:var(--good)}.neg{color:var(--bad)}
.movers{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.mv{border:1px solid var(--line);border-radius:7px;background:var(--surface);overflow:hidden}
.mv .h{padding:12px 16px;background:var(--surface-2);border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.mv .h .nm{font-family:"Chivo",sans-serif;font-weight:700;font-size:15px}
.mv .h .st{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted)}
.mv ul{list-style:none;margin:0;padding:6px 0}
.mv li{padding:7px 16px;display:grid;grid-template-columns:1fr auto;gap:8px;font-size:13px;
  border-bottom:1px solid var(--line)}
.mv li:last-child{border-bottom:none}
.mv .tk{font-family:"IBM Plex Mono",monospace;font-weight:500}
.mv .meta{font-size:11px;color:var(--muted);font-family:"IBM Plex Mono",monospace;grid-column:1/-1;margin-top:1px}
.mv .ch{font-family:"IBM Plex Mono",monospace;font-weight:600;font-variant-numeric:tabular-nums}
.callout{background:var(--surface-3);border:1px solid var(--line);border-left:3px solid var(--accent);
  padding:17px 20px;border-radius:0 6px 6px 0;margin:20px 0}
.callout.warn{border-left-color:var(--warm)}
.callout.bad{border-left-color:var(--hot)}
.callout h3{font-size:16px;font-weight:700;margin-bottom:7px;font-family:"Chivo",sans-serif}
.callout p{margin:0 0 9px;font-size:14.5px}.callout p:last-child{margin-bottom:0}
ul.plain{margin:12px 0;padding-left:19px}
ul.plain li{margin-bottom:8px}
ul.plain li::marker{color:var(--accent)}
footer{margin-top:56px;padding-top:19px;border-top:1px solid var(--line);
  font-size:12px;color:var(--muted);font-family:"IBM Plex Mono",monospace;line-height:1.7}
</style>
"""

def _pct_class(v: float) -> str:
    if v > 0: return "pos"
    if v < 0: return "neg"
    return ""

def _row_class(rank: int, s: ChainStats) -> str:
    if s.is_new: return "new-row"
    if rank <= 3 and s.heat_score >= 4: return "hi"
    if s.heat_score < 1: return "dim"
    return ""

def _verdict_cards(sorted_chains: list, num_chains: int) -> str:
    ok = [c for c in sorted_chains if not c.error and c.meme_count > 0]
    if not ok:
        return ""

    hottest   = ok[0]
    coldest   = min(ok, key=lambda c: c.heat_score)
    most_new  = max(ok, key=lambda c: c.new_count)

    negative_count = sum(1 for c in ok if c.median_h24 < 0)
    total_ok = len(ok)
    if negative_count > total_ok * 0.8:
        sentiment_v = "Sóng đang gãy"
        sentiment_h = f"{negative_count}/{total_ok} chain có trung vị h24 âm"
    elif negative_count > total_ok * 0.5:
        sentiment_v = "Thị trường yếu"
        sentiment_h = f"{negative_count}/{total_ok} chain có trung vị h24 âm"
    elif negative_count > total_ok * 0.3:
        sentiment_v = "Thị trường trung tính"
        sentiment_h = f"Phân hóa — {negative_count}/{total_ok} chain âm"
    else:
        sentiment_v = "Sóng đang hình thành"
        sentiment_h = f"Chỉ {negative_count}/{total_ok} chain có trung vị âm"

    return f"""
  <div class="verdict">
    <div class="vc"><div class="k">Nóng nhất</div>
      <div class="v" style="color:var(--hot)">{hottest.name}</div>
      <div class="h">{fmt_usd(hottest.vol_24h)} volume trên {fmt_usd(hottest.liq_usd)} thanh khoản — vòng quay {hottest.turnover:.1f} lần</div>
    </div>
    <div class="vc"><div class="k">Nhiều token mới nhất</div>
      <div class="v" style="color:var(--warm)">{most_new.name}</div>
      <div class="h">{most_new.new_count}/{most_new.meme_count} pool dưới 48h, {most_new.blast_count} token vượt +100%</div>
    </div>
    <div class="vc"><div class="k">Đang lạnh</div>
      <div class="v" style="color:var(--cold)">{coldest.name}</div>
      <div class="h">trung vị {fmt_pct(coldest.median_h24)}, {coldest.blast_count} token vượt +100%, điểm nhiệt {coldest.heat_score}</div>
    </div>
    <div class="vc"><div class="k">Bối cảnh chung</div>
      <div class="v">{sentiment_v}</div>
      <div class="h">{sentiment_h}</div>
    </div>
  </div>"""

def _ranking_section(sorted_chains: list) -> str:
    ok = [c for c in sorted_chains if not c.error]
    if not ok:
        return "<p>Không có dữ liệu.</p>"

    max_score = max((c.heat_score for c in ok), default=1) or 1
    rows = []
    for i, c in enumerate(ok, 1):
        lbl, lcls, fcls, _ = heat_label(c.heat_score)
        width = f"{c.heat_score / max_score * 100:.1f}%"
        new_badge = ' <span class="lab l-new">mới</span>' if c.is_new else ""
        rk_cls = " new-chain" if c.is_new else ""
        rows.append(
            f'    <div class="rk{rk_cls}">'
            f'<span class="p">{i}</span>'
            f'<span class="nm">{c.name}{new_badge} <span class="lab {lcls}">{lbl}</span></span>'
            f'<span class="track"><span class="fill {fcls}" style="width:{width}"></span></span>'
            f'<span class="sc">{c.heat_score}</span>'
            f'</div>'
        )
    return '<div class="rank">\n' + "\n".join(rows) + "\n  </div>"

def _table_section(sorted_chains: list) -> str:
    rows = []
    for i, c in enumerate(sorted_chains):
        if c.error:
            rows.append(
                f'<tr class="dim"><td class="sym">{c.name}</td>'
                f'<td class="n" colspan="8" style="color:var(--bad)">lỗi API</td></tr>'
            )
            continue

        rcls      = _row_class(i + 1, c)
        pool_str  = f"{c.meme_count}/{c.raw_count}"
        h24c      = _pct_class(c.median_h24)
        new_badge = " 🆕" if c.is_new else ""
        rows.append(
            f'<tr class="{rcls}"><td class="sym">{c.name}{new_badge}</td>'
            f'<td class="n">{pool_str}</td>'
            f'<td class="n">{fmt_usd(c.liq_usd)}</td>'
            f'<td class="n">{fmt_usd(c.vol_24h)}</td>'
            f'<td class="n">{fmt_usd(c.median_lp)}</td>'
            f'<td class="n">{c.turnover:.2f}×</td>'
            f'<td class="n {h24c}">{fmt_pct(c.median_h24)}</td>'
            f'<td class="n">{c.blast_count}</td>'
            f'<td class="n">{c.new_count}</td>'
            f'</tr>'
        )

    return """  <div class="tw">
    <table>
      <thead><tr>
        <th>Chain</th><th class="n">Pool meme</th><th class="n">Thanh khoản</th>
        <th class="n">Volume 24h</th><th class="n">LP trung vị</th>
        <th class="n">Vòng quay</th><th class="n">h24 trung vị</th>
        <th class="n">&gt;100%</th><th class="n">&lt;48h</th>
      </tr></thead>
      <tbody>
""" + "\n".join(rows) + """
      </tbody>
    </table>
  </div>
  <p style="font-size:13px;color:var(--muted);margin-top:11px">
    <strong>Vòng quay</strong> = volume 24h chia thanh khoản. 🆕 = chain mới được khám phá lần này.
  </p>"""

def _movers_card(c: ChainStats) -> str:
    lbl, lcls, _, _ = heat_label(c.heat_score)
    subtitle = (f"{c.turnover:.1f}× vòng quay"
                if c.heat_score >= 1 else
                f"{c.new_count} pool mới" if c.new_count else "không có sóng")

    top_pools = sorted(c.pools, key=lambda p: p.h24_pct, reverse=True)[:4]
    items = []
    for p in top_pools:
        clr = ("style=\"color:var(--hot)\"" if p.h24_pct > 200 else
               "style=\"color:var(--warm)\"" if p.h24_pct > 50 else
               f"style=\"color:var(--bad)\"" if p.h24_pct < -10 else "")
        items.append(
            f'    <li><span class="tk">{p.symbol}</span>'
            f'<span class="ch" {clr}>{fmt_pct(p.h24_pct)}</span>'
            f'<span class="meta">{fmt_age(p.age_hours)} tuổi · LP {fmt_usd(p.lp_usd)} '
            f'· vol {fmt_usd(p.vol_24h)} · h1 {fmt_pct(p.h1_pct)}</span></li>'
        )

    items_html = "\n".join(items) if items else "    <li><span>Không có pool meme</span></li>"
    new_badge = ' <span class="lab l-new">mới khám phá</span>' if c.is_new else ""
    return f"""  <div class="mv">
    <div class="h">
      <span class="nm">{c.name}{new_badge}</span>
      <span class="lab {lcls}">{subtitle}</span>
      <span class="st">{fmt_usd(c.vol_24h)}</span>
    </div>
    <ul>
{items_html}
    </ul>
  </div>"""

def _movers_section(sorted_chains: list) -> str:
    ok = [c for c in sorted_chains if not c.error and c.meme_count > 0]
    if not ok:
        return "<p>Không có dữ liệu.</p>"

    featured = ok[:3]
    base_chain = next((c for c in sorted_chains if c.slug == "base"), None)
    if base_chain and base_chain not in featured:
        featured.append(base_chain)

    cards = [_movers_card(c) for c in featured]
    return '  <div class="movers">\n' + "\n".join(cards) + "\n  </div>"

def _analysis_section(sorted_chains: list) -> str:
    ok = [c for c in sorted_chains if not c.error and c.meme_count > 0]
    if not ok:
        return ""

    parts = []
    negative_count = sum(1 for c in ok if c.median_h24 < 0)
    total_ok = len(ok)
    hottest  = ok[0]
    coldest  = min(ok, key=lambda c: c.heat_score)

    if negative_count > total_ok * 0.7:
        parts.append(f"""  <div class="callout bad">
    <h3>Toàn thị trường meme đang trong giai đoạn giảm</h3>
    <p>{negative_count}/{total_ok} chain có trung vị h24 âm. Hầu hết token trending thực ra đang giảm — chỉ vài token bùng nổ kéo con số trung bình lên. Đây là đặc trưng của <strong>cuối sóng hoặc thị trường yếu</strong>.</p>
  </div>""")
    elif negative_count > total_ok * 0.4:
        parts.append(f"""  <div class="callout warn">
    <h3>Thị trường phân hóa — {negative_count}/{total_ok} chain âm</h3>
    <p>Không phải xu hướng rõ ràng. Chọn lọc chain và thời điểm quan trọng hơn bình thường.</p>
  </div>""")
    else:
        parts.append(f"""  <div class="callout">
    <h3>Thị trường đang phục hồi — chỉ {negative_count}/{total_ok} chain âm</h3>
    <p>Tín hiệu tích cực. Các chain nóng có thể tạo thêm cơ hội trong 12h tới.</p>
  </div>""")

    top_by_blast = sorted(hottest.pools, key=lambda p: p.h24_pct, reverse=True)[:3]
    blast_names  = ", ".join(p.symbol for p in top_by_blast)
    parts.append(f"""  <div class="callout">
    <h3>{hottest.name} dẫn đầu với điểm {hottest.heat_score}</h3>
    <p>Vòng quay {hottest.turnover:.1f}× trên {fmt_usd(hottest.liq_usd)} thanh khoản, {hottest.blast_count} token vượt +100%, {hottest.new_count} pool dưới 48h{"." if not blast_names else f" — token bùng nổ: {blast_names}."}</p>
  </div>""")

    if coldest.slug != hottest.slug:
        parts.append(f"""  <div class="callout warn">
    <h3>{coldest.name} đang lạnh nhất ({coldest.heat_score} điểm)</h3>
    <p>Trung vị h24 {fmt_pct(coldest.median_h24)}, {coldest.blast_count} token vượt +100%, vòng quay {coldest.turnover:.2f}×.</p>
  </div>""")

    rh = next((c for c in ok if c.slug == "robinhood"), None)
    ba = next((c for c in ok if c.slug == "base"),      None)
    if rh and ba:
        parts.append(f"""  <div class="callout">
    <h3>Robinhood vs Base — hai hướng khác nhau</h3>
    <p>Robinhood: {rh.new_count} pool mới trong 48h, LP trung vị {fmt_usd(rh.median_lp)}.
    Base: LP trung vị {fmt_usd(ba.median_lp)}, {ba.blast_count} token +100%.</p>
  </div>""")

    # Highlight newly discovered chains
    new_chains = [c for c in ok if c.is_new]
    if new_chains:
        nc_list = "".join(
            f"<li><strong>{c.name}</strong> — {c.meme_count} pool meme, điểm {c.heat_score}, "
            f"vòng quay {c.turnover:.2f}×</li>"
            for c in new_chains
        )
        parts.append(f"""  <div class="callout">
    <h3>🆕 Chain mới được khám phá lần này</h3>
    <ul class="plain">{nc_list}</ul>
    <p>Chain mới xuất hiện đủ {PROMOTE_THRESHOLD} lần sẽ tự động được thêm vào danh sách cố định.</p>
  </div>""")

    return "\n".join(parts)


def generate_html(sorted_chains: list, scan_dt: datetime.datetime, candidates: dict) -> str:
    dt_str    = scan_dt.strftime("%d-%m-%Y %H:%M UTC")
    num_total = len([c for c in sorted_chains if not c.error])
    num_new   = len([c for c in sorted_chains if c.is_new and not c.error])
    chain_label = f"{num_total} chain" + (f" (+{num_new} mới)" if num_new else "")

    # Promoted chains waiting to be added
    promoted_slugs = {s for s, v in candidates.items()
                      if v.get("seen", 0) >= PROMOTE_THRESHOLD}

    lines = [
        "<!doctype html><html lang='vi'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Bản Đồ Nhiệt Meme · {chain_label}</title>",
        CSS,
        "</head><body>",
        "<div class='wrap'>",

        "<header>",
        f"  <div class='eyebrow'>GeckoTerminal · {chain_label} · {dt_str}</div>",
        "  <h1>Bản Đồ Nhiệt Meme</h1>",
        "  <p class='sub'>Quét top 20 pool trending mỗi mạng lưới, lọc bỏ cặp stablecoin và wrapped, xếp hạng theo dòng tiền thực chảy qua pool meme. Chain mới tự động được khám phá mỗi lần quét.</p>",
        _verdict_cards(sorted_chains, num_total),
        "</header>",

        "<section>",
        "  <div class='shead'><span class='idx'>01</span><h2>Xếp hạng độ nóng</h2>"
        "<span class='note'>điểm = vòng quay × token bùng nổ × pool mới</span></div>",
        _ranking_section(sorted_chains),
        "</section>",

        "<section>",
        "  <div class='shead'><span class='idx'>02</span><h2>Số liệu đầy đủ</h2>"
        "<span class='note'>chỉ tính pool meme, đã lọc cặp hạ tầng</span></div>",
        _table_section(sorted_chains),
        "</section>",

        "<section>",
        "  <div class='shead'><span class='idx'>03</span><h2>Token đang kéo sóng</h2>"
        "<span class='note'>3 chain nóng nhất + Base để đối chiếu</span></div>",
        _movers_section(sorted_chains),
        "</section>",

        "<section>",
        "  <div class='shead'><span class='idx'>04</span><h2>Đọc bảng này thế nào</h2></div>",
        _analysis_section(sorted_chains),
        "</section>",

        f"""<footer>
  Nguồn: GeckoTerminal API v2, endpoint trending_pools, top 20 pool mỗi mạng lưới · chụp {dt_str}<br>
  Bộ lọc cặp hạ tầng loại stablecoin, wrapped native và bluechip · Vòng quay = volume 24h ÷ thanh khoản<br>
  Tự động cập nhật 2 lần/ngày lúc 7:00 và 19:00 (UTC+7) · Chain ứng viên trong candidates.json: {len(candidates)}
</footer>""",

        "</div></body></html>",
    ]
    return "\n".join(lines)

# ─── GitHub push ───────────────────────────────────────────────────────────────

def _github_put(api_url: str, token: str, filename: str,
                content_b64: str, sha: Optional[str], message: str, dry: bool) -> bool:
    if not token:
        print(f"  [WARN] GITHUB_TOKEN không có — bỏ qua {filename}")
        return False

    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "meme-signal-bot/2.0",
    }
    payload = {"message": message, "content": content_b64, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha

    if dry:
        print(f"  [DRY] Sẽ push {filename}")
        return True

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(api_url, data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"  [OK] {filename} → {r.status}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  [ERR] {filename} lỗi {e.code}: {e.read().decode()[:200]}")
        return False

def _github_get_sha(api_url: str, token: str) -> Optional[str]:
    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github+json",
        "User-Agent":    "meme-signal-bot/2.0",
    }
    try:
        req = urllib.request.Request(api_url + f"?ref={GITHUB_BRANCH}", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("sha")
    except Exception:
        return None

def push_github(html: str, dry: bool = False) -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("  [WARN] GITHUB_TOKEN không có — bỏ qua push GitHub")
        return ""

    owner, repo = GITHUB_REPO.split("/")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{GITHUB_FILE}"
    sha     = _github_get_sha(api_url, token)

    content_b64 = base64.b64encode(html.encode()).decode()
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    ok = _github_put(api_url, token, GITHUB_FILE, content_b64, sha,
                     f"heatmap: auto update {ts}", dry)

    if ok:
        return f"https://{owner}.github.io/{repo}/{GITHUB_FILE}"
    return ""

# ─── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(sorted_chains: list, page_url: str, new_chains: list,
                  candidates: dict, dry: bool = False):
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("  [WARN] TG token/chat_id thiếu — bỏ qua Telegram")
        return

    now_vn = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    ts = now_vn.strftime("%d-%m %H:%M")

    ok = [c for c in sorted_chains if not c.error and c.meme_count > 0]
    if not ok:
        return

    negative_count = sum(1 for c in ok if c.median_h24 < 0)
    neg_pct = int(negative_count / len(ok) * 100)
    num_chains = len([c for c in sorted_chains if not c.error])

    emoji_map = {"rất nóng": "🔥", "nóng": "🔶", "ấm": "🟡", "nguội": "🔵", "chết": "⚫"}

    lines = [
        f"🌡️ <b>Bản đồ nhiệt meme · {num_chains} chain · {ts}</b>",
        "",
    ]

    for i, c in enumerate(ok[:8], 1):
        lbl = heat_label(c.heat_score)[0]
        em  = emoji_map.get(lbl, "")
        blast_note = f" · {c.blast_count}🚀" if c.blast_count else ""
        new_note   = f" · {c.new_count}🆕" if c.new_count else ""
        new_tag    = " <i>(mới)</i>" if c.is_new else ""
        lines.append(
            f"{i}. {em} <b>{c.name}</b>{new_tag} — {c.heat_score} điểm "
            f"({c.turnover:.1f}× vòng quay{blast_note}{new_note})"
        )

    if len(ok) > 8:
        rest_names = ", ".join(c.name for c in ok[8:])
        lines.append(f"... nguội: {rest_names}")

    lines += [
        "",
        f"📉 Bối cảnh: {negative_count}/{len(ok)} chain trung vị âm ({neg_pct}%)",
    ]

    if new_chains:
        nc_names = ", ".join(c.name for c in new_chains)
        lines += ["", f"🔍 Khám phá mới: {nc_names}"]

    # Promoted chains
    promoted = [(s, v["name"]) for s, v in candidates.items()
                if v.get("seen", 0) >= PROMOTE_THRESHOLD]
    if promoted:
        pr_names = ", ".join(n for _, n in promoted)
        lines += ["", f"⭐ Sắp tự động thêm: {pr_names}"]

    if page_url:
        lines += ["", f"🌐 <a href='{page_url}'>Xem chi tiết</a>"]

    msg = "\n".join(lines)

    if dry:
        print(f"  [DRY] Telegram message ({len(msg)} ký tự):\n{msg}")
        return

    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id":    chat_id,
        "text":       msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [OK] Telegram gửi thành công")
    except Exception as e:
        print(f"  [ERR] Telegram: {e}")

# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Bản đồ nhiệt meme đa chain")
    ap.add_argument("--dry", action="store_true",
                    help="quét nhưng không push GitHub / không gửi Telegram")
    ap.add_argument("--no-discover", action="store_true",
                    help="bỏ qua bước khám phá chain mới")
    args = ap.parse_args()

    load_dotenv(".env")
    load_dotenv(os.path.expanduser("~/.env"))

    token = os.getenv("GITHUB_TOKEN", "")

    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== meme_signal_bot.py === {now_str}")

    # Load candidates từ gh-pages
    print("Đọc candidates.json từ gh-pages ...")
    candidates = load_candidates(token)
    if args.dry:
        print(f"  [DRY MODE] candidates: {len(candidates)} entries")

    # Build danh sách chain: cố định + auto-promoted
    known_slugs = {slug for slug, _ in CHAINS}
    promoted_chains = [
        (slug, v["name"])
        for slug, v in candidates.items()
        if v.get("seen", 0) >= PROMOTE_THRESHOLD and slug not in known_slugs
    ]

    active_chains = list(CHAINS) + [(s, n, False) for s, n in promoted_chains]
    if promoted_chains:
        print(f"  + {len(promoted_chains)} chain đã promote: "
              + ", ".join(n for _, n in promoted_chains))

    print(f"\nQuét {len(active_chains)} chain, giãn cách {GT_GAP}s mỗi lượt")
    if args.dry:
        print("  [DRY MODE] Sẽ không push GitHub hoặc gửi Telegram")
    print()

    # Scan các chain đã biết
    chain_input = [(s, n) + ((is_new,) if len(t := (s, n, False)) > 2 else ())
                   for t in active_chains for s, n, *rest in [t]]
    # Đơn giản hơn:
    scan_input = []
    for entry in active_chains:
        if len(entry) == 3:
            scan_input.append(entry)  # (slug, name, is_new_flag)
        else:
            scan_input.append((entry[0], entry[1], False))

    results = scan_chains(scan_input)

    # Khám phá chain mới
    new_discovered = []
    if not args.no_discover:
        all_known = known_slugs | {slug for slug, v in candidates.items()
                                   if v.get("seen", 0) >= PROMOTE_THRESHOLD}
        new_discovered = discover_new_chains(all_known, candidates, token, dry=args.dry)
        results.extend(new_discovered)

    # Sort by heat_score
    sorted_chains = sorted(
        results,
        key=lambda c: (0 if c.error else 1, c.heat_score),
        reverse=True
    )

    scan_dt = datetime.datetime.utcnow()
    html    = generate_html(sorted_chains, scan_dt, candidates)
    print(f"\nHTML tạo xong: {len(html):,} bytes")

    print("\nĐẩy lên GitHub Pages ...")
    page_url = push_github(html, dry=args.dry)

    print("\nLưu candidates.json ...")
    save_candidates(candidates, token, dry=args.dry)

    print("\nGửi Telegram ...")
    send_telegram(sorted_chains, page_url, new_discovered, candidates, dry=args.dry)

    print("\nXong.")

if __name__ == "__main__":
    main()
