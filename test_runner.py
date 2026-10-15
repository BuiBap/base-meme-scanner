#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_runner.py — Kiểm thử rate limiter, runner 2 nhịp, dashboard, backup.

Chạy:  python test_runner.py
Không gọi mạng. Không đụng vào state thật (dùng thư mục tạm).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rate_limit as RL
import paper_trader as P
import dashboard as D
import state_backup as SB

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail else ""))


# =========================================================================== #
#  Token bucket
# =========================================================================== #

def test_bucket_burst():
    print("\n[1] Token bucket: cho bắn dồn rồi mới chặn")
    b = RL.TokenBucket(capacity=5, refill_per_sec=10.0, name="t")
    t0 = time.monotonic()
    for _ in range(5):
        b.acquire()
    burst = time.monotonic() - t0
    check("5 call đầu gần như tức thì", burst < 0.05, f"{burst*1000:.1f}ms")

    t1 = time.monotonic()
    b.acquire()                       # phải chờ ~0.1s để có token thứ 6
    waited = time.monotonic() - t1
    check("call thứ 6 phải chờ ~0.1s", 0.05 < waited < 0.35, f"{waited*1000:.0f}ms")
    check("thống kê ghi nhận lần chờ", b.stats.throttle_events == 1)


def test_bucket_rate():
    print("\n[2] Token bucket: tốc độ trung bình đúng trần")
    # capacity 2, refill 20/s -> 10 call sau khi cạn phải mất ~0.4s
    b = RL.TokenBucket(capacity=2, refill_per_sec=20.0, name="t")
    b.acquire(); b.acquire()          # làm cạn
    t0 = time.monotonic()
    for _ in range(8):
        b.acquire()
    dt = time.monotonic() - t0
    check("8 call ở 20/s mất ~0.4s", 0.32 < dt < 0.60, f"{dt:.3f}s")


def test_bucket_penalty():
    print("\n[3] Phanh khẩn cấp khi gặp 429")
    b = RL.TokenBucket(capacity=10, refill_per_sec=100.0, name="t")
    check("còn token trước khi phạt", b.try_acquire() is True)
    b.penalize(cooldown_seconds=0.3)
    check("bị phạt -> không lấy được token ngay", b.try_acquire() is False)
    check("bucket bị rút cạn", b.available == 0.0, str(b.available))
    t0 = time.monotonic()
    b.acquire()                       # phải chờ hết cooldown
    waited = time.monotonic() - t0
    check("chờ đúng cooldown rồi mới cho qua", 0.25 < waited < 0.6, f"{waited:.2f}s")
    check("đếm số lần 429", b.stats.penalties == 1)


def test_bucket_timeout_and_guards():
    print("\n[4] Timeout và kiểm tra tham số")
    b = RL.TokenBucket(capacity=1, refill_per_sec=0.5, name="t")
    b.acquire()
    t0 = time.monotonic()
    got = b.acquire(timeout=0.2)
    check("hết timeout thì trả False", got is False, f"{time.monotonic()-t0:.2f}s")

    try:
        b.acquire(tokens=99)
        check("xin quá sức chứa phải báo lỗi", False)
    except ValueError:
        check("xin quá sức chứa phải báo lỗi", True)

    for bad in [(0, 1), (1, 0), (-1, 1)]:
        try:
            RL.TokenBucket(*bad)
            check(f"tham số {bad} phải bị từ chối", False)
        except ValueError:
            check(f"tham số {bad} phải bị từ chối", True)


def test_registry_shared():
    print("\n[5] Registry: mọi phần dùng chung quota một host")
    reg = RL.RateLimiterRegistry(overrides={"a.test": (3.0, 100.0)})
    b1 = reg.bucket("a.test")
    b2 = reg.bucket("A.TEST")          # khác hoa thường -> vẫn cùng bucket
    check("cùng host -> cùng bucket", b1 is b2)

    for _ in range(3):
        reg.acquire("a.test")
    check("đã tiêu hết quota chung", reg.bucket("a.test").try_acquire() is False)

    check("host lạ dùng ngưỡng dự phòng",
          reg.bucket("khong-biet.test").capacity == RL.RateLimiterRegistry.FALLBACK[0])
    check("GeckoTerminal có ngưỡng riêng dưới 30",
          reg.bucket("api.geckoterminal.com").capacity <= 30)
    check("report() chạy được", "a.test" in reg.report())


