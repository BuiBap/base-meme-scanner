#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runner.py — Vòng chạy 24/7 cho base-meme-scanner.

VÌ SAO KHÔNG DÙNG `--loop` CÓ SẴN
    `--loop` chỉ lặp lại run_once() với một khoảng nghỉ cố định. Chạy thật
    24/7 cần nhiều hơn thế:

      1. HAI NHỊP KHÁC NHAU. Theo dõi vị thế ảo rất rẻ (1 call/30 pool) nhưng
         cần dày để TP/SL khớp sát ngưỡng. Quét token mới thì đắt và không cần
         dày. Gộp chung một nhịp là ép cả hai vào tần suất sai.
      2. LỖI KHÔNG ĐƯỢC LÀM CHẾT TIẾN TRÌNH. API sập 10 phút là chuyện thường;
         bot phải lùi dần rồi tự quay lại, không phải thoát.
      3. TẮT ÊM. Nhận SIGTERM (systemd restart, reboot) phải lưu state xong
         mới thoát, nếu không mất vị thế đang mở.
      4. BÁO KHI CHẾT. Bot im lặng vì hỏng và bot im lặng vì không có tín hiệu
         nhìn giống hệt nhau trên Telegram. Heartbeat để phân biệt.

CÁC NHỊP
    fast   (45s)  : cập nhật giá vị thế ảo, chốt lệnh chạm ngưỡng
    slow   (150s) : quét pool mới, chấm điểm, gửi cảnh báo
    report (6h)   : heartbeat + tình trạng ngân sách API
    backup (1h)   : sinh dashboard.html, đẩy state lên GitHub

CHẠY
    python runner.py                       # dùng mặc định
    python runner.py --fast 30 --slow 120  # chỉnh nhịp
    python runner.py --dry-run             # in lịch chạy rồi thoát
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone

import base_meme_bot as B

try:
    import paper_trader as P
except ImportError:
    P = None

try:
    import rate_limit
except ImportError:
    rate_limit = None

try:
    import dashboard
except ImportError:
    dashboard = None

try:
    import state_backup
except ImportError:
    state_backup = None


# =========================================================================== #
#  Cấu hình vòng chạy
# =========================================================================== #

@dataclass
class RunnerConfig:
    # Nhịp (giây). Mặc định chọn sao cho tổng tiêu thụ nằm gọn trong
    # 25 call/phút của GeckoTerminal — xem estimate_budget() bên dưới.
    fast_interval: float = 45.0        # theo dõi vị thế ảo
    slow_interval: float = 150.0       # quét token mới
    heartbeat_interval: float = 6 * 3600.0
    backup_interval: float = 3600.0

    # Chống lỗi
    max_consecutive_errors: int = 10   # quá số này -> thoát để systemd restart sạch
    backoff_base: float = 5.0          # giây, nhân đôi mỗi lần lỗi liên tiếp
    backoff_max: float = 300.0

    # Vận hành
    alert_on_error_after: int = 3      # báo Telegram sau N lỗi liên tiếp
    tick: float = 1.0                  # độ phân giải vòng lặp
    log_budget: bool = True


def _now() -> float:
    return time.time()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def log(msg: str):
    print(f"[{_stamp()}] {msg}", flush=True)


# =========================================================================== #
#  Runner
# =========================================================================== #

