"""~/.claude-usage-widget 의 파일 입출력과 로그 설정."""

import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import core

HOME = Path.home()
APP_DIR = Path(os.environ.get("CLAUDE_USAGE_WIDGET_HOME") or HOME / ".claude-usage-widget")
HISTORY_KEEP_SEC = 8 * 86400

log = logging.getLogger("claude-usage-widget")


def path(name):
    return APP_DIR / name


def _read_json(name, default):
    try:
        return json.loads(path(name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        log.warning("%s 를 읽을 수 없어 기본값을 씁니다", name)
        return default


def _write_json(name, data):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path(name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path(name))


# ── 계정 ──

def load_accounts():
    """계정 목록. 파일이 없으면 기본 계정 하나를 만들어 저장한다."""
    if not path("accounts.json").exists():
        accounts = [{"name": "기본", "dir": str(HOME / ".claude")}]
        save_accounts(accounts)
        return accounts
    data = _read_json("accounts.json", [])
    return [a for a in data if isinstance(a, dict) and a.get("name") and a.get("dir")] \
        if isinstance(data, list) else []


def save_accounts(accounts):
    _write_json("accounts.json", accounts)


# ── 설정 / 알림 상태 ──

def load_settings():
    return core.normalize_settings(_read_json("settings.json", {}))


def save_settings(settings):
    _write_json("settings.json", core.normalize_settings(settings))


def load_state():
    data = _read_json("state.json", {})
    return data if isinstance(data, dict) else {}


def save_state(state):
    _write_json("state.json", state)


# ── 추이 기록 ──

def append_history(results, now=None):
    """조회에 성공한 계정마다 한 줄 추가하고, 추가한 항목들을 돌려준다."""
    now = int(now or time.time())
    entries = [{"t": now, "a": r["name"], "v": {lim["key"]: lim["pct"] for lim in r["limits"]}}
               for r in results if r and "error" not in r and r.get("limits")]
    if entries:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with path("history.jsonl").open("a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return entries


def load_history(since=0):
    out = []
    try:
        with path("history.jsonl").open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue  # 깨진 줄은 건너뛴다
                if isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and e["t"] >= since:
                    out.append(e)
    except OSError:
        pass
    return out


def prune_history(now=None):
    """보관 기간이 지난 줄을 지운다."""
    keep = load_history((now or time.time()) - HISTORY_KEEP_SEC)
    if not path("history.jsonl").exists():
        return
    tmp = path("history.jsonl.tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep), encoding="utf-8")
    os.replace(tmp, path("history.jsonl"))


# ── 로그 ──

def setup_logging():
    """widget.log 로 기록하고, 처리되지 않은 예외도 남긴다."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path("widget.log"), maxBytes=256_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def on_error(exc_type, exc, tb):
        log.error("처리되지 않은 예외", exc_info=(exc_type, exc, tb))

    sys.excepthook = on_error
    threading.excepthook = lambda a: on_error(a.exc_type, a.exc_value, a.exc_traceback)
