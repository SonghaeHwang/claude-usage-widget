"""Claude 사용량(5시간 / 7일) 트레이 위젯.

작업 표시줄 트레이 아이콘을 누르면 등록된 모든 Claude 계정의 사용량을 보여준다.
계정 = Claude Code 설정 폴더(CLAUDE_CONFIG_DIR) 하나. 그 안의 .credentials.json 토큰을 사용한다.
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
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog

import pystray
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageGrab, ImageTk

HOME = Path.home()
APP_DIR = HOME / ".claude-usage-widget"
ACCOUNTS_FILE = APP_DIR / "accounts.json"
ACCOUNTS_DIR = APP_DIR / "accounts"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code 공개 OAuth client id
BETA = "oauth-2025-04-20"
REFRESH_SEC = 300  # 자동 갱신 주기

# 팝업 배경: 열 때 뒤 화면을 캡처해 흐리게 만든 뒤 밝은 반투명 색을 덮는다 (macOS 위젯 느낌).
# Windows의 "투명 효과" 설정이 꺼져 있어도 동작하도록 시스템 아크릴 대신 직접 블러한다.
FG, DIM = "#1d1d1f", "#4a4844"
TINT = (255, 255, 255, 175)    # 블러 위에 덮는 색 (R, G, B, 불투명도 0~255)
BLUR_RADIUS = 28               # 블러 세기 (px)
TRACK = (0, 0, 0, 40)          # 게이지 빈 부분
LINE = (0, 0, 0, 35)           # 구분선
SOLID = "#f3f2ee"              # 화면 캡처 실패 시 불투명 배경
OK, WARN, BAD = "#cc5f3c", "#b87400", "#d03535"  # 밝은 배경에서 읽히도록 진하게
ICON_COLOR = "#d97757"
WEEKDAYS = "월화수목금토일"


# ───────────────────────── 계정 / API ─────────────────────────

def load_accounts():
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    accounts = [{"name": "기본", "dir": str(HOME / ".claude")}]
    save_accounts(accounts)
    return accounts


def save_accounts(accounts):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def _http(url, token=None, body=None):
    headers = {"User-Agent": "claude-code/2.0", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = BETA
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _refresh_token(cred_path, creds):
    """만료된 액세스 토큰을 갱신하고, 회전된 리프레시 토큰을 파일에 다시 저장한다."""
    oauth = creds["claudeAiOauth"]
    res = _http(TOKEN_URL, body={
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    })
    oauth["accessToken"] = res["access_token"]
    oauth["refreshToken"] = res.get("refresh_token", oauth["refreshToken"])
    oauth["expiresAt"] = int((time.time() + res.get("expires_in", 3600)) * 1000)
    tmp = cred_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(creds), encoding="utf-8")
    os.replace(tmp, cred_path)
    return oauth["accessToken"]


def fetch_account(acc):
    """계정 하나의 사용량을 조회. {'name','plan','five','week'} 또는 {'name','error'}."""
    out = {"name": acc["name"]}
    cred_path = Path(acc["dir"]).expanduser() / ".credentials.json"
    try:
        if not cred_path.exists():
            raise RuntimeError("로그인 필요 (자격 증명 없음)")
        creds = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth")
        if not oauth:
            raise RuntimeError("구독 계정 로그인이 아님")
        token = oauth["accessToken"]
        if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
            token = _refresh_token(cred_path, creds)
        try:
            usage = _http(USAGE_URL, token)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # 다른 프로세스(Claude Code)가 이미 갱신했을 수 있으니 파일을 다시 읽고 시도
            creds = json.loads(cred_path.read_text(encoding="utf-8"))
            token = creds["claudeAiOauth"]["accessToken"]
            try:
                usage = _http(USAGE_URL, token)
            except urllib.error.HTTPError as e2:
                if e2.code != 401:
                    raise
                usage = _http(USAGE_URL, _refresh_token(cred_path, creds))
        out["plan"] = (oauth.get("subscriptionType") or "").upper()
        out["five"] = usage.get("five_hour")
        out["week"] = usage.get("seven_day")
    except urllib.error.HTTPError as e:
        out["error"] = "재로그인 필요" if e.code in (400, 401, 403) else f"HTTP {e.code}"
    except urllib.error.URLError:
        out["error"] = "네트워크 오류"
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e) or type(e).__name__
    return out


# ───────────────────────── 표시 헬퍼 ─────────────────────────

def level_color(pct):
    return BAD if pct >= 85 else WARN if pct >= 60 else OK


def fmt_reset(iso, with_date):
    if not iso:
        return "—"
    t = datetime.fromisoformat(iso).astimezone()
    left = int((t - datetime.now().astimezone()).total_seconds())
    if left <= 0:
        return "곧 리셋"
    d, rem = divmod(left, 86400)
    h, m = divmod(rem // 60, 60)
    remain = f"{d}일 {h}시간" if d else f"{h}시간 {m}분" if h else f"{m}분"
    at = t.strftime("%m/%d") + f"({WEEKDAYS[t.weekday()]}) " + t.strftime("%H:%M") if with_date else t.strftime("%H:%M")
    return f"{at} 리셋 · {remain} 후"


def make_icon():
    """트레이 아이콘: 항상 같은 모양(주황 바탕에 흰색 C). 수치는 팝업과 툴팁에서 본다."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), 14, fill=ICON_COLOR)
    try:
        font = ImageFont.truetype("arialbd.ttf", 46)
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 31), "C", font=font, fill="white", anchor="mm")
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


