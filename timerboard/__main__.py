"""コマンドの入口。引数なしで TUI、--list / --json / --failed は画面なしで出す。"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, core

SCOPE = {False: "システム", True: "ユーザー"}


def main(argv: list[str] | None = None, runner=core.run_command) -> int:
    # Windows の既定のコードページだと日本語の print で落ちるため
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(
        prog="timerboard",
        description="systemd タイマーの次回・前回・結果を一覧にする(読むだけ)。")
    ap.add_argument("--list", action="store_true", help="表をテキストで出す")
    ap.add_argument("--json", action="store_true", help="JSON で出す")
    ap.add_argument("--failed", action="store_true",
                    help="前回失敗したタイマーだけを出す。1件でもあれば終了コード 1")
    ap.add_argument("--user", action="store_true", help="ユーザーのタイマーを見る")
    ap.add_argument("--version", action="version", version=f"timerboard {__version__}")
    a = ap.parse_args(argv)

    if not (a.list or a.json or a.failed):
        try:
            from . import tui
        except ImportError:  # Windows の素の Python には curses が無い
            print("curses が使えません。--list か --json を使ってください", file=sys.stderr)
            return 2
        return tui.run(a.user, runner)

    scope = SCOPE[a.user]
    try:
        rows = core.collect(a.user, runner)
    except core.Unavailable as e:
        msg = f"{scope}のタイマーは取れない: {e}"
        if a.json:
            print(json.dumps({"scope": "user" if a.user else "system", "available": False,
                              "error": str(e)}, ensure_ascii=False, indent=2))
        print(msg, file=sys.stderr)
        return 2

    if a.failed:
        rows = [r for r in rows if r["state"] == "failed"]
    now = time.time()
    if a.json:
        print(json.dumps({"scope": "user" if a.user else "system", "available": True,
                          "timers": core.to_json(rows, now)}, ensure_ascii=False, indent=2))
    elif rows:
        print("\n".join(core.table(rows, now)))
    else:
        print(f"{scope}: 失敗したタイマーはありません" if a.failed else f"{scope}: タイマーはありません")
    return 1 if a.failed and rows else 0


if __name__ == "__main__":
    sys.exit(main())