# =========================================================================== #
#  HttpClient dùng limiter
# =========================================================================== #

def test_httpclient_uses_limiter():
    print("\n[6] HttpClient xin quota trước mỗi call, phạt khi gặp 429")
    import base_meme_bot as B

    cfg = B.Config()
    cfg.max_retries = 3
    reg = RL.RateLimiterRegistry(overrides={"fake.test": (2.0, 1000.0)})
    http = B.HttpClient(cfg, limiter=reg)

    calls = {"n": 0}

    class Resp:
        def __init__(self, code, payload=None, headers=None):
            self.status_code = code
            self._p = payload or {}
            self.headers = headers or {}

        def json(self):
            return self._p

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return Resp(200, {"ok": calls["n"]})

    http.s.get = fake_get
    out = http.get("https://fake.test/x")
    check("trả về JSON khi 200", out == {"ok": 1})
    check("đã tiêu 1 token", reg.bucket("fake.test").available <= 1.01,
          str(reg.bucket("fake.test").available))

    # giờ giả lập 429 rồi 200
    seq = [Resp(429, {}, {"Retry-After": "0.2"}), Resp(200, {"ok": "sau429"})]

    def fake_429(url, headers=None, timeout=None):
        return seq.pop(0)

    http.s.get = fake_429
    t0 = time.monotonic()
    out = http.get("https://fake.test/y")
    dt = time.monotonic() - t0
    check("gặp 429 -> retry và cuối cùng thành công", out == {"ok": "sau429"})
    check("429 kích hoạt phạt bucket", reg.bucket("fake.test").stats.penalties == 1)
    check("có nghỉ trước khi thử lại", dt >= 0.15, f"{dt:.2f}s")

    # lỗi mạng liên tục -> trả None, không ném exception
    def boom(url, headers=None, timeout=None):
        raise B.requests.RequestException("mạng chết")

    cfg.max_retries = 2
    http.s.get = boom
    check("lỗi mạng liên tục -> trả None chứ không sập", http.get("https://fake.test/z") is None)


def test_httpclient_without_limiter():
    print("\n[7] Thiếu rate_limit.py -> lùi về giãn cách cũ, vẫn chạy")
    import base_meme_bot as B

    cfg = B.Config()
    http = B.HttpClient(cfg, limiter=None)
    http.limiter = None                # ép trường hợp không có limiter

    class Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"ok": True}

    http.s.get = lambda url, headers=None, timeout=None: Resp()
    t0 = time.monotonic()
    http.get("https://x.test/a", min_interval=0.2)
    http.get("https://x.test/a", min_interval=0.2)
    dt = time.monotonic() - t0
    check("vẫn giãn cách theo min_interval", dt >= 0.15, f"{dt:.2f}s")


# =========================================================================== #
#  Runner
# =========================================================================== #

def _make_runner(tmp, fast=0.05, slow=0.15):
    import base_meme_bot as B
    import runner as R

    cfg = B.Config()
    cfg.seen_file = os.path.join(tmp, "seen.json")
    cfg.output_csv = os.path.join(tmp, "sig.csv")
    cfg.heartbeat_file = os.path.join(tmp, "hb.json")
    pcfg = P.PaperConfig(
        positions_file=os.path.join(tmp, "pos.json"),
        trades_csv=os.path.join(tmp, "trades.csv"),
        state_file=os.path.join(tmp, "pstate.json"),
        telegram_on_close=False, telegram_daily_report=False)

    rcfg = R.RunnerConfig(fast_interval=fast, slow_interval=slow,
                          heartbeat_interval=9999, backup_interval=9999,
                          tick=0.01, backoff_base=0.02, backoff_max=0.05)
    r = R.Runner(cfg, rcfg, pcfg)
    return r, R


