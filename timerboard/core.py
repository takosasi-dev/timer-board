"""systemd タイマーを読んで一覧にする中身。画面(curses)とは分けてある。

外のコマンドはすべて `runner(argv) -> (returncode, stdout, stderr)` を通す。
テストでは runner を差し替えて fixture(手で作った架空の出力)を読ませる。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata

# 一覧の1行に要るものを、タイマー分とサービス分まとめて1回の show で取る
TIMER_PROPS = ["Id", "Unit", "TimersCalendar", "TimersMonotonic", "UnitFileState",
               "ActiveState", "Result"]
SERVICE_PROPS = ["Id", "ActiveState", "Result", "ExecMainCode", "ExecMainStatus",
                 "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic",
                 "UnitFileState"]
SHOW_PROPS = sorted(set(TIMER_PROPS) | set(SERVICE_PROPS))


class Unavailable(Exception):
    """systemctl が無い・マネージャーに繋がらないなど、一覧そのものが取れない。"""


def run_command(argv: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
    except FileNotFoundError:
        return 127, "", f"{argv[0]} が見つかりません"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} が時間内に終わりませんでした"
    return p.returncode, p.stdout, p.stderr


def _systemctl(user: bool) -> list[str]:
    return ["systemctl", "--user"] if user else ["systemctl"]


# ---- list-timers ---------------------------------------------------------

def parse_list_json(text: str) -> list[dict]:
    """`list-timers --output=json`。next/last は µs のエポック、0 か null は「無し」。

    left/passed は版によって中身が揺れる(Arch の 262 では left に絶対時刻が入る)
    ので使わず、残り・経過は表示のたびに今の時刻から計算する。
    """
    out = []
    for e in json.loads(text):
        out.append({
            "timer": e["unit"],
            "service": e.get("activates") or None,
            "next": e["next"] / 1e6 if e.get("next") else None,
            "last": e["last"] / 1e6 if e.get("last") else None,
        })
    return out


_DT = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _local_dt(s: str) -> float | None:
    m = _DT.search(s)
    if not m:
        return None
    # systemctl もこのプロセスも同じ TZ で動くので、地方時として読めば合う
    return time.mktime(time.strptime(m.group(0), "%Y-%m-%d %H:%M:%S"))


def parse_list_table(text: str) -> list[dict]:
    """JSON が使えない systemd 向け。表の見出しの位置から LAST 列の始まりを知る。

    NEXT・LAST・UNIT・ACTIVATES は左寄せ、LEFT・PASSED は右寄せで並ぶ。
    UNIT と ACTIVATES は行の最後の2語。
    """
    lines = text.splitlines()
    try:
        h = next(i for i, l in enumerate(lines) if l.startswith("NEXT"))
    except StopIteration:
        return []
    header = lines[h]
    last_col, unit_col = header.index("LAST"), header.index("UNIT")
    out = []
    for line in lines[h + 1:]:
        if not line.strip():
            break
        words = line.split()
        if len(words) < 2:
            continue
        out.append({
            "timer": words[-2],
            "service": words[-1] if words[-1] not in ("-", "n/a") else None,
            "next": _local_dt(line[:last_col]),
            "last": _local_dt(line[last_col:unit_col]),
        })
    return out


# ---- show ----------------------------------------------------------------

def parse_show(text: str) -> list[dict[str, str]]:
    """`systemctl show a b c -p ...` を、引数の順の辞書の並びにする。

    ユニットごとに空行で区切られる。Id で引かずに順番で対応させるのは、
    別名のサービスだと Id が頼んだ名前と違って返るため。
    同じ名前の行(TimersMonotonic など)は改行でつなぐ。
    """
    blocks, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = cur[k] + "\n" + v if k in cur else v
    if cur:
        blocks.append(cur)
    return blocks


_TIMER_SPEC = re.compile(r"\{\s*(\w+)=(.*?)\s*;\s*next_elapse=")


def parse_schedule(t: dict[str, str]) -> list[str]:
    """TimersCalendar / TimersMonotonic を `OnCalendar=...` `OnBootSec=15min` の形に。"""
    out = []
    for key in ("TimersCalendar", "TimersMonotonic"):
        for line in t.get(key, "").splitlines():
            m = _TIMER_SPEC.search(line)
            if m:
                name = m.group(1)
                if name.endswith("USec"):  # D-Bus 上の名前 → unit ファイルでの書き方
                    name = name[:-4] + "Sec"
                out.append(f"{name}={m.group(2)}")
    return out


def _int(s: str | None) -> int:
    try:
        return int(s or 0)
    except ValueError:
        return 0


def classify(svc: dict[str, str], last: float | None) -> dict:
    """前回の結果。state は ok / failed / running / never / unknown。"""
    start = _int(svc.get("ExecMainStartTimestampMonotonic"))
    exit_ = _int(svc.get("ExecMainExitTimestampMonotonic"))
    result = svc.get("Result") or None
    active = svc.get("ActiveState", "")
    r = {"result": result, "status": None, "duration": None}
    if active in ("active", "activating", "reloading") and start and not exit_:
        return {**r, "state": "running"}
    if not start:
        # このブートでは動いていない。Persistent なタイマーは前回時刻だけ残る
        if active == "failed" or (result and result != "success"):
            return {**r, "state": "failed"}  # 起動前に落ちた(条件・依存など)
        return {**r, "result": None, "state": "never" if last is None else "unknown"}
    r["status"] = _int(svc.get("ExecMainStatus"))
    if exit_ >= start:
        r["duration"] = (exit_ - start) / 1e6
    failed = active == "failed" or (result not in (None, "success"))
    return {**r, "state": "failed" if failed else "ok"}


def collect(user: bool = False, runner=run_command) -> list[dict]:
    """タイマーの一覧。取れなければ Unavailable。"""
    base = _systemctl(user)
    rc, out, err = runner(base + ["list-timers", "--all", "--output=json"])
    entries = None
    if rc == 0 and out.strip():
        try:
            entries = parse_list_json(out)
        except (ValueError, KeyError, TypeError):
            entries = None  # 古い systemd は --output を無視して表を出す
    if entries is None:
        rc, out, err = runner(base + ["list-timers", "--all"])
        # ユーザーのバスに繋がらないとき、版によっては終了コードが 0 のまま
        # 何も出さないので、出力が空なのも失敗として扱う
        if rc != 0 or not out.strip():
            raise Unavailable((err or out).strip() or f"systemctl が終了コード {rc} で終わりました")
        entries = parse_list_table(out)
    if not entries:
        return []

    names = []
    for e in entries:
        names.append(e["timer"])
        names.append(e["service"] or e["timer"])  # 起動先が無いときは数合わせ
    rc, out, err = runner(base + ["show", *names, "-p", ",".join(SHOW_PROPS)])
    blocks = parse_show(out) if rc == 0 else []
    if len(blocks) != len(names):
        raise Unavailable("systemctl show の結果が読めません: " + (err.strip() or "件数が合わない"))

    rows = []
    for i, e in enumerate(entries):
        t, s = blocks[2 * i], blocks[2 * i + 1]
        service = e["service"] or t.get("Unit") or None
        rows.append({
            "timer": e["timer"],
            "service": service,
            "schedule": parse_schedule(t),
            "next": e["next"],
            "last": e["last"],
            "unit_file_state": t.get("UnitFileState") or None,
            **classify(s if e["service"] else {}, e["last"]),
        })
    return rows


def journal_argv(service: str, user: bool) -> list[str]:
    base = ["journalctl", "--user"] if user else ["journalctl"]
    return base + ["-u", service, "-n", "30", "--no-pager", "-o", "short-iso"]


# ---- 表示 ----------------------------------------------------------------

def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def clip(s: str, w: int) -> str:
    """表示幅 w に収める(はみ出すときは末尾を … に)。"""
    if width(s) <= w:
        return s
    out, used = "", 0
    for c in s:
        cw = width(c)
        if used + cw > w - 1:
            break
        out += c
        used += cw
    return out + "…"


def pad(s: str, w: int, right: bool = False) -> str:
    s = clip(s, w)
    fill = " " * (w - width(s))
    return fill + s if right else s + fill


def span(sec: float | None) -> str:
    """残り・経過・かかった時間。"""
    if sec is None:
        return "-"
    sec = max(0, sec)
    if sec < 10:
        return f"{sec:.1f}秒" if sec % 1 else f"{int(sec)}秒"
    sec = int(sec)
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分{sec % 60}秒" if sec < 600 and sec % 60 else f"{sec // 60}分"
    if sec < 86400:
        return f"{sec // 3600}時間{sec % 3600 // 60}分"
    return f"{sec // 86400}日{sec % 86400 // 3600}時間"


def stamp(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


FILE_STATE = {"enabled": "有効", "enabled-runtime": "有効(一時)", "disabled": "無効",
              "static": "静的", "masked": "マスク", "transient": "一時", "generated": "生成",
              "indirect": "間接", "linked": "リンク"}


def result_text(r: dict) -> str:
    st = r["state"]
    if st == "ok":
        return "成功"
    if st == "running":
        return "実行中"
    if st == "never":
        return "未実行"
    if st == "unknown":
        return "記録なし"  # 前回は再起動より前で、結果がもう残っていない
    if r["result"] == "exit-code":
        return f"失敗(終了コード{r['status']})"
    if r["result"] == "signal":
        return f"失敗(シグナル{r['status']})"
    return f"失敗({r['result'] or 'failed'})"


# (見出し, 右寄せか, 最大幅, 狭いときに消す順。小さいほど先に消す。None は消さない)
COLUMNS = [
    ("タイマー", False, 32, None),
    ("サービス", False, 32, 1),
    ("スケジュール", False, 28, 3),
    ("次回", False, 11, 6),
    ("残り", True, 12, 8),
    ("前回", False, 11, 5),
    ("経過", True, 12, 7),
    ("結果", False, 20, None),
    ("時間", True, 10, 4),
    ("有効", False, 10, 2),
]


def cells(r: dict, now: float) -> list[str]:
    return [
        r["timer"],
        r["service"] or "-",
        ", ".join(r["schedule"]) or "-",
        stamp(r["next"]),
        span(r["next"] - now) if r["next"] else "-",
        stamp(r["last"]),
        span(now - r["last"]) if r["last"] else "-",
        result_text(r),
        span(r["duration"]),
        FILE_STATE.get(r["unit_file_state"] or "", r["unit_file_state"] or "-"),
    ]


def table(rows: list[dict], now: float, max_width: int | None = None) -> list[str]:
    """見出し行+1タイマー1行。max_width を超えるなら消してよい列から外す。"""
    data = [cells(r, now) for r in rows]
    widths = [min(c[2], max([width(c[0])] + [width(d[i]) for d in data]))
              for i, c in enumerate(COLUMNS)]
    keep = list(range(len(COLUMNS)))
    if max_width:
        for i in sorted((i for i, c in enumerate(COLUMNS) if c[3]), key=lambda i: COLUMNS[i][3]):
            if sum(widths[k] for k in keep) + 2 * (len(keep) - 1) <= max_width:
                break
            keep.remove(i)

    def line(vals):
        return "  ".join(pad(vals[k], widths[k], COLUMNS[k][1]) for k in keep).rstrip()

    return [line([c[0] for c in COLUMNS])] + [line(d) for d in data]


def to_json(rows: list[dict], now: float) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            **r,
            "left_sec": round(r["next"] - now, 3) if r["next"] else None,
            "passed_sec": round(now - r["last"], 3) if r["last"] else None,
            "result_text": result_text(r),
        })
    return out
