"""Claude 사용량 조회와 OAuth 토큰 처리."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import core
from storage import log

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code 공개 OAuth client id
BETA = "oauth-2025-04-20"


def _http(url, token=None, body=None):
    headers = {"User-Agent": "claude-code/2.0", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = BETA
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _read_creds(cred_path):
    creds = json.loads(cred_path.read_text(encoding="utf-8"))
    if not isinstance(creds, dict) or not isinstance(creds.get("claudeAiOauth"), dict):
        raise RuntimeError("구독 계정 로그인이 아님")
    return creds


def _expired(oauth):
    return core._num(oauth.get("expiresAt")) / 1000 < time.time()


def _refresh_token(cred_path, used_token):
    """토큰을 갱신한다. 다른 프로세스가 먼저 갱신했으면 그 토큰을 그대로 쓴다.

    used_token: 방금 써 보고 실패한(또는 만료된) 액세스 토큰.
    """
    oauth = _read_creds(cred_path)["claudeAiOauth"]
    if oauth.get("accessToken") != used_token and not _expired(oauth):
        return oauth["accessToken"]  # Claude Code 가 이미 갱신함
    res = _http(TOKEN_URL, body={
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    })
    # 최신 파일을 다시 읽어 토큰 필드만 바꾼다 (그 사이 바뀐 다른 내용 보존)
    creds = _read_creds(cred_path)
    latest = creds["claudeAiOauth"]
    latest["accessToken"] = res["access_token"]
    latest["refreshToken"] = res.get("refresh_token", oauth["refreshToken"])
    latest["expiresAt"] = int((time.time() + res.get("expires_in", 3600)) * 1000)
    tmp = cred_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(creds), encoding="utf-8")
    os.replace(tmp, cred_path)
    log.info("토큰 갱신: %s", cred_path.parent)
    return latest["accessToken"]


def fetch_account(acc, allow_refresh=True):
    """계정 하나 조회. {name, plan, limits, breakdown, spend} 또는 {name, error[, retry]}."""
    out = {"name": acc["name"]}
    cred_path = Path(acc["dir"]).expanduser() / ".credentials.json"
    try:
        if not cred_path.exists():
            raise RuntimeError("로그인 필요 (자격 증명 없음)")
        oauth = _read_creds(cred_path)["claudeAiOauth"]
        token = oauth["accessToken"]
        if _expired(oauth):
            if not allow_refresh:
                raise RuntimeError("토큰 만료 — Claude Code 를 한 번 실행하세요")
            token = _refresh_token(cred_path, token)
        try:
            usage = _http(USAGE_URL, token)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # 다른 프로세스가 이미 갱신했을 수 있으니 파일을 다시 읽는다
            fresh = _read_creds(cred_path)["claudeAiOauth"]["accessToken"]
            if fresh == token:
                if not allow_refresh:
                    raise
                fresh = _refresh_token(cred_path, token)
            usage = _http(USAGE_URL, fresh)
        out["plan"] = (oauth.get("subscriptionType") or "").upper()
        out["limits"] = core.parse_limits(usage)
        out["breakdown"] = core.parse_breakdown(usage)
        out["spend"] = core.parse_spend(usage)
        if not out["limits"]:
            raise RuntimeError("응답 형식을 알 수 없음")
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            out["error"] = "재로그인 필요"
        else:
            out["error"] = f"HTTP {e.code}"
            out["retry"] = e.code == 429 or e.code >= 500
        log.warning("%s: HTTP %s", acc["name"], e.code)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        out["error"] = "네트워크 오류"
        out["retry"] = True
        log.warning("%s: 네트워크 오류 (%s)", acc["name"], e)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e) or type(e).__name__
        log.warning("%s: %s", acc["name"], out["error"])
    if "error" in out:
        for k in ("plan", "limits", "breakdown", "spend"):
            out.pop(k, None)
    return out