def blurred(img):
    """화면 캡처를 흐리게 하고 밝은 반투명 색을 덮는다. 축소 후 블러해 빠르게 처리."""
    w, h = img.size
    small = img.convert("RGB").resize((max(w // 4, 1), max(h // 4, 1)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(BLUR_RADIUS / 4))
    out = ImageEnhance.Color(small.resize((w, h), Image.BICUBIC)).enhance(1.4).convert("RGBA")
    return Image.alpha_composite(out, Image.new("RGBA", (w, h), TINT))


def pin_tray_icon():
    """Windows 11: 트레이 아이콘을 '^' 숨김 영역이 아니라 작업 표시줄에 바로 보이게 한다.

    처음 한 번만 적용한다(이후 사용자가 직접 숨기면 그 선택을 존중).
    """
    flag = APP_DIR / "tray-pinned"
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
                        APP_DIR.mkdir(parents=True, exist_ok=True)
                        flag.touch()
    except OSError:
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ClaudeUsageWidget"


def autostart_command():
    """로그인할 때 실행할 명령: 콘솔 창이 없는 pythonw 로 이 파일을 실행한다."""
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


def work_area():
    rect = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


# ───────────────────────── 앱 ─────────────────────────

class App:
    def __init__(self):
        self.accounts = load_accounts()
        self.results = []
        self.last_fetch = 0.0
        self.fetching = False
        self.hidden_at = 0.0
        self.events = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=SOLID)
        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)
        self.scale = self.root.winfo_fpixels("1i") / 96  # DPI 배율
        self.canvas = tk.Canvas(self.root, bg=SOLID, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.backdrop = None   # 팝업을 열 때 캡처한 (흐린) 뒤 화면
        self.bg_photo = None
        try:
            round_corners(self.root)
        except Exception:  # noqa: BLE001 - Windows 10 등 미지원 환경은 각진 모서리
            pass

        menu = pystray.Menu(
            pystray.MenuItem("사용량 보기", lambda: self.events.put("toggle"), default=True),
            pystray.MenuItem("새로고침", lambda: self.events.put("refresh")),
            pystray.MenuItem("계정 추가…", lambda: self.events.put("add")),
            pystray.MenuItem("계정 목록 파일 열기", lambda: os.startfile(ACCOUNTS_FILE)),
            pystray.MenuItem("Windows 시작 시 자동 실행",
                             lambda: set_autostart(not autostart_enabled()),
                             checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda: self.events.put("quit")),
        )
        self.tray = pystray.Icon("claude-usage", make_icon(), "Claude 사용량", menu)
        self.tray.run_detached()
        self.root.after(3000, pin_tray_icon)  # 아이콘이 Windows에 등록된 뒤 적용

        self.refresh()
        self.root.after(100, self._pump)
        self.root.after(REFRESH_SEC * 1000, self._auto_refresh)

    # 트레이 스레드 → Tk 메인 스레드로 이벤트 전달
    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "toggle":
                    self.toggle()
                elif ev == "refresh":
                    self.refresh()
                elif ev == "add":
                    self.add_account()
                elif ev == "done":
                    self._on_fetched()
                elif ev == "quit":
                    self.tray.stop()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _auto_refresh(self):
        self.refresh()
        self.root.after(REFRESH_SEC * 1000, self._auto_refresh)

    def refresh(self):
        if self.fetching:
            return
        self.fetching = True
        self.accounts = load_accounts()
        if self.root.state() == "normal":
            self.render()

        def work():
            results = [None] * len(self.accounts)

            def one(i, acc):
                results[i] = fetch_account(acc)

            threads = [threading.Thread(target=one, args=(i, a)) for i, a in enumerate(self.accounts)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.results = results
            self.events.put("done")

        threading.Thread(target=work, daemon=True).start()

    def _on_fetched(self):
        self.fetching = False
        self.last_fetch = time.time()
        lines = []
        for r in self.results:
            if "error" in r:
                lines.append(f"{r['name']}: {r['error']}")
            else:
                f, w = r.get("five") or {}, r.get("week") or {}
                lines.append(f"{r['name']}: 5h {f.get('utilization', 0):.0f}% · 7d {w.get('utilization', 0):.0f}%")
        self.tray.title = ("Claude 사용량\n" + "\n".join(lines))[:127]
        if self.root.state() == "normal":
            self.render()

    # ── 팝업 ──
    def toggle(self):
        if self.root.state() == "normal":
            self.hide()
        elif time.time() - self.hidden_at > 0.3:  # 아이콘 클릭으로 포커스를 잃어 방금 닫힌 경우는 다시 열지 않음
            self.show()

    def show(self):
        if time.time() - self.last_fetch > 60:
            self.refresh()
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
            self.backdrop = blurred(ImageGrab.grab((right - w, bottom - h, right, bottom)))
        except Exception:  # noqa: BLE001 - 캡처 불가 시 불투명 배경
            self.backdrop = None

    def hide(self):
        self.root.withdraw()
        self.hidden_at = time.time()

    def _on_focus_out(self, _):
        # 대화상자(계정 추가)가 떠 있는 동안은 닫지 않는다
        self.root.after(50, lambda: self.root.focus_displayof() is None and not self.dialog_open and self.hide())

    dialog_open = False

    def render(self):
        """캔버스 한 장에 전부 그린다. 글자는 캔버스 텍스트(배경 투명), 선·게이지는 배경 이미지에 합성."""
        c, s = self.canvas, self.scale
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
        for r in rows:
            shapes.append(("line", (x0, y, x1, y), LINE))
            y += px(11)
            _, bb = text(x0, y, r["name"], font=("Malgun Gothic", 10, "bold"))
            mid = (bb[1] + bb[3]) // 2
            if r.get("plan"):
                text(bb[2] + px(8), mid, r["plan"], OK, ("Segoe UI", 8, "bold"), "w")
            y = bb[3]
            if r.get("loading"):
                _, bb = text(x0, y + px(6), "불러오는 중…", DIM)
                y = bb[3]
            elif "error" in r:
                _, bb = text(x0, y + px(6), "⚠ " + r["error"], WARN, width=x1 - x0)
                y = bb[3]
            else:
                for label, key, with_date in (("5시간", "five", False), ("7일", "week", True)):
                    data = r.get(key) or {}
                    pct = float(data.get("utilization") or 0)
                    color = level_color(pct)
                    _, bb = text(x0, y + px(9), label)
                    mid = (bb[1] + bb[3]) // 2
                    text(x0 + px(44), mid, f"{pct:.0f}%", color, ("Segoe UI", 10, "bold"), "w")
                    text(x1, mid, fmt_reset(data.get("resets_at"), with_date), DIM, ("Malgun Gothic", 8), "e")
                    y = bb[3] + px(5)
                    bar_h = px(6)
                    shapes.append(("bar", (x0, y, x1, y + bar_h), TRACK))
                    if pct > 0:
                        fill_w = max((x1 - x0) * min(pct, 100) / 100, bar_h)
                        shapes.append(("bar", (x0, y, x0 + fill_w, y + bar_h), color))
                    y += bar_h
            y += px(12)
        H = y + px(4)

        # 배경: 흐린 뒤 화면에서 팝업 자리만 잘라내고 선·게이지를 반투명으로 합성
        if self.backdrop:
            bw, bh = self.backdrop.size
            box = (bw - margin - W, bh - margin - H, bw - margin, bh - margin)
            bg = self.backdrop.crop(box)
        else:
            bg = Image.new("RGBA", (W, H), SOLID)
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        for kind, xy, color in shapes:
            if kind == "line":
                d.line(xy, fill=color, width=max(px(1), 1))
            else:
                d.rounded_rectangle(xy, radius=(xy[3] - xy[1]) / 2, fill=color)
        self.bg_photo = ImageTk.PhotoImage(Image.alpha_composite(bg, overlay))
        c.tag_lower(c.create_image(0, 0, image=self.bg_photo, anchor="nw"))

        c.config(width=W, height=H)
        _, _, right, bottom = work_area()
        self.root.geometry(f"{W}x{H}+{right - W - margin}+{bottom - H - margin}")

    # ── 계정 추가 ──
    def add_account(self):
        self.dialog_open = True
        try:
            name = simpledialog.askstring("계정 추가", "계정 이름 (예: 회사, 개인2):", parent=self.root)
            if not name or not name.strip():
                return
            name = name.strip()
            if any(a["name"] == name for a in self.accounts):
                messagebox.showerror("계정 추가", "이미 있는 이름입니다.", parent=self.root)
                return
            slug = re.sub(r"[^\w\-]+", "_", name)
            cfg = ACCOUNTS_DIR / slug
            cfg.mkdir(parents=True, exist_ok=True)
            accounts = load_accounts() + [{"name": name, "dir": str(cfg)}]
            save_accounts(accounts)
            # 이 계정 전용 설정 폴더로 Claude Code를 띄워 로그인하게 한다
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))
            subprocess.Popen(["cmd", "/c", "start", "Claude 로그인 - " + name, "cmd", "/k", "claude"], env=env)
            messagebox.showinfo(
                "계정 추가",
                "새 터미널에서 Claude Code가 열립니다.\n추가할 계정으로 로그인한 뒤 터미널을 닫고,\n위젯에서 새로고침(⟳)을 누르세요.",
                parent=self.root)
        finally:
            self.dialog_open = False
            self.refresh()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    if "--once" in sys.argv:  # 디버그: 조회 결과만 출력
        for acc in load_accounts():
            r = fetch_account(acc)
            print(json.dumps(r, ensure_ascii=False))
        return
    if "--autostart" in sys.argv:  # 자동 실행 등록/해제만 하고 종료: --autostart on|off
        set_autostart(sys.argv[-1] != "off")
        print("자동 실행:", "켜짐" if autostart_enabled() else "꺼짐")
        return
    App().root.mainloop()


if __name__ == "__main__":
    main()
