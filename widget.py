"""Claude 사용량 트레이 위젯 (진입점: 화면·트레이·타이머·자동 실행).

작업 표시줄 트레이 아이콘을 누르면 등록된 모든 Claude 계정의 사용량을 보여준다.
계정 = Claude Code 설정 폴더(CLAUDE_CONFIG_DIR) 하나. 그 안의 .credentials.json 토큰을 사용한다.
구조는 DESIGN.md 참고.
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import pystray
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageGrab, ImageTk

import api
import core
import storage
from storage import log

# 팝업 배경: 열 때 뒤 화면을 캡처해 흐리게 만든 뒤 반투명 색을 덮는다 (macOS 위젯 느낌).
# Windows의 "투명 효과" 설정이 꺼져 있어도 동작하도록 시스템 아크릴 대신 직접 블러한다.
BLUR_RADIUS = 28
PALETTES = {
    "light": {
        "fg": "#1d1d1f", "dim": "#4a4844", "solid": "#f3f2ee",
        "tint": (255, 255, 255, 175), "track": (0, 0, 0, 40), "line": (0, 0, 0, 35),
        "ok": "#cc5f3c", "warn": "#b87400", "bad": "#d03535", "sat": 1.4,
    },
    "dark": {
        "fg": "#f2f1ee", "dim": "#b5b1a9", "solid": "#232325",
        "tint": (26, 26, 28, 195), "track": (255, 255, 255, 45), "line": (255, 255, 255, 40),
        "ok": "#e98d6e", "warn": "#e3a633", "bad": "#ff6f6f", "sat": 1.2,
    },
}
ICON_COLORS = {"ok": "#d97757", "warn": "#d39400", "bad": "#d03535", "error": "#8a8a8a"}
THEME_LABELS = {"auto": "Windows 설정 따름", "light": "밝게", "dark": "어둡게"}
ICON_LABELS = {"percent": "가장 높은 사용률 숫자", "plain": "고정 아이콘 (C)"}
SPARK_SPAN = 24 * 3600
RETRY_STEPS = (15, 30, 60, 120)


# ───────────────────────── Windows 헬퍼 ─────────────────────────

def system_theme():
    """Windows 앱 모드(밝게/어둡게)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def make_icon(mode="plain", pct=None, error=False):
    """트레이 아이콘. percent 모드면 가장 높은 사용률을 숫자와 색으로 보여 준다."""
    if error:
        color, label = ICON_COLORS["error"], "!"
    elif mode == "percent" and pct is not None:
        color, label = ICON_COLORS[core.level(pct)], f"{min(pct, 100):.0f}"
    else:
        color, label = ICON_COLORS["ok"], "C"
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), 14, fill=color)
    try:
        font = ImageFont.truetype("arialbd.ttf", {1: 46, 2: 42}.get(len(label), 30))
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 31), label, font=font, fill="white", anchor="mm")
    return img


def round_corners(root):
    """Windows 11: 테두리 없는 창의 모서리를 둥글게."""
    root.update_idletasks()
    user32 = ctypes.windll.user32
    user32.GetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    corner = ctypes.c_int(2)  # DWMWCP_ROUND
    ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(corner), 4)


