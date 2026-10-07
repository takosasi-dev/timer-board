"""curses の画面。中身の計算は core にあり、ここは描くのとキーだけ。"""

from __future__ import annotations

import curses
import locale
import time

from . import core

REFETCH_SEC = 10  # 一覧の取り直し。残り時間は1秒ごとに描き直して進める
HELP = "j/k 移動  Enter ログ  r 再読込  u システム/ユーザー  f 失敗のみ  q 終了"
SCOPE = {False: "システム", True: "ユーザー"}


def put(win, y: int, x: int, s: str, attr: int = 0, fill: bool = False) -> None:
    """幅に収めて書く。fill は行の端まで空白で埋める(反転の帯用)。"""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        avail = w - x - (1 if y == h - 1 else 0)  # 右下の1桁は書くと curses が怒る
        try:
            win.addstr(y, x, core.pad(s, avail) if fill else core.clip(s, avail), attr)
        except curses.error:
            pass


class Board:
    def __init__(self, user: bool, runner):
        self.user, self.runner = user, runner
        self.rows: list[dict] = []
        self.error = ""
        self.failed_only = False
        self.sel = 0
        self.top = 0
        self.fetched = 0.0

    def fetch(self) -> None:
        vis = self.visible()
        keep = vis[self.sel]["timer"] if self.sel < len(vis) else None
        try:
            self.rows, self.error = core.collect(self.user, self.runner), ""
        except core.Unavailable as e:
            self.rows, self.error = [], f"{SCOPE[self.user]}のタイマーは取れない: {e}"
        self.fetched = time.time()
        names = [r["timer"] for r in self.visible()]
        self.sel = names.index(keep) if keep in names else 0

    def visible(self) -> list[dict]:
        return [r for r in self.rows if r["state"] == "failed"] if self.failed_only else self.rows

    def draw(self, scr, red: int) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        rows, now = self.visible(), time.time()
        nfail = sum(r["state"] == "failed" for r in self.rows)
        title = (f" TimerBoard  {SCOPE[self.user]}  {len(self.rows)}件  失敗 {nfail}  "
                 f"更新 {time.strftime('%H:%M:%S', time.localtime(self.fetched))}"
                 + ("  [失敗のみ]" if self.failed_only else ""))
        put(scr, 0, 0, title, curses.A_REVERSE, fill=True)
        if self.error:
            put(scr, 2, 1, self.error, red)
        elif not rows:
            put(scr, 2, 1, "失敗したタイマーはありません" if self.failed_only else "タイマーはありません")
        else:
            lines = core.table(rows, now, w - 2)
            put(scr, 1, 1, lines[0], curses.A_BOLD)
            body = max(1, h - 3)
            self.sel = min(self.sel, len(rows) - 1)
            self.top = min(max(self.top, self.sel - body + 1), self.sel)
            for i in range(self.top, min(len(rows), self.top + body)):
                attr = red if rows[i]["state"] == "failed" else 0
                if i == self.sel:
                    attr |= curses.A_REVERSE
                put(scr, 2 + i - self.top, 0, " " + lines[i + 1], attr, fill=True)
        put(scr, h - 1, 0, " " + HELP, curses.A_DIM)
        scr.refresh()

    def show_log(self, scr) -> None:
        rows = self.visible()
        if not rows or not rows[self.sel]["service"]:
            return
        svc = rows[self.sel]["service"]
        rc, out, err = self.runner(core.journal_argv(svc, self.user))
        lines = (out or err or "(ログはありません)").rstrip("\n").splitlines()
        top = max(0, len(lines) - (scr.getmaxyx()[0] - 2))
        while True:
            scr.erase()
            h, w = scr.getmaxyx()
            body = max(1, h - 2)
            put(scr, 0, 0, f" {svc} の直近のログ ({len(lines)}行)", curses.A_REVERSE, fill=True)
            for y, l in enumerate(lines[top:top + body]):
                put(scr, 1 + y, 0, l)
            put(scr, h - 1, 0, " j/k スクロール  q/Esc/Enter 戻る", curses.A_DIM)
            scr.refresh()
            k = scr.getch()
            if k in (ord("q"), 27, 10, 13, curses.KEY_ENTER, curses.KEY_LEFT):
                return
            if k in (ord("j"), curses.KEY_DOWN):
                top = min(top + 1, max(0, len(lines) - body))
            elif k in (ord("k"), curses.KEY_UP):
                top = max(0, top - 1)


def _loop(scr, board: Board) -> int:
    curses.curs_set(0)
    red = 0
    if curses.has_colors():
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        red = curses.color_pair(1) | curses.A_BOLD
    scr.timeout(1000)
    board.fetch()
    while True:
        if time.time() - board.fetched >= REFETCH_SEC:
            board.fetch()
        board.draw(scr, red)
        k = scr.getch()
        n = len(board.visible())
        if k in (ord("q"), ord("Q")):
            return 0
        if k in (ord("j"), curses.KEY_DOWN) and n:
            board.sel = min(board.sel + 1, n - 1)
        elif k in (ord("k"), curses.KEY_UP):
            board.sel = max(board.sel - 1, 0)
        elif k in (10, 13, curses.KEY_ENTER):
            board.show_log(scr)
        elif k == ord("r"):
            board.fetch()
        elif k == ord("u"):
            board.user, board.sel, board.top = not board.user, 0, 0
            board.fetch()
        elif k == ord("f"):
            board.failed_only, board.sel, board.top = not board.failed_only, 0, 0


def run(user: bool, runner=core.run_command) -> int:
    locale.setlocale(locale.LC_ALL, "")
    return curses.wrapper(_loop, Board(user, runner))
