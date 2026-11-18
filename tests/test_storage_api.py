import json
import time
import urllib.error

import pytest

import api
import core
import storage


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    """실제 홈 폴더 대신 임시 폴더를 쓴다."""
    monkeypatch.setattr(storage, "APP_DIR", tmp_path / "app")
    return tmp_path / "app"


# ── storage ──

def test_accounts_default_created_and_roundtrip(app_dir):
    """계정 파일이 없으면 기본 계정을 만들어 저장하고, 저장한 목록을 그대로 읽는다."""
    assert [a["name"] for a in storage.load_accounts()] == ["기본"]
    assert (app_dir / "accounts.json").exists()
    storage.save_accounts([{"name": "회사", "dir": "x"}])
    assert storage.load_accounts() == [{"name": "회사", "dir": "x"}]


def test_corrupt_files_fall_back(app_dir):
    """깨진 JSON 은 예외 대신 빈 목록/기본 설정/빈 상태로 읽힌다."""
    app_dir.mkdir()
    for name in ("accounts.json", "settings.json", "state.json"):
        (app_dir / name).write_text("{not json", encoding="utf-8")
    assert storage.load_accounts() == []
    assert storage.load_settings() == core.DEFAULT_SETTINGS
    assert storage.load_state() == {}


def test_settings_saved_normalized():
    """설정은 검증된 값으로 저장된다."""
    storage.save_settings({"theme": "dark", "refresh_sec": 1})
    s = storage.load_settings()
    assert s["theme"] == "dark" and s["refresh_sec"] == 60 and s["alert_thresholds"] == [80, 95]


def test_history_append_load_prune(app_dir):
    """성공한 계정만 기록되고, since 로 걸러 읽으며, 보관 기간이 지난 줄과 깨진 줄은 정리된다."""
    lim = [{"key": "session", "pct": 13.0}]
    now = 1_000_000_000
    assert storage.append_history([{"name": "A", "limits": lim}, {"name": "B", "error": "x"}, None],
                                  now - storage.HISTORY_KEEP_SEC - 1) != []
    with (app_dir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write("broken line\n")
    added = storage.append_history([{"name": "A", "limits": lim}], now)
    assert added == [{"t": now, "a": "A", "v": {"session": 13.0}}]
    assert len(storage.load_history()) == 2
    assert storage.load_history(now) == added
    storage.prune_history(now)
    assert (app_dir / "history.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert storage.append_history([{"name": "B", "error": "x"}], now) == []


# ── api (네트워크 대신 가짜 _http) ──

USAGE = {"limits": [{"kind": "session", "percent": 13, "resets_at": None}]}


def make_account(tmp_path, expires_in=3600, extra=None):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    creds = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1", "subscriptionType": "max",
                               "expiresAt": int((time.time() + expires_in) * 1000)}, **(extra or {})}
    (cfg / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")
    return {"name": "개인", "dir": str(cfg)}, cfg / ".credentials.json"


def fake_http(monkeypatch, handler):
    calls = []

    def _http(url, token=None, body=None):
        calls.append((url, token))
        return handler(url, token, body)

    monkeypatch.setattr(api, "_http", _http)
    return calls


def http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, None)


def test_fetch_valid_token_does_not_refresh(tmp_path, monkeypatch):
    """토큰이 유효하면 갱신 없이 조회하고 계약된 키를 돌려준다."""
    acc, _ = make_account(tmp_path)
    calls = fake_http(monkeypatch, lambda url, token, body: USAGE)
    r = api.fetch_account(acc)
    assert calls == [(api.USAGE_URL, "old")]
    assert r["name"] == "개인" and r["plan"] == "MAX" and r["limits"][0]["pct"] == 13.0
    assert r["breakdown"] == [] and r["spend"] is None and "error" not in r


def test_fetch_expired_token_refreshes_and_preserves_other_fields(tmp_path, monkeypatch):
    """만료되면 갱신해 새 토큰으로 조회하고, 파일의 다른 내용은 건드리지 않는다."""
    acc, cred = make_account(tmp_path, expires_in=-10, extra={"other": {"keep": 1}})

    def handler(url, token, body):
        if url == api.TOKEN_URL:
            assert body["refresh_token"] == "r1"
            return {"access_token": "new", "refresh_token": "r2", "expires_in": 100}
        assert token == "new"
        return USAGE

    fake_http(monkeypatch, handler)
    assert "error" not in api.fetch_account(acc)
    saved = json.loads(cred.read_text(encoding="utf-8"))
    assert saved["other"] == {"keep": 1}
    assert saved["claudeAiOauth"]["accessToken"] == "new" and saved["claudeAiOauth"]["refreshToken"] == "r2"
    assert saved["claudeAiOauth"]["subscriptionType"] == "max"


def test_fetch_expired_token_without_refresh_permission(tmp_path, monkeypatch):
    """갱신이 꺼져 있으면 만료 시 네트워크를 쓰지 않고 오류를 돌려준다."""
    acc, cred = make_account(tmp_path, expires_in=-10)
    before = cred.read_text(encoding="utf-8")
    calls = fake_http(monkeypatch, lambda *a: USAGE)
    r = api.fetch_account(acc, allow_refresh=False)
    assert "만료" in r["error"] and calls == [] and cred.read_text(encoding="utf-8") == before
    assert "limits" not in r


def test_fetch_401_uses_token_refreshed_by_another_process(tmp_path, monkeypatch):
    """401 인데 그 사이 파일의 토큰이 바뀌었으면 직접 갱신하지 않고 그 토큰을 쓴다."""
    acc, cred = make_account(tmp_path)

    def handler(url, token, body):
        assert url == api.USAGE_URL
        if token == "old":
            creds = json.loads(cred.read_text(encoding="utf-8"))
            creds["claudeAiOauth"]["accessToken"] = "by-claude-code"
            cred.write_text(json.dumps(creds), encoding="utf-8")
            raise http_error(401)
        return USAGE

    calls = fake_http(monkeypatch, handler)
    assert "error" not in api.fetch_account(acc)
    assert [t for _, t in calls] == ["old", "by-claude-code"]


@pytest.mark.parametrize("exc,message,retry", [
    (urllib.error.URLError("down"), "네트워크 오류", True),
    (TimeoutError(), "네트워크 오류", True),
    (http_error(503), "HTTP 503", True),
    (http_error(429), "HTTP 429", True),
    (http_error(404), "HTTP 404", False),
    (http_error(403), "재로그인 필요", None),
])
def test_fetch_error_mapping(tmp_path, monkeypatch, exc, message, retry):
    """오류 종류별 문구와 재시도 여부."""
    acc, _ = make_account(tmp_path)

    def handler(*a):
        raise exc

    fake_http(monkeypatch, handler)
    r = api.fetch_account(acc)
    assert r["error"] == message and r.get("retry") == retry


def test_fetch_missing_or_invalid_credentials(tmp_path, monkeypatch):
    """자격 증명이 없거나 구독 로그인이 아니거나 응답에 한도가 없으면 오류로 돌려준다."""
    fake_http(monkeypatch, lambda *a: {})
    assert "로그인 필요" in api.fetch_account({"name": "x", "dir": str(tmp_path / "none")})["error"]
    cfg = tmp_path / "apikey"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text("{}", encoding="utf-8")
    assert api.fetch_account({"name": "x", "dir": str(cfg)})["error"] == "구독 계정 로그인이 아님"
    acc, _ = make_account(tmp_path)
    assert api.fetch_account(acc)["error"] == "응답 형식을 알 수 없음"
