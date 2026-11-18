"""순수 로직. 네트워크·파일·화면에 의존하지 않는다 (pytest 대상)."""

from datetime import datetime, timedelta, timezone

WEEKDAYS = "월화수목금토일"
WINDOW_SEC = {"session": 5 * 3600, "weekly": 7 * 86400}
THEMES = ("auto", "light", "dark")
ICON_MODES = ("percent", "plain")

DEFAULT_SETTINGS = {
    "refresh_sec": 300,
    "theme": "auto",
    "icon_mode": "percent",
    "notify": True,
    "notify_reset": True,
    "alert_thresholds": [80, 95],
    "token_refresh": True,
}


# ───────────────────────── 설정 ─────────────────────────

def normalize_settings(raw):
    """설정 dict 를 검증해 돌려준다. 없거나 잘못된 값은 기본값으로 대체한다."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
    if not isinstance(raw, dict):
        return out
    sec = raw.get("refresh_sec")
    if isinstance(sec, (int, float)) and not isinstance(sec, bool):
        out["refresh_sec"] = int(min(max(sec, 60), 3600))
    if raw.get("theme") in THEMES:
        out["theme"] = raw["theme"]
    if raw.get("icon_mode") in ICON_MODES:
        out["icon_mode"] = raw["icon_mode"]
    for key in ("notify", "notify_reset", "token_refresh"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    ths = raw.get("alert_thresholds")
    if isinstance(ths, list):
        ok = sorted({int(t) for t in ths
                     if isinstance(t, (int, float)) and not isinstance(t, bool) and 1 <= t <= 100})
        if ok or not ths:
            out["alert_thresholds"] = ok
    return out


def parse_thresholds(text):
    """'80, 95' 같은 입력을 [80, 95] 로. 잘못된 값이 있으면 ValueError."""
    out = set()
    for part in text.replace(",", " ").split():
        n = int(part)  # 숫자가 아니면 ValueError
        if not 1 <= n <= 100:
            raise ValueError(part)
        out.add(n)
    return sorted(out)


# ───────────────────────── 응답 파싱 ─────────────────────────

def _num(v, default=0.0):
    if isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dt(iso):
    if not isinstance(iso, str):
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse_limits(usage):
    """사용량 응답 → 표시할 한도 행 목록 [{key, label, pct, resets_at, window}]."""
    if not isinstance(usage, dict):
        return []
    rows = []
    raw = usage.get("limits")
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        if kind == "session":
            key, label, window = "session", "5시간", "session"
        elif kind == "weekly_all":
            key, label, window = "weekly_all", "7일", "weekly"
        elif kind == "weekly_scoped":
            scope = it.get("scope") if isinstance(it.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name") or "모델"
            key, label, window = f"weekly_scoped:{name}", f"7일 · {name}", "weekly"
        else:
            continue  # 모르는 종류는 건너뛴다
        rows.append({"key": key, "label": label, "pct": _num(it.get("percent")),
                     "resets_at": it.get("resets_at") if isinstance(it.get("resets_at"), str) else None,
                     "window": window})
    if rows:
        return rows
    # 예전 형식
    for field, key, label, window in (("five_hour", "session", "5시간", "session"),
                                      ("seven_day", "weekly_all", "7일", "weekly")):
        d = usage.get(field)
        if isinstance(d, dict):
            rows.append({"key": key, "label": label, "pct": _num(d.get("utilization")),
                         "resets_at": d.get("resets_at") if isinstance(d.get("resets_at"), str) else None,
                         "window": window})
    return rows


def parse_breakdown(usage):
    """7일 사용처 분해 → [(이름, %)] 0% 초과만, 큰 순서."""
    bd = usage.get("seven_day_breakdown") if isinstance(usage, dict) else None
    rows = bd.get("rows") if isinstance(bd, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and _num(r.get("percent")) > 0:
            out.append((str(r.get("display_name") or r.get("key") or "?"), _num(r.get("percent"))))
    return sorted(out, key=lambda x: -x[1])


def _money(d):
    if not isinstance(d, dict) or d.get("amount_minor") is None:
        return None
    return _num(d.get("amount_minor")) / 10 ** int(_num(d.get("exponent"), 2))


def parse_spend(usage):
    """추가 사용 크레딧 → {used, limit, currency} 또는 None(꺼져 있음)."""
    sp = usage.get("spend") if isinstance(usage, dict) else None
    if not isinstance(sp, dict) or not sp.get("enabled"):
        return None
    used = _money(sp.get("used"))
    if used is None:
        return None
    currency = sp["used"].get("currency") or ""
    return {"used": used, "limit": _money(sp.get("limit")), "currency": currency}


def fmt_spend(spend):
    sym = "$" if spend["currency"] == "USD" else ""
    unit = "" if sym else f" {spend['currency']}".rstrip()
    s = f"추가 사용 {sym}{spend['used']:.2f}"
    if spend.get("limit") is not None:
        s += f" / {sym}{spend['limit']:.2f}"
    return s + unit


# ───────────────────────── 표시 ─────────────────────────

def level(pct):
    """사용률 단계: 'ok'(<60) / 'warn'(60~85 미만) / 'bad'(85 이상)."""
    return "bad" if pct >= 85 else "warn" if pct >= 60 else "ok"


def _now(now):
    return now or datetime.now(timezone.utc)


def fmt_when(t, now=None):
    """시각을 현지 시간으로. 오늘이면 HH:MM, 아니면 날짜·요일 포함."""
    t = t.astimezone()
    if t.date() == _now(now).astimezone().date():
        return t.strftime("%H:%M")
    return t.strftime("%m/%d") + f"({WEEKDAYS[t.weekday()]}) " + t.strftime("%H:%M")


def fmt_remaining(seconds):
    d, rem = divmod(int(seconds), 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d}일 {h}시간" if d else f"{h}시간 {m}분" if h else f"{m}분"


def fmt_reset(iso, with_date, now=None):
    """리셋 시각 문구. 시각이 없으면 '—', 이미 지났으면 '곧 리셋'."""
    t = _dt(iso)
    if t is None:
        return "—"
    left = int((t - _now(now)).total_seconds())
    if left <= 0:
        return "곧 리셋"
    lt = t.astimezone()
    at = lt.strftime("%H:%M")
    if with_date:
        at = lt.strftime("%m/%d") + f"({WEEKDAYS[lt.weekday()]}) " + at
    return f"{at} 리셋 · {fmt_remaining(left)} 후"


def forecast(pct, resets_at, window, now=None):
    """지금 속도면 리셋 전에 100% 에 닿는 시각. 닿지 않거나 판단 불가면 None."""
    reset = _dt(resets_at)
    length = WINDOW_SEC.get(window)
    if reset is None or not length or pct >= 100 or pct < 5:
        return None
    now = _now(now)
    start = reset - timedelta(seconds=length)
    elapsed = (now - start).total_seconds()
    if elapsed < length * 0.1 or now >= reset:
        return None
    hit = start + timedelta(seconds=elapsed * 100 / pct)
    return hit if hit < reset else None


def max_pct(results):
    """조회에 성공한 모든 계정·한도 중 최댓값. 성공한 계정이 없으면 None."""
    vals = [lim["pct"] for r in results if r and "error" not in r for lim in r.get("limits", [])]
    return max(vals) if vals else None


def _short(lim):
    return {"session": "5h", "weekly_all": "7d"}.get(lim["key"], lim["key"].split(":", 1)[-1])


def tooltip(results):
    """트레이 툴팁 (Windows 제한 127자)."""
    lines = []
    for r in results:
        if not r:
            continue
        if "error" in r:
            lines.append(f"{r['name']}: {r['error']}")
        else:
            parts = " · ".join(f"{_short(lim)} {lim['pct']:.0f}%" for lim in r.get("limits", []))
            lines.append(f"{r['name']}: {parts}")
    return ("Claude 사용량\n" + "\n".join(lines))[:127]


# ───────────────────────── 알림 판정 ─────────────────────────

def check_alerts(state, account, limits, thresholds, now=None, notify_reset=True):
    """알림 문구 목록과 새 상태를 돌려준다. 같은 창에서 같은 기준은 한 번만 알린다.

    state: {"계정|한도키": {"level": 마지막으로 알린 기준, "resets_at": 그때의 리셋 시각}}
    """
    now = _now(now)
    state = dict(state or {})
    msgs = []
    for lim in limits:
        k = f"{account}|{lim['key']}"
        st = state.get(k) if isinstance(state.get(k), dict) else {}
        lvl = int(_num(st.get("level")))
        old_reset = _dt(st.get("resets_at"))
        if old_reset and now >= old_reset:  # 저장해 둔 창이 끝남
            if lvl and notify_reset:
                msgs.append(f"{account} · {lim['label']} 한도가 리셋되었습니다")
            lvl = 0
        new_reset = _dt(lim.get("resets_at"))
        stale = new_reset is not None and now >= new_reset  # 리셋 직후 아직 옛 값이 오는 경우
        hit = max((t for t in thresholds if lim["pct"] >= t), default=0)
        if hit > lvl and not stale:
            msgs.append(f"{account} · {lim['label']} {lim['pct']:.0f}% 사용"
                        f" ({fmt_reset(lim.get('resets_at'), lim['window'] == 'weekly', now)})")
            lvl = hit
        keep = lim.get("resets_at") or (st.get("resets_at") if lvl else None)
        state[k] = {"level": lvl, "resets_at": keep}
    return msgs, state


# ───────────────────────── 계정 목록 편집 ─────────────────────────

def _index(accounts, name):
    for i, a in enumerate(accounts):
        if a["name"] == name:
            return i
    raise ValueError(f"없는 계정: {name}")


def rename_account(accounts, old, new):
    new = (new or "").strip()
    if not new:
        raise ValueError("이름이 비어 있습니다.")
    i = _index(accounts, old)
    if any(a["name"] == new for j, a in enumerate(accounts) if j != i):
        raise ValueError("이미 있는 이름입니다.")
    out = [dict(a) for a in accounts]
    out[i]["name"] = new
    return out


def move_account(accounts, name, delta):
    """delta 만큼 위(-)/아래(+)로. 범위를 벗어나면 끝에 멈춘다."""
    i = _index(accounts, name)
    j = min(max(i + delta, 0), len(accounts) - 1)
    out = [dict(a) for a in accounts]
    out.insert(j, out.pop(i))
    return out


def remove_account(accounts, name):
    i = _index(accounts, name)
    return [dict(a) for j, a in enumerate(accounts) if j != i]


# ───────────────────────── 추이 ─────────────────────────

def spark_points(history, account, key, now_ts, span_sec, width, height):
    """기록 → 스파크라인 좌표 [(x, y)]. 점이 2개 미만이면 빈 목록."""
    start = now_ts - span_sec
    pts = []
    for h in history:
        if not isinstance(h, dict) or h.get("a") != account:
            continue
        t, v = h.get("t"), (h.get("v") or {}).get(key)
        if not isinstance(t, (int, float)) or v is None or not start <= t <= now_ts:
            continue
        pct = min(max(_num(v), 0), 100)
        pts.append(((t - start) / span_sec * width, height - pct / 100 * height))
    return sorted(pts) if len(pts) >= 2 else []