def test_runner_two_cadences():
    print("\n[8] Runner chạy đúng 2 nhịp với tần suất khác nhau")
    tmp = tempfile.mkdtemp()
    try:
        r, R = _make_runner(tmp, fast=0.05, slow=0.20)
        hits = {"fast": 0, "slow": 0}
        r.scanner.track_positions = lambda: hits.__setitem__("fast", hits["fast"] + 1) or []
        r.scanner.run_once = lambda track=True: hits.__setitem__("slow", hits["slow"] + 1) or []

        import threading
        th = threading.Thread(target=r.run, daemon=True)
        th.start()
        time.sleep(0.9)
        r.stop = True
        th.join(timeout=3)

        check("nhịp nhanh chạy nhiều lần", hits["fast"] >= 8, str(hits["fast"]))
        check("nhịp chậm chạy ít lần hơn", 2 <= hits["slow"] <= 8, str(hits["slow"]))
        check("nhanh chạy dày hơn chậm rõ rệt", hits["fast"] > hits["slow"] * 1.5,
              f"{hits['fast']} vs {hits['slow']}")
        check("bộ đếm nội bộ khớp", r.counts["fast"] == hits["fast"])
    finally:
        shutil.rmtree(tmp)


def test_runner_survives_errors():
    print("\n[9] Runner sống sót qua lỗi và hồi phục")
    tmp = tempfile.mkdtemp()
    try:
        r, R = _make_runner(tmp, fast=0.05, slow=99)
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] <= 3:
                raise RuntimeError("API sập")
            return []

        r.scanner.track_positions = flaky
        r.scanner.run_once = lambda track=True: []
        sent = []
        r._notify = lambda t: sent.append(t)

        import threading
        th = threading.Thread(target=r.run, daemon=True)
        th.start()
        time.sleep(0.8)
        r.stop = True
        th.join(timeout=3)

        check("không sập dù lỗi liên tiếp", state["n"] > 4, f"{state['n']} lượt")
        check("đếm lỗi đúng", r.counts["errors"] == 3, str(r.counts["errors"]))
        check("đã reset chuỗi lỗi sau khi hồi phục", r.errors == 0)
        check("có báo Telegram khi lỗi dồn",
              any("lỗi liên tiếp" in s for s in sent), f"{len(sent)} tin")
        check("có báo khi hồi phục", any("hồi phục" in s for s in sent))
    finally:
        shutil.rmtree(tmp)


def test_runner_exits_after_max_errors():
    print("\n[10] Lỗi liên tục quá ngưỡng -> thoát để systemd restart")
    tmp = tempfile.mkdtemp()
    try:
        r, R = _make_runner(tmp, fast=0.02, slow=99)
        r.rcfg.max_consecutive_errors = 4
        r.scanner.track_positions = lambda: (_ for _ in ()).throw(RuntimeError("chết hẳn"))
        r.scanner.run_once = lambda track=True: []
        r._notify = lambda t: None

        code = None
        try:
            r.run()
        except SystemExit as e:
            code = e.code
        check("thoát với mã 1", code == 1, str(code))
        check("state vẫn được lưu trước khi thoát",
              os.path.exists(os.path.join(tmp, "seen.json")))
    finally:
        shutil.rmtree(tmp)


def test_runner_graceful_shutdown():
    print("\n[11] Tắt êm: lưu vị thế trước khi thoát")
    tmp = tempfile.mkdtemp()
    try:
        r, R = _make_runner(tmp, fast=0.05, slow=99)
        r.scanner.run_once = lambda track=True: []
        # chặn mạng: trả giá cố định, không gọi ra ngoài
        r.scanner.gt.quotes_for_pools = lambda pools: {
            p: {"price": 1.05, "liquidity": 100_000.0} for p in pools}

        class C:
            token_address = "0x" + "a" * 40
            pool_address = "0x" + "b" * 62
            symbol = "SHUT"
            price_usd = 1.0
            liquidity_usd = 100_000.0
            market_cap = 500_000.0
            age_hours = 3.0
            score = 75.0
            reasons = []
            url = ""
        r.scanner.paper.open_from_signals([C()])
        os.remove(os.path.join(tmp, "pos.json"))    # xoá để chắc chắn shutdown ghi lại

        import threading
        th = threading.Thread(target=r.run, daemon=True)
        th.start()
        time.sleep(0.3)
        r._on_signal(15, None)                       # giả lập SIGTERM
        th.join(timeout=5)

        check("thread đã dừng", not th.is_alive())
        check("vị thế được ghi lại khi tắt", os.path.exists(os.path.join(tmp, "pos.json")))
        t2 = P.PaperTrader(r.scanner.paper.cfg, verbose=False)
        check("đọc lại đúng vị thế sau khi tắt", len(t2.positions) == 1)
    finally:
        shutil.rmtree(tmp)