class Runner:
    def __init__(self, cfg: B.Config, rcfg: RunnerConfig,
                 paper_cfg=None):
        self.cfg = cfg
        self.rcfg = rcfg
        self.scanner = B.Scanner(cfg, paper_cfg)
        self.stop = False
        # Cho tầng HTTP biết khi nào đang tắt, để nó bỏ dở chuỗi retry thay vì
        # bắt shutdown chờ hết backoff (có thể tới 30s+ mỗi call).
        self.scanner.http.should_stop = lambda: self.stop
        self.errors = 0            # lỗi liên tiếp
        self.error_alerted = False
        self.started_at = _now()
        self.counts = {"fast": 0, "slow": 0, "closed": 0, "signals": 0, "errors": 0}

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    # ---------------------- vòng đời ---------------------- #

    def _on_signal(self, signum, frame):
        name = signal.Signals(signum).name
        log(f"nhận {name} -> đang tắt êm, sẽ lưu state trước khi thoát")
        self.stop = True

    def _shutdown(self):
        """Lưu mọi thứ trước khi thoát. Mất vị thế đang mở = mất dữ liệu hiệu suất."""
        try:
            if self.scanner.paper is not None:
                self.scanner.paper._save_positions()
                self.scanner.paper._save_state()
            self.scanner._save_seen()
            self.scanner._save_heartbeat()
            log("đã lưu state")
        except Exception as e:
            log(f"LỖI khi lưu state lúc tắt: {e}")

        up = (_now() - self.started_at) / 3600.0
        log(f"tổng kết phiên: {self.counts['slow']} lượt quét · "
            f"{self.counts['fast']} lượt theo dõi · {self.counts['signals']} tín hiệu · "
            f"{self.counts['closed']} lệnh đóng · {self.counts['errors']} lỗi · "
            f"chạy {up:.1f}h")
        if rate_limit is not None and self.rcfg.log_budget:
            log("ngân sách API: " + rate_limit.GLOBAL.report())

    # ---------------------- xử lý lỗi ---------------------- #

    def _guard(self, name: str, fn, *args, **kwargs):
        """Chạy một nhịp, nuốt lỗi, đếm và lùi dần. Trả (ok, kết quả)."""
        try:
            out = fn(*args, **kwargs)
            if self.errors:
                log(f"{name}: đã hồi phục sau {self.errors} lỗi liên tiếp")
                if self.error_alerted:
                    self._notify(f"✅ Bot đã hồi phục sau {self.errors} lỗi liên tiếp.")
                self.errors = 0
                self.error_alerted = False
            return True, out
        except Exception as e:
            self.errors += 1
            self.counts["errors"] += 1
            log(f"LỖI trong nhịp {name} (liên tiếp {self.errors}): {e!r}")
            traceback.print_exc()

            if (self.errors >= self.rcfg.alert_on_error_after
                    and not self.error_alerted):
                self._notify(f"⚠️ Bot gặp {self.errors} lỗi liên tiếp ở nhịp <b>{name}</b>:\n"
                             f"<code>{str(e)[:300]}</code>\nVẫn đang thử lại.")
                self.error_alerted = True

            backoff = min(self.rcfg.backoff_base * (2 ** (self.errors - 1)),
                          self.rcfg.backoff_max)
            log(f"nghỉ {backoff:.0f}s rồi thử lại")
            self._sleep(backoff)
            return False, None

    def _notify(self, text: str):
        try:
            if self.scanner.tg and self.scanner.tg.enabled:
                self.scanner.tg.send(text)
        except Exception:
            pass

    def _sleep(self, seconds: float):
        """Ngủ nhưng vẫn phản ứng ngay với tín hiệu tắt."""
        end = _now() + seconds
        while _now() < end and not self.stop:
            time.sleep(min(self.rcfg.tick, max(0.0, end - _now())))

    # ---------------------- các nhịp ---------------------- #

    def do_fast(self):
        closed = self.scanner.track_positions()
        self.counts["fast"] += 1
        self.counts["closed"] += len(closed)

    def do_slow(self):
        hits = self.scanner.run_once(track=False)   # nhịp fast đã lo theo dõi
        self.counts["slow"] += 1
        self.counts["signals"] += len(hits)

    def do_heartbeat(self):
        up = (_now() - self.started_at) / 3600.0
        parts = [
            f"💚 Bot còn sống · chạy {up:.1f}h",
            f"{self.counts['slow']} lượt quét · {self.counts['signals']} tín hiệu "
            f"· {self.counts['closed']} lệnh đóng",
        ]
        if self.scanner.paper is not None:
            u = self.scanner.paper.unrealized()
            parts.append(f"đang mở {u['open_count']} vị thế · tạm tính {u['pnl_pct']:+.1f}%")
        if self.counts["errors"]:
            parts.append(f"⚠️ {self.counts['errors']} lỗi từ lúc khởi động")
        self._notify("\n".join(parts))
        if rate_limit is not None and self.rcfg.log_budget:
            log("ngân sách API: " + rate_limit.GLOBAL.report())

    def do_backup(self):
        made = []
        if dashboard is not None and self.scanner.paper is not None:
            path = dashboard.build(self.scanner.paper.cfg)
            if path:
                made.append(os.path.basename(path))
        if state_backup is not None:
            pushed = state_backup.push()
            if pushed:
                made.append("đã đẩy lên git")
        if made:
            log("backup: " + " · ".join(made))

    # ---------------------- vòng lặp chính ---------------------- #

    def estimate_budget(self) -> dict:
        """Ước tính tiêu thụ GeckoTerminal mỗi phút để cảnh báo nếu đặt nhịp quá dày.

        Cố ý ước tính DƯ, vì đụng trần thì mất nhịp theo dõi vị thế — thứ đắt
        giá nhất trong hệ thống này.
        """
        per_min_fast = 60.0 / max(self.rcfg.fast_interval, 1)      # ~1 call/lượt
        # mỗi lượt quét: 2 trang new_pools + 1 trending + ~4 call trades cho shortlist
        per_slow = self.cfg.discovery_pages + 1 + 4
        per_min_slow = (60.0 / max(self.rcfg.slow_interval, 1)) * per_slow
        total = per_min_fast + per_min_slow
        cap = 25.0
        if rate_limit is not None:
            cap = rate_limit.RateLimiterRegistry.DEFAULTS["api.geckoterminal.com"][0]
        return {"fast": per_min_fast, "slow": per_min_slow,
                "total": total, "cap": cap, "ok": total <= cap}

    def run(self):
        b = self.estimate_budget()
        log(f"khởi động · nhịp nhanh {self.rcfg.fast_interval:.0f}s · "
            f"nhịp quét {self.rcfg.slow_interval:.0f}s")
        log(f"ước tính dùng ~{b['total']:.1f} call/phút GeckoTerminal "
            f"(trần đặt {b['cap']:.0f})")
        if not b["ok"]:
            log("⚠️ CẢNH BÁO: nhịp đang đặt quá dày so với ngân sách API. "
                "Bot sẽ tự chờ và các nhịp sẽ bị trễ. Nên tăng --fast hoặc --slow.")

        self._notify(f"🚀 Bot khởi động lúc {_stamp()} · nhịp {self.rcfg.fast_interval:.0f}s"
                     f"/{self.rcfg.slow_interval:.0f}s")

        now = _now()
        next_fast = now
        # Lệch nhẹ để hai nhịp không bắn cùng lúc ngay từ đầu, nhưng không lệch
        # quá 1/10 chu kỳ — nếu không, với nhịp ngắn thì nhịp chậm bị hoãn oan.
        next_slow = now + min(2.0, self.rcfg.slow_interval / 10.0)
        next_hb = now + self.rcfg.heartbeat_interval
        next_backup = now + self.rcfg.backup_interval

        while not self.stop:
            now = _now()

            if now >= next_fast:
                self._guard("theo dõi vị thế", self.do_fast)
                next_fast = _now() + self.rcfg.fast_interval

            if self.stop:
                break

            if now >= next_slow:
                self._guard("quét token mới", self.do_slow)
                next_slow = _now() + self.rcfg.slow_interval

            if now >= next_hb:
                self._guard("heartbeat", self.do_heartbeat)
                next_hb = _now() + self.rcfg.heartbeat_interval

            if now >= next_backup:
                self._guard("backup", self.do_backup)
                next_backup = _now() + self.rcfg.backup_interval

            if self.errors >= self.rcfg.max_consecutive_errors:
                log(f"quá {self.rcfg.max_consecutive_errors} lỗi liên tiếp -> thoát "
                    f"để systemd khởi động lại sạch sẽ")
                self._notify(f"🔴 Bot thoát sau {self.errors} lỗi liên tiếp. "
                             f"systemd sẽ khởi động lại.")
                self._shutdown()
                sys.exit(1)

            time.sleep(self.rcfg.tick)

        self._shutdown()


