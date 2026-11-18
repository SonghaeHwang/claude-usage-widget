from datetime import datetime, timedelta, timezone

import pytest

import core

NOW = datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)


def iso(**delta):
    return (NOW + timedelta(**delta)).isoformat()


USAGE = {
    "five_hour": {"utilization": 13.0, "resets_at": iso(hours=2)},
    "seven_day": {"utilization": 20.0, "resets_at": iso(days=1)},
    "limits": [
        {"kind": "session", "group": "session", "percent": 13, "resets_at": iso(hours=2), "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 20, "resets_at": iso(days=1), "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 31, "resets_at": iso(days=1),
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        {"kind": "something_new", "percent": 99},
    ],
    "seven_day_breakdown": {"rows": [
        {"key": "chat", "display_name": "Chats", "percent": 1},
        {"key": "claude_code", "display_name": "Claude Code", "percent": 97},
        {"key": "other", "display_name": "Other", "percent": 0},
    ]},
    "spend": {"enabled": False, "used": {"amount_minor": 0, "currency": "USD", "exponent": 2}},
}


# ── 응답 파싱 ──

def test_parse_limits_includes_model_scoped_limit():
    """limits 배열에서 5시간·7일·모델별 한도를 순서대로 뽑고 모르는 종류는 버린다."""
    rows = core.parse_limits(USAGE)
    assert [(r["key"], r["label"], r["pct"]) for r in rows] == [
        ("session", "5시간", 13.0), ("weekly_all", "7일", 20.0), ("weekly_scoped:Fable", "7일 · Fable", 31.0)]
    assert [r["window"] for r in rows] == ["session", "weekly", "weekly"]


def test_parse_limits_falls_back_to_legacy_fields():
    """limits 가 없으면 five_hour / seven_day 로 대체한다."""
    rows = core.parse_limits({"five_hour": {"utilization": 5, "resets_at": None}, "seven_day": {"utilization": 100}})
    assert [(r["key"], r["pct"], r["resets_at"]) for r in rows] == [("session", 5.0, None), ("weekly_all", 100.0, None)]


@pytest.mark.parametrize("bad", [None, [], "x", {}, {"limits": "nope"}, {"limits": [1, None, {"kind": "session"}]}])
def test_parse_limits_never_raises_on_malformed_input(bad):
    """형식이 깨진 응답에도 예외 없이 목록을 돌려준다."""
    assert isinstance(core.parse_limits(bad), list)


def test_parse_limits_non_numeric_percent_becomes_zero():
    """숫자가 아닌 percent 는 0 으로 본다."""
    assert core.parse_limits({"limits": [{"kind": "session", "percent": "abc"}]})[0]["pct"] == 0.0


def test_parse_breakdown_drops_zero_and_sorts_desc():
    """사용처 분해는 0% 를 빼고 큰 순서로 정렬한다."""
    assert core.parse_breakdown(USAGE) == [("Claude Code", 97.0), ("Chats", 1.0)]
    assert core.parse_breakdown({"seven_day_breakdown": None}) == []


def test_parse_spend_only_when_enabled():
    """추가 크레딧은 켜져 있을 때만 금액(소수 단위 환산)을 돌려준다."""
    assert core.parse_spend(USAGE) is None
    on = {"spend": {"enabled": True, "used": {"amount_minor": 1234, "currency": "USD", "exponent": 2},
                    "limit": {"amount_minor": 5000, "currency": "USD", "exponent": 2}}}
    spend = core.parse_spend(on)
    assert spend == {"used": 12.34, "limit": 50.0, "currency": "USD"}
    assert core.fmt_spend(spend) == "추가 사용 $12.34 / $50.00"


# ── 표시 ──

@pytest.mark.parametrize("pct,expected", [(0, "ok"), (59.9, "ok"), (60, "warn"), (84.9, "warn"), (85, "bad"), (250, "bad")])
def test_level_boundaries(pct, expected):
    """단계 경계값: 60 부터 warn, 85 부터 bad."""
    assert core.level(pct) == expected


def test_fmt_reset_cases():
    """리셋 문구: 시각 없음 '—', 지남 '곧 리셋', 남은 시간 단위(분/시간/일)."""
    assert core.fmt_reset(None, False, NOW) == "—"
    assert core.fmt_reset("not-a-date", False, NOW) == "—"
    assert core.fmt_reset(iso(seconds=-1), False, NOW) == "곧 리셋"
    assert core.fmt_reset(iso(minutes=30), False, NOW).endswith("리셋 · 30분 후")
    assert core.fmt_reset(iso(hours=2, minutes=5), False, NOW).endswith("리셋 · 2시간 5분 후")
    assert core.fmt_reset(iso(days=3, hours=4), True, NOW).endswith("리셋 · 3일 4시간 후")


def test_forecast_predicts_exhaustion_before_reset():
    """창의 절반에서 80% 면 리셋 전에 소진 시각을 예측한다."""
    reset = iso(hours=2.5)  # 5시간 창의 정확히 절반 경과
    hit = core.forecast(80, reset, "session", NOW)
    start = NOW - timedelta(hours=2.5)
    assert hit == start + timedelta(hours=2.5 * 100 / 80)


@pytest.mark.parametrize("pct,hours_left,window", [
    (40, 2.5, "session"),   # 이 속도면 리셋까지 80% → 소진 안 함
    (4, 2.5, "session"),    # 사용량이 너무 적음
    (90, 4.8, "session"),   # 창이 막 시작됨(10% 미만 경과)
    (100, 2.5, "session"),  # 이미 소진
    (80, -1, "session"),    # 리셋 시각이 지남
    (80, 2.5, "unknown"),   # 모르는 창
])
def test_forecast_returns_none(pct, hours_left, window):
    """소진이 예상되지 않거나 판단할 수 없으면 None."""
    assert core.forecast(pct, iso(hours=hours_left), window, NOW) is None
    assert core.forecast(80, None, "session", NOW) is None


def test_max_pct_and_tooltip():
    """최댓값은 성공한 계정에서만 구하고, 툴팁은 127자를 넘지 않는다."""
    results = [{"name": "A", "limits": core.parse_limits(USAGE)}, {"name": "B", "error": "네트워크 오류"}]
    assert core.max_pct(results) == 31.0
    assert core.max_pct([{"name": "B", "error": "x"}]) is None
    tip = core.tooltip(results)
    assert "A: 5h 13% · 7d 20% · Fable 31%" in tip and "B: 네트워크 오류" in tip
    assert len(core.tooltip([{"name": "가" * 200, "error": "x"}])) == 127


# ── 알림 판정 ──

def lim(pct, **delta):
    return [{"key": "session", "label": "5시간", "pct": pct, "resets_at": iso(**(delta or {"hours": 2})),
             "window": "session"}]


def test_alert_fires_once_per_threshold():
    """기준을 넘으면 한 번 알리고, 같은 창에서 같은 기준으로는 다시 알리지 않는다."""
    msgs, st = core.check_alerts({}, "개인", lim(79), [80, 95], NOW)
    assert msgs == []
    msgs, st = core.check_alerts(st, "개인", lim(80), [80, 95], NOW)
    assert len(msgs) == 1 and "개인 · 5시간 80% 사용" in msgs[0]
    msgs, st = core.check_alerts(st, "개인", lim(90), [80, 95], NOW)
    assert msgs == []
    msgs, st = core.check_alerts(st, "개인", lim(96), [80, 95], NOW)
    assert len(msgs) == 1 and "96%" in msgs[0]
    assert st["개인|session"]["level"] == 95


def test_alert_jumping_past_two_thresholds_sends_one_message():
    """한 번에 두 기준을 넘어도 알림은 하나."""
    msgs, st = core.check_alerts({}, "개인", lim(99), [80, 95], NOW)
    assert len(msgs) == 1 and st["개인|session"]["level"] == 95


def test_alert_reset_notifies_and_rearms():
    """알렸던 한도의 리셋 시각이 지나면 리셋 알림을 보내고 다시 알릴 수 있게 된다."""
    _, st = core.check_alerts({}, "개인", lim(90), [80], NOW)
    later = NOW + timedelta(hours=3)
    new = [{"key": "session", "label": "5시간", "pct": 0, "resets_at": None, "window": "session"}]
    msgs, st = core.check_alerts(st, "개인", new, [80], later)
    assert msgs == ["개인 · 5시간 한도가 리셋되었습니다"]
    assert st["개인|session"] == {"level": 0, "resets_at": None}
    msgs, _ = core.check_alerts(st, "개인", lim(85, hours=6), [80], later)
    assert len(msgs) == 1


def test_alert_reset_message_can_be_disabled_and_stale_value_ignored():
    """리셋 알림을 끄면 문구가 없고, 리셋 직후 아직 옛 값이 와도 다시 알리지 않는다."""
    _, st = core.check_alerts({}, "개인", lim(90), [80], NOW)
    later = NOW + timedelta(hours=3)
    msgs, st = core.check_alerts(st, "개인", lim(90), [80], later, notify_reset=False)  # resets_at 이 이미 과거
    assert msgs == [] and st["개인|session"]["level"] == 0


def test_alert_no_thresholds_means_no_alerts():
    """기준이 비어 있으면 알리지 않는다."""
    assert core.check_alerts({}, "개인", lim(100), [], NOW)[0] == []


# ── 계정 목록 편집 ──

ACCS = [{"name": "a", "dir": "1"}, {"name": "b", "dir": "2"}, {"name": "c", "dir": "3"}]


def test_rename_account():
    """이름 변경은 공백을 다듬고, 빈 이름·중복·없는 계정은 ValueError."""
    assert [a["name"] for a in core.rename_account(ACCS, "b", "  z ")] == ["a", "z", "c"]
    assert core.rename_account(ACCS, "b", "b")[1]["name"] == "b"
    for old, new in (("b", "a"), ("b", "  "), ("x", "y")):
        with pytest.raises(ValueError):
            core.rename_account(ACCS, old, new)
    assert ACCS[1]["name"] == "b"  # 원본은 그대로


def test_move_account_clamps_at_ends():
    """위/아래 이동, 끝에서는 그대로."""
    names = lambda xs: [a["name"] for a in xs]  # noqa: E731
    assert names(core.move_account(ACCS, "b", -1)) == ["b", "a", "c"]
    assert names(core.move_account(ACCS, "b", 1)) == ["a", "c", "b"]
    assert names(core.move_account(ACCS, "a", -1)) == ["a", "b", "c"]
    assert names(core.move_account(ACCS, "c", 5)) == ["a", "b", "c"]


def test_remove_account():
    """삭제는 해당 계정만 빼고, 없는 계정은 ValueError."""
    assert [a["name"] for a in core.remove_account(ACCS, "b")] == ["a", "c"]
    with pytest.raises(ValueError):
        core.remove_account(ACCS, "x")


# ── 설정 ──

def test_normalize_settings_defaults_and_clamping():
    """없는 값은 기본값, 범위를 벗어난 주기는 60~3600 으로 자르고 잘못된 값은 버린다."""
    assert core.normalize_settings(None) == core.DEFAULT_SETTINGS
    s = core.normalize_settings({"refresh_sec": 5, "theme": "pink", "icon_mode": "plain", "notify": "yes",
                                 "alert_thresholds": [95, 80, 80, 0, 101, "x", True]})
    assert s["refresh_sec"] == 60 and s["theme"] == "auto" and s["icon_mode"] == "plain"
    assert s["notify"] is True and s["alert_thresholds"] == [80, 95]
    assert core.normalize_settings({"refresh_sec": 99999})["refresh_sec"] == 3600
    assert core.normalize_settings({"alert_thresholds": []})["alert_thresholds"] == []
    assert core.normalize_settings({"alert_thresholds": [0]})["alert_thresholds"] == [80, 95]


def test_parse_thresholds():
    """'80, 95' → [80, 95], 숫자가 아니거나 범위를 벗어나면 ValueError."""
    assert core.parse_thresholds("95, 80 80") == [80, 95]
    assert core.parse_thresholds("") == []
    for bad in ("abc", "0", "101", "-5", "80.5"):
        with pytest.raises(ValueError):
            core.parse_thresholds(bad)


# ── 추이 ──

def test_spark_points_scales_and_filters():
    """기간·계정·한도로 거르고 좌표를 0~폭/높이로 환산한다. 점이 2개 미만이면 빈 목록."""
    hist = [{"t": 0, "a": "A", "v": {"session": 0}}, {"t": 50, "a": "A", "v": {"session": 50}},
            {"t": 100, "a": "A", "v": {"session": 250}}, {"t": 60, "a": "B", "v": {"session": 9}},
            {"t": -10, "a": "A", "v": {"session": 9}}, {"t": 70, "a": "A", "v": {}}, "garbage"]
    assert core.spark_points(hist, "A", "session", 100, 100, 200, 10) == [(0.0, 10.0), (100.0, 5.0), (200.0, 0.0)]
    assert core.spark_points(hist, "B", "session", 100, 100, 200, 10) == []
