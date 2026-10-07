"""中身(解析・判定・表示)のテスト。systemctl は呼ばず、fixture を読む runner に差し替える。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from timerboard import __main__ as cli, core  # noqa: E402

FX = Path(__file__).parent / "fixtures"
NOW = 1800000000.0  # list-timers.json の時刻はここを基準に作ってある


def read(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


SHOW = {b["Id"]: b for b in core.parse_show(read("show.txt"))}


def fake(json_ok: bool = True, user_ok: bool = True, calls: list | None = None):
    """systemctl の代わり。show は頼まれた名前の順にブロックを組み立てて返す。"""
    def run(argv):
        if calls is not None:
            calls.append(argv)
        if "--user" in argv and not user_ok:
            # 版によっては繋がらなくても終了コードが 0 のまま
            return 0, "", "Failed to connect to user scope bus via local transport\n"
        if "list-timers" in argv:
            if "--output=json" in argv:
                return (0, read("list-timers.json"), "") if json_ok else (0, read("list-timers.txt"), "")
            return 0, read("list-timers.txt"), ""
        if "show" in argv:
            names = argv[argv.index("show") + 1:argv.index("-p")]
            blocks = [SHOW.get(n, {"Id": n, "LoadState": "not-found"}) for n in names]
            return 0, "\n\n".join("\n".join(f"{k}={v}" for k, v in b.items()) for b in blocks) + "\n", ""
        return 1, "", "unexpected"
    return run


class Parse(unittest.TestCase):
    def test_json(self):
        e = core.parse_list_json(read("list-timers.json"))
        self.assertEqual(e[0], {"timer": "backup.timer", "service": "backup.service",
                                "next": 1800000000.0, "last": 1799990000.5})
        self.assertIsNone(e[2]["next"])
        self.assertIsNone(e[2]["last"])  # 0 は「無し」

    def test_table(self):
        e = core.parse_list_table(read("list-timers.txt"))
        self.assertEqual([x["timer"] for x in e],
                         ["backup.timer", "report.timer", "cache-clean.timer", "orphan.timer"])
        loc = lambda s: time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))  # noqa: E731
        self.assertEqual(e[0]["next"], loc("2027-01-15 08:00:00"))
        self.assertEqual(e[0]["last"], loc("2027-01-14 08:00:03"))
        self.assertEqual(e[1]["next"], loc("2027-01-22 15:45:54"))
        self.assertIsNone(e[1]["last"])
        self.assertIsNone(e[3]["service"])  # 古い版の n/a
        self.assertEqual(core.parse_list_table("garbage"), [])

    def test_show_and_schedule(self):
        self.assertEqual(core.parse_schedule(SHOW["report.timer"]),
                         ["OnCalendar=Mon *-*-* 09:00:00", "OnCalendar=Fri *-*-* 18:00:00"])
        self.assertEqual(core.parse_schedule(SHOW["cache-clean.timer"]),
                         ["OnUnitActiveSec=1d", "OnBootSec=15min"])
        self.assertEqual(core.parse_schedule({}), [])


class Collect(unittest.TestCase):
    def rows(self, **kw):
        return {r["timer"]: r for r in core.collect(runner=fake(**kw))}

    def test_states(self):
        r = self.rows()
        self.assertEqual(r["backup.timer"]["state"], "ok")
        self.assertEqual(r["backup.timer"]["duration"], 2.5)
        self.assertEqual(r["report.timer"]["state"], "failed")
        self.assertEqual(core.result_text(r["report.timer"]), "失敗(終了コード2)")
        self.assertEqual(r["cache-clean.timer"]["state"], "never")
        self.assertEqual(r["sync.timer"]["state"], "running")
        # 前回の時刻はあるが、このブートでは動いていない(Persistent で再起動をまたいだ)
        self.assertEqual(r["rotate.timer"]["state"], "unknown")
        self.assertEqual(r["backup.timer"]["unit_file_state"], "enabled")

    def test_one_show_call(self):
        calls = []
        core.collect(runner=fake(calls=calls))
        self.assertEqual(sum("show" in c for c in calls), 1)
        self.assertEqual(sum("list-timers" in c for c in calls), 1)

    def test_table_fallback(self):
        calls = []
        r = {x["timer"]: x for x in core.collect(runner=fake(json_ok=False, calls=calls))}
        self.assertEqual(len(calls), 3)  # json を試す → 表 → show
        self.assertEqual(r["backup.timer"]["state"], "ok")
        self.assertIsNone(r["orphan.timer"]["service"])
        self.assertEqual(r["orphan.timer"]["state"], "never")

    def test_user_unavailable(self):
        with self.assertRaises(core.Unavailable) as c:
            core.collect(user=True, runner=fake(user_ok=False))
        self.assertIn("Failed to connect", str(c.exception))

    def test_no_systemctl(self):
        with self.assertRaises(core.Unavailable):
            core.collect(runner=lambda argv: (127, "", "systemctl が見つかりません"))

    def test_classify_edges(self):
        # 起動前に落ちた(依存の失敗など)は、時刻が無くても失敗
        self.assertEqual(core.classify({"ActiveState": "failed", "Result": "resources"}, None)["state"],
                         "failed")
        r = core.classify({"ActiveState": "failed", "Result": "signal", "ExecMainStatus": "9",
                           "ExecMainStartTimestampMonotonic": "1", "ExecMainExitTimestampMonotonic": "2"}, 1.0)
        self.assertEqual(core.result_text(r), "失敗(シグナル9)")


class Display(unittest.TestCase):
    def test_span(self):
        self.assertEqual(core.span(None), "-")
        self.assertEqual(core.span(0), "0秒")
        self.assertEqual(core.span(0.25), "0.2秒")
        self.assertEqual(core.span(45), "45秒")
        self.assertEqual(core.span(125), "2分5秒")
        self.assertEqual(core.span(1800), "30分")
        self.assertEqual(core.span(3 * 3600 + 120), "3時間2分")
        self.assertEqual(core.span(8 * 86400 + 12 * 3600 + 5), "8日12時間")
        self.assertEqual(core.span(-5), "0秒")  # 次回の時刻を過ぎて、まだ動いていない間

    def test_width(self):
        self.assertEqual(core.width("失敗abc"), 7)
        self.assertEqual(core.clip("あいうえお", 6), "あい…")
        self.assertEqual(core.width(core.pad("成功", 6)), 6)

    def test_table_narrow(self):
        rows = core.collect(runner=fake())
        wide = core.table(rows, NOW)
        self.assertIn("スケジュール", wide[0])
        self.assertEqual(len(wide), len(rows) + 1)
        narrow = core.table(rows, NOW, 60)
        self.assertTrue(all(core.width(l) <= 60 for l in narrow), narrow)
        self.assertIn("タイマー", narrow[0])
        self.assertIn("結果", narrow[0])  # 結果は消さない
        self.assertNotIn("スケジュール", narrow[0])
        self.assertIn("失敗(終了コード2)", "\n".join(narrow))

    def test_left_and_passed(self):
        r = {x["timer"]: x for x in core.to_json(core.collect(runner=fake()), NOW)}
        self.assertEqual(r["backup.timer"]["left_sec"], 0)
        self.assertEqual(r["sync.timer"]["left_sec"], 600)
        self.assertEqual(r["report.timer"]["passed_sec"], 3600)
        self.assertIsNone(r["cache-clean.timer"]["left_sec"])


class Cli(unittest.TestCase):
    def run_cli(self, *args, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(args), runner=fake(**kw))
        return rc, out.getvalue(), err.getvalue()

    def test_list(self):
        rc, out, _ = self.run_cli("--list")
        self.assertEqual(rc, 0)
        self.assertIn("backup.timer", out)
        self.assertIn("未実行", out)

    def test_failed(self):
        rc, out, _ = self.run_cli("--failed")
        self.assertEqual(rc, 1)
        self.assertIn("report.timer", out)
        self.assertNotIn("backup.timer", out)

    def test_failed_json(self):
        rc, out, _ = self.run_cli("--failed", "--json")
        self.assertEqual(rc, 1)
        d = json.loads(out)
        self.assertEqual([t["timer"] for t in d["timers"]], ["report.timer"])
        self.assertEqual(d["timers"][0]["result"], "exit-code")

    def test_user_unavailable(self):
        rc, out, err = self.run_cli("--list", "--user", user_ok=False)
        self.assertEqual(rc, 2)
        self.assertIn("ユーザーのタイマーは取れない", err)
        rc, out, _ = self.run_cli("--json", "--user", user_ok=False)
        self.assertFalse(json.loads(out)["available"])


if __name__ == "__main__":
    unittest.main()