# =========================================================================== #

def main():
    ap = argparse.ArgumentParser(description="Chạy base-meme-scanner 24/7")
    ap.add_argument("--fast", type=float, help="giây giữa 2 lần theo dõi vị thế (mặc định 45)")
    ap.add_argument("--slow", type=float, help="giây giữa 2 lần quét token mới (mặc định 150)")
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--pages", type=int)
    ap.add_argument("--paper-size", type=float)
    ap.add_argument("--no-paper", action="store_true")
    ap.add_argument("--heartbeat-hours", type=float, default=6.0)
    ap.add_argument("--backup-minutes", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="in cấu hình + ước tính ngân sách API rồi thoát")
    args = ap.parse_args()

    cfg = B.Config()
    if args.min_score is not None:
        cfg.min_score_to_alert = args.min_score
    if args.pages:
        cfg.discovery_pages = args.pages

    paper_cfg = None
    if P is not None:
        paper_cfg = P.PaperConfig()
        if args.no_paper:
            paper_cfg.enabled = False
        if args.paper_size:
            paper_cfg.position_size_usd = args.paper_size

    rcfg = RunnerConfig()
    if args.fast:
        rcfg.fast_interval = args.fast
    if args.slow:
        rcfg.slow_interval = args.slow
    rcfg.heartbeat_interval = args.heartbeat_hours * 3600.0
    rcfg.backup_interval = args.backup_minutes * 60.0

    runner = Runner(cfg, rcfg, paper_cfg)

    if args.dry_run:
        b = runner.estimate_budget()
        print(f"nhịp nhanh    : {rcfg.fast_interval:.0f}s  -> {b['fast']:.1f} call/phút")
        print(f"nhịp quét     : {rcfg.slow_interval:.0f}s  -> {b['slow']:.1f} call/phút")
        print(f"tổng ước tính : {b['total']:.1f} call/phút (trần {b['cap']:.0f})")
        print(f"kết luận      : {'OK ✅' if b['ok'] else 'QUÁ DÀY ❌ - nên tăng nhịp'}")
        print(f"min_score     : {cfg.min_score_to_alert}")
        print(f"paper trading : {'bật' if paper_cfg and paper_cfg.enabled else 'tắt'}")
        return

    runner.run()


if __name__ == "__main__":
    main()
