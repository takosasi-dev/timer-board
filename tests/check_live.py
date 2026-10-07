"""実物の systemd で一覧が出るかを確かめる(Linux 専用。CI と WSL の Arch で回す)。

    python tests/check_live.py              # 読むだけ
    python tests/check_live.py --transient  # 一時的なタイマーで成功・失敗も見る

--transient は systemd-run で tb-test-ok / tb-test-ng を作り、終わったら
stop と reset-failed で片付ける。root でなければ `sudo -n` を付けて叩く。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from timerboard import __main__ as cli, core  # noqa: E402

UNITS = {"tb-test-ok": "/bin/true", "tb-test-ng": "/bin/false"}


def no_json(argv):
    """古い systemd の真似。--output=json を付けると表が返る。"""
    if "--output=json" in argv:
        argv = [a for a in argv if a != "--output=json"]
    return core.run_command(argv)


def sudo(argv):
    pre = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run(pre + argv, capture_output=True, text=True)


def check_read() -> None:
    rows = core.collect()
    print(f"システム: {len(rows)} 本")
    print("\n".join(core.table(rows, time.time())))
    assert rows, "システムのタイマーが1本も取れない"
    for r in rows:
        assert r["timer"].endswith(".timer"), r
        assert r["state"] in ("ok", "failed", "running", "never", "unknown"), r

    # 表の解析(フォールバック)でも同じものが取れる
    fb = {r["timer"]: r for r in core.collect(runner=no_json)}
    assert set(fb) == {r["timer"] for r in rows}, (set(fb), rows)
    for r in rows:
        for k in ("next", "last"):
            a, b = r[k], fb[r["timer"]][k]
            assert (a is None) == (b is None) and (a is None or abs(a - b) < 1.5), (r["timer"], k, a, b)
    print("表の解析でも一致")

    try:
        print(f"ユーザー: {len(core.collect(user=True))} 本")
    except core.Unavailable as e:
        print(f"ユーザー: 取れない ({e})")

    rc, out, err = core.run_command(core.journal_argv(rows[0]["service"], False))
    assert rc == 0, err


def check_transient() -> None:
    try:
        for name, exe in UNITS.items():
            p = sudo(["systemd-run", "--quiet", f"--unit={name}", "--on-active=2s",
                      # 一度きりのタイマーは発火後すぐ消えるので、1時間後の次回を持たせて残す
                      "--on-unit-active=1h", "--timer-property=AccuracySec=100ms", exe])
            assert p.returncode == 0, p.stderr
        deadline = time.time() + 30
        while True:
            rows = {r["timer"]: r for r in core.collect()}
            st = {n: rows.get(n + ".timer", {}).get("state") for n in UNITS}
            if st == {"tb-test-ok": "ok", "tb-test-ng": "failed"}:
                break
            assert time.time() < deadline, f"結果が揃わない: {st}"
            time.sleep(0.5)
        ng = rows["tb-test-ng.timer"]
        assert ng["result"] == "exit-code" and ng["status"] == 1 and ng["last"], ng
        assert rows["tb-test-ok.timer"]["duration"] is not None
        print("\n".join(core.table(list(rows.values()), time.time())))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--failed"])
        assert rc == 1 and "tb-test-ng.timer" in buf.getvalue() and "tb-test-ok" not in buf.getvalue()
        print("--failed: 終了コード 1、tb-test-ng だけ")
    finally:
        sudo(["systemctl", "stop", *(n + ".timer" for n in UNITS)])
        sudo(["systemctl", "reset-failed", *(n + s for n in UNITS for s in (".timer", ".service"))])


def main() -> int:
    if not shutil.which("systemctl"):
        print("systemctl が無いので飛ばします")
        return 0
    check_read()
    if "--transient" in sys.argv:
        check_transient()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