def test_shutdown_interrupts_retries():
    print("\n[11b] Đang retry mạng mà nhận SIGTERM -> bỏ dở, không treo")
    import base_meme_bot as B

    cfg = B.Config()
    cfg.max_retries = 5          # chuỗi backoff đầy đủ sẽ mất ~30s
    stopping = {"v": False}
    http = B.HttpClient(cfg, limiter=RL.RateLimiterRegistry(
        overrides={"slow.test": (50.0, 1000.0)}))
    http.should_stop = lambda: stopping["v"]

    attempts = {"n": 0}

    def always_fail(url, headers=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 2:
            stopping["v"] = True     # SIGTERM đến giữa chuỗi retry
        raise B.requests.RequestException("mạng chết")

    http.s.get = always_fail
    t0 = time.monotonic()
    out = http.get("https://slow.test/x")
    dt = time.monotonic() - t0

    check("trả None chứ không ném lỗi", out is None)
    check("dừng sớm thay vì chạy hết 5 lần thử", attempts["n"] <= 3, f"{attempts['n']} lần")
    check("thoát nhanh, không chờ hết backoff", dt < 8.0, f"{dt:.1f}s")


def test_budget_estimate():
    print("\n[12] Ước tính ngân sách API cảnh báo đúng khi nhịp quá dày")
    tmp = tempfile.mkdtemp()
    try:
        r, _ = _make_runner(tmp, fast=45, slow=150)
        b = r.estimate_budget()
        check("nhịp mặc định nằm trong ngân sách", b["ok"] is True,
              f"{b['total']:.1f}/{b['cap']:.0f} call/phút")

        r.rcfg.fast_interval = 1        # 60 call/phút chỉ riêng nhịp nhanh
        b2 = r.estimate_budget()
        check("nhịp 1 giây bị đánh dấu quá dày", b2["ok"] is False,
              f"{b2['total']:.1f} call/phút")
    finally:
        shutil.rmtree(tmp)


# =========================================================================== #
#  Dashboard + backup
# =========================================================================== #

def test_dashboard():
    print("\n[13] Dashboard sinh HTML hợp lệ")
    tmp = tempfile.mkdtemp()
    try:
        cfg = P.PaperConfig(positions_file=os.path.join(tmp, "pos.json"),
                            trades_csv=os.path.join(tmp, "trades.csv"),
                            state_file=os.path.join(tmp, "st.json"),
                            telegram_on_close=False, telegram_daily_report=False)
        out = os.path.join(tmp, "dash.html")

        # 1) chưa có dữ liệu
        path = D.build(cfg, out)
        check("sinh được dashboard khi chưa có dữ liệu", path and os.path.exists(out))
        htm = open(out, encoding="utf-8").read()
        check("có thông báo trống", "Chưa có lệnh nào đóng" in htm)

        # 2) có dữ liệu thật
        t = P.PaperTrader(cfg, verbose=False)

        class C:
            def __init__(self, i, score):
                self.token_address = f"0x{i}" + "a" * 39
                self.pool_address = f"0x{i}" + "b" * 61
                self.symbol = f"TK{i}"
                self.price_usd = 1.0
                self.liquidity_usd = 100_000.0
                self.market_cap = 500_000.0
                self.age_hours = 3.0
                self.score = score
                self.reasons = ["x"]
                self.url = "https://ex.test"

        class Q:
            def __init__(self, m):
                self.m = m
            def __call__(self, pools):
                return {p: {"price": self.m, "liquidity": 100_000.0} for p in pools}

        p1 = t.open_from_signals([C(1, 90.0)])[0]
        t.mark_to_market(Q(2.5))                 # chốt lời
        p2 = t.open_from_signals([C(2, 60.0)])[0]
        t.mark_to_market(Q(0.5))                 # cắt lỗ
        t.open_from_signals([C(3, 70.0)])        # để mở
        t.mark_to_market(Q(1.2))

        path = D.build(cfg, out)
        htm = open(out, encoding="utf-8").read()
        check("có dữ liệu -> sinh lại được", path is not None)
        check("có bảng vị thế mở", "TK3" in htm)
        check("có lệnh đã đóng", "TK1" in htm and "TK2" in htm)
        check("có equity curve", "new Chart" in htm and "EQ_VALUES" in htm)
        check("có bảng theo bậc điểm", "85+" in htm)
        check("HTML đóng thẻ đầy đủ", htm.rstrip().endswith("</html>"))
        check("nhắc rõ đây là mô phỏng", "mô phỏng" in htm)

        # 3) chống chèn HTML từ tên token
        class Evil(C):
            def __init__(self):
                super().__init__(9, 70.0)
                self.symbol = '<img src=x onerror=alert(1)>'
        t.open_from_signals([Evil()])
        t.mark_to_market(Q(1.1))
        D.build(cfg, out)
        htm = open(out, encoding="utf-8").read()
        check("tên token độc hại bị escape",
              "<img src=x" not in htm and "&lt;img" in htm)
    finally:
        shutil.rmtree(tmp)


def test_backup_safety():
    print("\n[14] Backup: an toàn khi thiếu điều kiện, che token trong log")
    tmp = tempfile.mkdtemp()
    try:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            # không phải git repo (hoặc không có remote) -> phải trả False êm ru
            ok = SB.push(verbose=False)
            check("không có repo/remote -> trả False, không ném lỗi", ok is False)
        finally:
            os.chdir(cwd)

        check("che token dạng URL",
              "***" in SB._redact("https://x-access-token:ghp_abc@github.com/a/b"))
        check("che token dạng ghp_",
              "ghp_***" in SB._redact("token là ghp_" + "A" * 30))
        check("danh sách file là tường minh, không có dấu sao",
              all("*" not in f for f in SB.STATE_FILES))
        check("không đẩy file nhạy cảm",
              not any(f in SB.STATE_FILES for f in [".env", "config.json", ".git"]))

        s = SB.status()
        check("status() luôn trả dict có khoá ready", "ready" in s)
    finally:
        shutil.rmtree(tmp)


def test_cli_smoke():
    print("\n[15] CLI chạy được (dry-run, không gọi mạng)")
    here = os.path.dirname(os.path.abspath(__file__))
    p = subprocess.run([sys.executable, "runner.py", "--dry-run"],
                       cwd=here, capture_output=True, text=True, timeout=60)
    check("runner --dry-run thoát mã 0", p.returncode == 0, p.stderr[-200:])
    check("in ước tính ngân sách", "call/phút" in p.stdout)
    check("kết luận OK với nhịp mặc định", "OK" in p.stdout, p.stdout[-120:])

    p2 = subprocess.run([sys.executable, "runner.py", "--dry-run", "--fast", "1"],
                        cwd=here, capture_output=True, text=True, timeout=60)
    check("nhịp 1s bị cảnh báo quá dày", "QUÁ DÀY" in p2.stdout)

    p3 = subprocess.run([sys.executable, "state_backup.py", "--status"],
                        cwd=here, capture_output=True, text=True, timeout=60)
    check("state_backup --status chạy được", p3.returncode == 0, p3.stderr[-200:])

    p4 = subprocess.run([sys.executable, "rate_limit.py"],
                        cwd=here, capture_output=True, text=True, timeout=60)
    check("rate_limit demo chạy được", p4.returncode == 0 and "call 8" in p4.stdout)


if __name__ == "__main__":
    print("=" * 62)
    print("KIỂM THỬ HẠ TẦNG 24/7".center(62))
    print("=" * 62)
    for fn in [test_bucket_burst, test_bucket_rate, test_bucket_penalty,
               test_bucket_timeout_and_guards, test_registry_shared,
               test_httpclient_uses_limiter, test_httpclient_without_limiter,
               test_runner_two_cadences, test_runner_survives_errors,
               test_runner_exits_after_max_errors, test_runner_graceful_shutdown,
               test_shutdown_interrupts_retries,
               test_budget_estimate, test_dashboard, test_backup_safety,
               test_cli_smoke]:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{fn.__name__} ném exception: {e}")
    print("\n" + "=" * 62)
    print(f"KẾT QUẢ: {len(PASS)} pass · {len(FAIL)} fail")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
