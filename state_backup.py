#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_backup.py — Đẩy state + dashboard lên GitHub định kỳ.

VÌ SAO CẦN
    Trên Oracle Cloud Always Free, instance CÓ THỂ bị thu hồi (xem DEPLOY.md).
    Mất VPS mà mất luôn paper_trades.csv thì mất toàn bộ bằng chứng về hiệu
    suất — thứ tốn hàng tuần mới tích được. Đẩy lên GitHub là bản sao rẻ nhất,
    lại tiện: repo public + GitHub Pages = dashboard xem được từ mọi nơi.

XÁC THỰC
    Ưu tiên biến môi trường GITHUB_TOKEN (Personal Access Token, quyền `repo`
    hoặc `contents:write` nếu dùng fine-grained). Không có thì dùng credential
    sẵn có của git (SSH key / credential helper).

    Token KHÔNG BAO GIỜ được ghi vào .git/config — nó chỉ nằm trong URL tạm
    của một lệnh push duy nhất, và được che khi in log.

AN TOÀN
    - Chỉ add đúng danh sách file state. Không bao giờ `git add .`
    - Không commit khi không có thay đổi.
    - `git pull --rebase` trước khi push để không đá nhau với commit khác.
    - Mọi lỗi đều được nuốt và trả False: backup hỏng không được làm chết bot.

DÙNG
    python state_backup.py            # đẩy ngay một lần
    python state_backup.py --status   # xem cấu hình có sẵn sàng không
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from datetime import datetime, timezone

# Chỉ những file này được đẩy lên. Cố ý liệt kê tường minh.
STATE_FILES = [
    "paper_positions.json",
    "paper_trades.csv",
    "paper_state.json",
    "base_seen.json",
    "base_signals.csv",
    "base_heartbeat.json",
    "dashboard.html",
]

BOT_NAME = "base-meme-bot"
BOT_EMAIL = "bot@users.noreply.github.com"


def _redact(text: str) -> str:
    """Che token trong log. Chưa từng thấy log nào lộ secret mà chủ nhân biết trước."""
    text = re.sub(r"https://[^@\s]*@", "https://***@", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "ghp_***", text)
    return text


def _run(args: list[str], cwd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, _redact((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        return 124, f"quá thời gian: {' '.join(args[:2])}"
    except FileNotFoundError:
        return 127, "không tìm thấy lệnh git"
    except Exception as e:
        return 1, _redact(str(e))


def _repo_root(start: str | None = None) -> str | None:
    start = start or os.path.dirname(os.path.abspath(__file__))
    code, out = _run(["git", "rev-parse", "--show-toplevel"], start)
    return out.strip() if code == 0 and out.strip() else None


def _remote_url(root: str) -> str | None:
    code, out = _run(["git", "remote", "get-url", "origin"], root)
    return out.strip() if code == 0 and out.strip() else None


def _branch(root: str) -> str:
    code, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    b = out.strip() if code == 0 else ""
    return b if b and b != "HEAD" else "main"


def _auth_url(remote: str, token: str) -> str | None:
    """Chèn token vào URL https. Remote dạng SSH thì trả None (đã có key rồi)."""
    if not remote.startswith("https://"):
        return None
    rest = remote.split("https://", 1)[1]
    if "@" in rest.split("/", 1)[0]:          # đã có credential trong URL
        rest = rest.split("@", 1)[1]
    return f"https://x-access-token:{token}@{rest}"


def status() -> dict:
    root = _repo_root()
    if not root:
        return {"ready": False, "reason": "không nằm trong git repo"}
    remote = _remote_url(root)
    if not remote:
        return {"ready": False, "reason": "chưa cấu hình remote origin", "root": root}
    token = os.getenv("GITHUB_TOKEN", "")
    present = [f for f in STATE_FILES if os.path.exists(os.path.join(root, f))]
    return {
        "ready": True,
        "root": root,
        "remote": _redact(remote),
        "branch": _branch(root),
        "auth": "GITHUB_TOKEN" if token else ("SSH key" if remote.startswith("git@")
                                              else "credential helper của git"),
        "files_present": present,
        "files_missing": [f for f in STATE_FILES if f not in present],
    }


def push(verbose: bool = True) -> bool:
    """Commit + push các file state. Trả True nếu có gì đó được đẩy lên."""
    def say(m):
        if verbose:
            print(f"  [backup] {m}")

    root = _repo_root()
    if not root:
        say("bỏ qua: không nằm trong git repo")
        return False

    remote = _remote_url(root)
    if not remote:
        say("bỏ qua: chưa có remote origin")
        return False

    present = [f for f in STATE_FILES if os.path.exists(os.path.join(root, f))]
    if not present:
        say("bỏ qua: chưa có file state nào")
        return False

    # Có gì thay đổi không? Không thì thôi, tránh rác lịch sử commit.
    code, out = _run(["git", "status", "--porcelain", "--"] + present, root)
    if code != 0:
        say(f"git status lỗi: {out.strip()[:200]}")
        return False
    if not out.strip():
        say("không có thay đổi")
        return False

    _run(["git", "config", "user.name", BOT_NAME], root)
    _run(["git", "config", "user.email", BOT_EMAIL], root)

    code, out = _run(["git", "add", "--"] + present, root)
    if code != 0:
        say(f"git add lỗi: {out.strip()[:200]}")
        return False

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    code, out = _run(["git", "commit", "-m", f"state: sao lưu tự động {stamp} [skip ci]"], root)
    if code != 0 and "nothing to commit" not in out:
        say(f"git commit lỗi: {out.strip()[:200]}")
        return False

    branch = _branch(root)
    token = os.getenv("GITHUB_TOKEN", "")
    push_target = _auth_url(remote, token) if token else None

    # Đồng bộ trước để không bị từ chối vì non-fast-forward.
    if push_target:
        _run(["git", "pull", "--rebase", "--autostash", push_target, branch], root, 120)
        code, out = _run(["git", "push", push_target, f"HEAD:{branch}"], root, 120)
    else:
        _run(["git", "pull", "--rebase", "--autostash", "origin", branch], root, 120)
        code, out = _run(["git", "push", "origin", f"HEAD:{branch}"], root, 120)

    if code != 0:
        say(f"git push lỗi: {out.strip()[:300]}")
        return False

    say(f"đã đẩy {len(present)} file lên {branch}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Sao lưu state paper trading lên GitHub")
    ap.add_argument("--status", action="store_true", help="kiểm tra cấu hình rồi thoát")
    args = ap.parse_args()

    if args.status:
        s = status()
        if not s["ready"]:
            print(f"❌ chưa sẵn sàng: {s['reason']}")
            return
        print(f"✅ repo      : {s['root']}")
        print(f"   remote    : {s['remote']}")
        print(f"   nhánh     : {s['branch']}")
        print(f"   xác thực  : {s['auth']}")
        print(f"   file có   : {', '.join(s['files_present']) or '(chưa có)'}")
        if s["files_missing"]:
            print(f"   chưa có   : {', '.join(s['files_missing'])}")
        return

    push()


if __name__ == "__main__":
    main()