def blurred(img, pal):
    """화면 캡처를 흐리게 하고 반투명 색을 덮는다. 축소 후 블러해 빠르게 처리."""
    w, h = img.size
    small = img.convert("RGB").resize((max(w // 4, 1), max(h // 4, 1)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(BLUR_RADIUS / 4))
    out = ImageEnhance.Color(small.resize((w, h), Image.BICUBIC)).enhance(pal["sat"]).convert("RGBA")
    return Image.alpha_composite(out, Image.new("RGBA", (w, h), pal["tint"]))


def pin_tray_icon():
    """Windows 11: 트레이 아이콘을 '^' 숨김 영역이 아니라 작업 표시줄에 바로 보이게 한다.

    처음 한 번만 적용한다(이후 사용자가 직접 숨기면 그 선택을 존중).
    """
    flag = storage.path("tray-pinned")
    if flag.exists():
        return
    exe = os.path.normcase(getattr(sys, "_base_executable", None) or sys.executable)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, i), 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
                    try:
                        path = winreg.QueryValueEx(k, "ExecutablePath")[0]
                    except OSError:
                        continue
                    if os.path.normcase(path) == exe:
                        winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        storage.APP_DIR.mkdir(parents=True, exist_ok=True)
                        flag.touch()
    except OSError:
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ClaudeUsageWidget"


def autostart_command():
    """로그인할 때 실행할 명령. exe 로 묶였으면 exe 자신, 아니면 pythonw 로 이 파일."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0] == autostart_command()
    except OSError:
        return False


def set_autostart(on):
    """Windows 로그인 시 자동 실행을 켜거나 끈다 (현재 사용자 Run 레지스트리)."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


_mutex = None


def single_instance():
    """이미 실행 중인 위젯이 있으면 False."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex = kernel32.CreateMutexW(None, False, "ClaudeUsageWidget.SingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def work_area():
    rect = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


# ───────────────────────── 앱 ─────────────────────────

class App:
    dialog_open = False

    def __init__(self):
        self.settings = storage.load_settings()
        self.accounts = storage.load_accounts()
        self.alert_state = storage.load_state()
        storage.prune_history()
        self.history = storage.load_history(time.time() - SPARK_SPAN)
        self.results = []
        self.last_fetch = 0.0
        self.fetching = False
        self.hidden_at = 0.0
        self.retry_n = 0
        self.retry_id = None
        self.timer_id = None
        self.events = queue.Queue()
        self.pal = PALETTES[self._theme()]

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self.pal["solid"])
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)
        self.root.report_callback_exception = lambda *exc: log.error("화면 콜백 예외", exc_info=exc)
        self.scale = self.root.winfo_fpixels("1i") / 96  # DPI 배율
        self.canvas = tk.Canvas(self.root, bg=self.pal["solid"], highlightthickness=0, bd=0)
        self.canvas.pack()
        self.label_font = tkfont.Font(root=self.root, family="Malgun Gothic", size=9)
        self.backdrop = None   # 팝업을 열 때 캡처한 (흐린) 뒤 화면
        self.bg_photo = None
        try:
            round_corners(self.root)
        except Exception:  # noqa: BLE001 - Windows 10 등 미지원 환경은 각진 모서리
            pass

        ev = self._ev
        menu = pystray.Menu(
            pystray.MenuItem("사용량 보기", ev("toggle"), default=True),
            pystray.MenuItem("새로고침", ev("refresh")),
            pystray.MenuItem("계정 관리", pystray.Menu(self._account_menu)),
            pystray.MenuItem("설정…", ev("settings")),
            pystray.MenuItem("Windows 시작 시 자동 실행",
                             lambda: set_autostart(not autostart_enabled()),
                             checked=lambda item: autostart_enabled()),
            pystray.MenuItem("로그 파일 열기", lambda: os.startfile(storage.path("widget.log"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", ev("quit")),
        )
        self.tray = pystray.Icon("claude-usage", make_icon(), "Claude 사용량", menu)
        self.tray.run_detached()
        self.root.after(3000, pin_tray_icon)  # 아이콘이 Windows에 등록된 뒤 적용

        self.refresh()
        self.root.after(100, self._pump)
        self._schedule()
        log.info("시작 (계정 %d개)", len(self.accounts))

    # ── 트레이 메뉴 ──
    def _ev(self, *payload):
        """트레이 스레드에서 호출돼 이벤트만 큐에 넣는 콜백을 만든다 (인자 없는 함수여야 함)."""
        return lambda: self.events.put(payload)

    def _account_menu(self):
        ev, accounts = self._ev, self.accounts
        items = []
        for i, a in enumerate(accounts):
            n = a["name"]
            items.append(pystray.MenuItem(n, pystray.Menu(
                pystray.MenuItem("이름 변경…", ev("rename", n)),
                pystray.MenuItem("위로", ev("move", n, -1), enabled=i > 0),
                pystray.MenuItem("아래로", ev("move", n, 1), enabled=i < len(accounts) - 1),
                pystray.MenuItem("목록에서 삭제…", ev("remove", n)),
            )))
        if items:
            items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("계정 추가…", ev("add")))
        items.append(pystray.MenuItem("계정 목록 파일 열기", lambda: os.startfile(storage.path("accounts.json"))))
        return items

    # 트레이 스레드 → Tk 메인 스레드로 이벤트 전달
    def _pump(self):
        try:
            while True:
                ev, *args = self.events.get_nowait()
                if ev == "quit":
                    log.info("종료")
                    self.tray.stop()
                    self.root.destroy()
                    return
                try:
                    {"toggle": self.toggle, "refresh": self.refresh, "add": self.add_account,
                     "done": self._on_fetched, "settings": self.open_settings,
                     "rename": self.rename_account, "move": self.move_account,
                     "remove": self.remove_account}[ev](*args)
                except Exception:  # noqa: BLE001 - 한 번의 실패로 위젯이 멈추지 않게
                    log.exception("이벤트 처리 실패: %s", ev)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    # ── 갱신 ──
    def _schedule(self):
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
        self.timer_id = self.root.after(self.settings["refresh_sec"] * 1000, self._auto_refresh)

    def _auto_refresh(self):
        self.timer_id = None
        self.refresh()
        self._schedule()

    def refresh(self):
        if self.fetching:
            return
        self.fetching = True
        self.accounts = storage.load_accounts()
        if self.root.state() == "normal":
            self.render()
        accounts, allow = list(self.accounts), self.settings["token_refresh"]

        def work():
            results = [None] * len(accounts)

            def one(i, acc):
                try:
                    results[i] = api.fetch_account(acc, allow)
                except Exception as e:  # noqa: BLE001
                    log.exception("조회 실패: %s", acc.get("name"))
                    results[i] = {"name": acc.get("name", "?"), "error": str(e) or type(e).__name__}

            threads = [threading.Thread(target=one, args=(i, a)) for i, a in enumerate(accounts)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.events.put(("done", results))

        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self, results):
        self.fetching = False
        self.results = results
        self.last_fetch = time.time()
        ok = [r for r in results if "error" not in r]

        self.tray.title = core.tooltip(results)
        self.tray.icon = make_icon(self.settings["icon_mode"], core.max_pct(results),
                                   error=bool(results) and not ok)

        # 추이 기록
        self.history += storage.append_history(results)
        cutoff = time.time() - SPARK_SPAN
        self.history = [h for h in self.history if h["t"] >= cutoff]

        # 알림
        msgs, state = [], self.alert_state
        for r in ok:
            m, state = core.check_alerts(state, r["name"], r["limits"], self.settings["alert_thresholds"],
                                         notify_reset=self.settings["notify_reset"])
            msgs += m
        if state != self.alert_state:
            self.alert_state = state
            storage.save_state(state)
        if msgs and self.settings["notify"]:
            log.info("알림: %s", " / ".join(msgs))
            try:
                self.tray.notify("\n".join(msgs)[:255], "Claude 사용량")
            except Exception:  # noqa: BLE001
                log.exception("알림 표시 실패")

        # 네트워크 오류면 짧은 간격으로 다시 시도 (부팅 직후 대응)
        if self.retry_id:
            self.root.after_cancel(self.retry_id)
            self.retry_id = None
        if any(r.get("retry") for r in results):
            delay = RETRY_STEPS[min(self.retry_n, len(RETRY_STEPS) - 1)]
            self.retry_n += 1
            if delay < self.settings["refresh_sec"]:
                self.retry_id = self.root.after(delay * 1000, self._retry)
        else:
            self.retry_n = 0

        if self.root.state() == "normal":
            self.render()

    def _retry(self):
        self.retry_id = None
        self.refresh()

    # ── 팝업 ──
    def _theme(self):
        t = self.settings["theme"]
        return system_theme() if t == "auto" else t

    def toggle(self):
        if self.root.state() == "normal":
            self.hide()
        elif time.time() - self.hidden_at > 0.3:  # 아이콘 클릭으로 포커스를 잃어 방금 닫힌 경우는 다시 열지 않음
            self.show()

    def show(self):
        if time.time() - self.last_fetch > 60:
            self.refresh()
        self.pal = PALETTES[self._theme()]
        self.root.configure(bg=self.pal["solid"])
        self.canvas.configure(bg=self.pal["solid"])
        self._capture_backdrop()  # 창이 숨겨진 상태에서 뒤 화면을 캡처
        self.render()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _capture_backdrop(self):
        """작업 영역 오른쪽 아래(팝업이 뜰 자리)를 넉넉히 캡처해 흐리게 만들어 둔다."""
        left, top, right, bottom = work_area()
        w = min(int(460 * self.scale), right - left)
        h = min(int(1000 * self.scale), bottom - top)
        try:
            self.backdrop = blurred(ImageGrab.grab((right - w, bottom - h, right, bottom)), self.pal)
        except Exception:  # noqa: BLE001 - 캡처 불가 시 불투명 배경
            self.backdrop = None

    def hide(self):
        self.root.withdraw()
        self.hidden_at = time.time()

    def _on_focus_out(self, _):
        # 대화상자(계정 추가·설정)가 떠 있는 동안은 닫지 않는다
        self.root.after(50, lambda: self.root.focus_displayof() is None and not self.dialog_open and self.hide())

    def render(self):
        """캔버스 한 장에 전부 그린다. 글자는 캔버스 텍스트(배경 투명), 선·게이지는 배경 이미지에 합성."""
        c, s, pal = self.canvas, self.scale, self.pal
        FG, DIM = pal["fg"], pal["dim"]
        px = lambda v: int(round(v * s))  # noqa: E731
        c.delete("all")
        W, pad, margin = px(350), px(18), px(12)
        x0, x1 = pad, W - pad
        shapes = []  # (종류, 좌표, 색) — 반투명이 필요해 PIL로 배경에 그린다

        def text(x, y, t, color=FG, font=("Malgun Gothic", 9), anchor="nw", **kw):
            item = c.create_text(x, y, text=t, fill=color, font=font, anchor=anchor, **kw)
            return item, c.bbox(item)

        y = px(14)
        _, bb = text(x0, y, "Claude 사용량", font=("Malgun Gothic", 11, "bold"))
        mid = (bb[1] + bb[3]) // 2
        btn, bbb = text(x1, mid, "⟳", DIM, ("Segoe UI Symbol", 12), "e")
        c.tag_bind(btn, "<Button-1>", lambda e: self.refresh())
        c.tag_bind(btn, "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind(btn, "<Leave>", lambda e: c.config(cursor=""))
        status = "갱신 중…" if self.fetching else (
            datetime.fromtimestamp(self.last_fetch).strftime("%H:%M 기준") if self.last_fetch else "")
        text(bbb[0] - px(8), mid, status, DIM, ("Malgun Gothic", 8), "e")
        y = bb[3] + px(10)

        rows = self.results if self.results else [{"name": a["name"], "loading": True} for a in self.accounts]
        if not rows:
            _, bb = text(x0, y, "계정이 없습니다. 트레이 메뉴 → 계정 관리에서 추가하세요.", DIM, width=x1 - x0)
            y = bb[3] + px(12)
        # 사용률 숫자 열 위치: 가장 긴 한도 이름에 맞춘다
        labels = [lim["label"] for r in rows for lim in r.get("limits", [])]
        pct_x = x0 + max([self.label_font.measure(t) for t in labels] or [0]) + px(10)
        now_ts = time.time()

        for r in rows:
            shapes.append(("line", (x0, y, x1, y), pal["line"]))
            y += px(11)
            _, bb = text(x0, y, r["name"], font=("Malgun Gothic", 10, "bold"))
            mid = (bb[1] + bb[3]) // 2
            if r.get("plan"):
                text(bb[2] + px(8), mid, r["plan"], pal["ok"], ("Segoe UI", 8, "bold"), "w")
            # 5시간 한도의 최근 24시간 추이
            sw, sh = px(72), px(14)
            pts = core.spark_points(self.history, r["name"], "session", now_ts, SPARK_SPAN, sw, sh)
            if pts:
                sx, sy = x1 - sw, mid - sh // 2
                shapes.append(("spark", [(sx + x, sy + yy) for x, yy in pts], DIM))
                text(sx - px(5), mid, "24h", DIM, ("Segoe UI", 7), "e")
            y = bb[3]
            if r.get("loading"):
                _, bb = text(x0, y + px(6), "불러오는 중…", DIM)
                y = bb[3]
            elif "error" in r:
                _, bb = text(x0, y + px(6), "⚠ " + r["error"], pal["warn"], width=x1 - x0)
                y = bb[3]
            else:
                for lim in r["limits"]:
                    pct = lim["pct"]
                    color = pal[core.level(pct)]
                    _, bb = text(x0, y + px(9), lim["label"])
                    mid = (bb[1] + bb[3]) // 2
                    text(pct_x, mid, f"{pct:.0f}%", color, ("Segoe UI", 10, "bold"), "w")
                    text(x1, mid, core.fmt_reset(lim["resets_at"], lim["window"] == "weekly"),
                         DIM, ("Malgun Gothic", 8), "e")
                    y = bb[3] + px(5)
                    bar_h = px(6)
                    shapes.append(("bar", (x0, y, x1, y + bar_h), pal["track"]))
                    if pct > 0:
                        fill_w = max((x1 - x0) * min(pct, 100) / 100, bar_h)
                        shapes.append(("bar", (x0, y, x0 + fill_w, y + bar_h), color))
                    y += bar_h
                    hit = core.forecast(pct, lim["resets_at"], lim["window"])
                    if hit:
                        _, bb = text(x0, y + px(3), f"이 속도면 {core.fmt_when(hit)} 소진",
                                     pal["warn"], ("Malgun Gothic", 8))
                        y = bb[3]
                extra = []
                if r.get("breakdown"):
                    extra.append("7일 사용처  " + " · ".join(f"{n} {p:.0f}%" for n, p in r["breakdown"]))
                if r.get("spend"):
                    extra.append(core.fmt_spend(r["spend"]))
                for line in extra:
                    _, bb = text(x0, y + px(7), line, DIM, ("Malgun Gothic", 8), width=x1 - x0)
                    y = bb[3] - px(3)
                if extra:
                    y += px(3)
            y += px(12)
        H = y + px(4)

        # 배경: 흐린 뒤 화면에서 팝업 자리만 잘라내고 선·게이지를 반투명으로 합성
        if self.backdrop:
            bw, bh = self.backdrop.size
            box = (bw - margin - W, bh - margin - H, bw - margin, bh - margin)
            bg = self.backdrop.crop(box)
        else:
            bg = Image.new("RGBA", (W, H), pal["solid"])
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for kind, xy, color in shapes:
            if kind == "line":
                d.line(xy, fill=color, width=max(px(1), 1))
            elif kind == "spark":
                d.line(xy, fill=color, width=max(px(1.4), 1), joint="curve")
            else:
                d.rounded_rectangle(xy, radius=(xy[3] - xy[1]) / 2, fill=color)
        self.bg_photo = ImageTk.PhotoImage(Image.alpha_composite(bg, overlay))
        c.tag_lower(c.create_image(0, 0, image=self.bg_photo, anchor="nw"))

        c.config(width=W, height=H)
        _, _, right, bottom = work_area()
        self.root.geometry(f"{W}x{H}+{right - W - margin}+{bottom - H - margin}")

    # ── 계정 관리 ──
    def _accounts_changed(self, accounts):
        storage.save_accounts(accounts)
        self.accounts = accounts
        self.results = []
        self.tray.update_menu()
        self.refresh()

    def add_account(self):
        self.dialog_open = True
        try:
            name = simpledialog.askstring("계정 추가", "계정 이름 (예: 회사, 개인2):", parent=self.root)
            if not name or not name.strip():
                return
            name = name.strip()
            accounts = storage.load_accounts()
            if any(a["name"] == name for a in accounts):
                messagebox.showerror("계정 추가", "이미 있는 이름입니다.", parent=self.root)
                return
            slug = re.sub(r"[^\w\-]+", "_", name)
            cfg = storage.path("accounts") / slug
            cfg.mkdir(parents=True, exist_ok=True)
            self._accounts_changed(accounts + [{"name": name, "dir": str(cfg)}])
            # 이 계정 전용 설정 폴더로 Claude Code를 띄워 로그인하게 한다
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))
            subprocess.Popen(["cmd", "/c", "start", "Claude 로그인 - " + name, "cmd", "/k", "claude"], env=env)
            messagebox.showinfo(
                "계정 추가",
                "새 터미널에서 Claude Code가 열립니다.\n추가할 계정으로 로그인한 뒤 터미널을 닫고,\n위젯에서 새로고침(⟳)을 누르세요.",
                parent=self.root)
        finally:
            self.dialog_open = False

    def rename_account(self, name):
        self.dialog_open = True
        try:
            new = simpledialog.askstring("이름 변경", f"'{name}' 의 새 이름:", initialvalue=name, parent=self.root)
            if not new or new.strip() == name:
                return
            try:
                self._accounts_changed(core.rename_account(storage.load_accounts(), name, new))
            except ValueError as e:
                messagebox.showerror("이름 변경", str(e), parent=self.root)
        finally:
            self.dialog_open = False

    def move_account(self, name, delta):
        self._accounts_changed(core.move_account(storage.load_accounts(), name, delta))

    def remove_account(self, name):
        self.dialog_open = True
        try:
            if messagebox.askyesno(
                    "계정 삭제",
                    f"'{name}' 을(를) 목록에서 뺄까요?\n로그인 정보가 든 폴더는 지우지 않습니다.",
                    parent=self.root):
                self._accounts_changed(core.remove_account(storage.load_accounts(), name))
        finally:
            self.dialog_open = False

    # ── 설정 창 ──
    def open_settings(self):
        if self.dialog_open:
            return
        self.dialog_open = True
        s = self.settings
        win = tk.Toplevel(self.root)
        win.title("Claude 사용량 설정")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        frm = ttk.Frame(win, padding=16)
        frm.pack()

        theme = tk.StringVar(value=THEME_LABELS[s["theme"]])
        icon = tk.StringVar(value=ICON_LABELS[s["icon_mode"]])
        minutes = tk.StringVar(value=str(max(s["refresh_sec"] // 60, 1)))
        thresholds = tk.StringVar(value=", ".join(map(str, s["alert_thresholds"])))
        notify = tk.BooleanVar(value=s["notify"])
        notify_reset = tk.BooleanVar(value=s["notify_reset"])
        token = tk.BooleanVar(value=s["token_refresh"])
        autostart = tk.BooleanVar(value=autostart_enabled())

        def row(i, label, widget):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=i, column=1, sticky="we", pady=4)

        row(0, "테마", ttk.Combobox(frm, textvariable=theme, state="readonly", width=22,
                                  values=list(THEME_LABELS.values())))
        row(1, "트레이 아이콘", ttk.Combobox(frm, textvariable=icon, state="readonly", width=22,
                                        values=list(ICON_LABELS.values())))
        row(2, "자동 갱신 주기 (분)", ttk.Spinbox(frm, textvariable=minutes, from_=1, to=60, width=6))
        row(3, "알림 기준 (%)", ttk.Entry(frm, textvariable=thresholds, width=24))
        checks = (("한도에 가까워지면 알림", notify), ("알렸던 한도가 리셋되면 알림", notify_reset),
                  ("만료된 토큰을 위젯이 직접 갱신", token), ("Windows 시작 시 자동 실행", autostart))
        for i, (label, var) in enumerate(checks, start=4):
            ttk.Checkbutton(frm, text=label, variable=var).grid(row=i, column=0, columnspan=2, sticky="w", pady=2)

        def close():
            self.dialog_open = False
            win.destroy()

        def save():
            try:
                ths = core.parse_thresholds(thresholds.get())
                mins = int(minutes.get())
                if not 1 <= mins <= 60:
                    raise ValueError
            except ValueError:
                messagebox.showerror("설정", "갱신 주기는 1~60 분, 알림 기준은 1~100 사이 숫자를\n쉼표로 구분해 적어 주세요.",
                                     parent=win)
                return
            rev = lambda labels, v: next(k for k, t in labels.items() if t == v)  # noqa: E731
            self.settings = core.normalize_settings({
                "refresh_sec": mins * 60, "theme": rev(THEME_LABELS, theme.get()),
                "icon_mode": rev(ICON_LABELS, icon.get()), "alert_thresholds": ths,
                "notify": notify.get(), "notify_reset": notify_reset.get(), "token_refresh": token.get(),
            })
            storage.save_settings(self.settings)
            set_autostart(autostart.get())
            self._schedule()
            if self.results:
                ok = [r for r in self.results if "error" not in r]
                self.tray.icon = make_icon(self.settings["icon_mode"], core.max_pct(self.results),
                                           error=not ok)
            log.info("설정 저장")
            close()

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="취소", command=close).pack(side="right")
        ttk.Button(btns, text="저장", command=save).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", close)
        win.update_idletasks()
        _, _, right, bottom = work_area()
        win.geometry(f"+{right - win.winfo_width() - 40}+{bottom - win.winfo_height() - 80}")
        win.focus_force()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    if "--once" in sys.argv:  # 디버그: 조회 결과만 출력
        for acc in storage.load_accounts():
            print(json.dumps(api.fetch_account(acc, storage.load_settings()["token_refresh"]), ensure_ascii=False))
        return
    if "--autostart" in sys.argv:  # 자동 실행 등록/해제만 하고 종료: --autostart on|off
        set_autostart(sys.argv[-1] != "off")
        print("자동 실행:", "켜짐" if autostart_enabled() else "꺼짐")
        return
    storage.setup_logging()
    if not single_instance():
        log.info("이미 실행 중이라 종료")
        return
    try:
        App().root.mainloop()
    except Exception:
        log.exception("치명적 오류")
        raise


if __name__ == "__main__":
    main()
