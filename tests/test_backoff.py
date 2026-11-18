import asyncio, httpx, pytest
from fastapi import HTTPException
import app as m

class Req:  # minimal session-bearing request
    def __init__(self): self.session = {"account_id": "acct"}; self.headers = {}

def run(seq, method="GET", monkeypatch=None):
    calls, tokens = [], iter(["t1", "t2", "t3"])
    monkeypatch.setattr(m, "_get_token", lambda a, force_refresh=False: next(tokens))
    monkeypatch.setattr(m, "_backoff_delay", lambda a, r, method="GET": 0)
    it = iter(seq)
    def handler(req):
        calls.append(req.headers["authorization"]); s = next(it)
        if s == "net": raise httpx.ConnectError("boom")
        if s == 204: return httpx.Response(204)
        return httpx.Response(s, json={"ok": s} if s < 400 else {"message": "Invalid Token" if s == 403 else "x"})
    real = httpx.AsyncClient
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    ev = []
    try:
        out = asyncio.run(m._databricks(Req(), method, "/x", ev, {"a": 1} if method == "POST" else None))
    except HTTPException as e:
        out = e.status_code
    return out, calls, ev

def test_get_backoff_then_ok(monkeypatch):
    out, calls, _ = run([429, 503, "net", 200], monkeypatch=monkeypatch)
    assert out == {"ok": 200} and len(calls) == 4

def test_expired_refresh_once(monkeypatch):
    out, calls, _ = run([401, 200], monkeypatch=monkeypatch)
    assert out == {"ok": 200} and calls == ["Bearer t1", "Bearer t2"]

def test_403_invalid_token_then_throttle(monkeypatch):
    out, calls, _ = run([403, 429, 200], monkeypatch=monkeypatch)
    assert out == {"ok": 200} and len(calls) == 3

def test_post_no_retry_on_503(monkeypatch):
    out, calls, _ = run([503, 200], "POST", monkeypatch)
    assert out == 503 and len(calls) == 1

def test_post_retries_429(monkeypatch):
    out, calls, _ = run([429, 200], "POST", monkeypatch)
    assert out == {"ok": 200} and len(calls) == 2

def test_gives_up(monkeypatch):
    out, calls, _ = run([503] * 5, monkeypatch=monkeypatch)
    assert out == 503 and len(calls) == m.MAX_ATTEMPTS

def test_delay_bounds():
    assert all(0 <= m._backoff_delay(a, None) <= m.BACKOFF_CAP_SECS for a in range(1, 10))
    assert m._backoff_delay(1, httpx.Response(429, headers={"retry-after": "7"})) == 7

def test_post_delay_budget():
    lo = sum(min(30, 4 * 2 ** (a - 1)) / 2 for a in range(1, 5))
    assert lo >= 29 and all(2 <= m._backoff_delay(a, None, "POST") <= 30 for a in range(1, 6))

class H(Req):
    def __init__(self, principal, oid):
        super().__init__(); self.session["oid"] = oid; self.headers = {"x-ms-client-principal-id": principal} if principal else {}

def test_easy_auth_mismatch_clears_session():
    r = H("aaa", "bbb")
    with pytest.raises(HTTPException) as e: m._account_id(r)
    assert e.value.status_code == 401 and r.session == {}

def test_easy_auth_match_ok():
    assert m._account_id(H("AAA", "aaa")) == "acct"

def test_easy_auth_required_missing(monkeypatch):
    monkeypatch.setattr(m, "REQUIRE_EASY_AUTH", True)
    with pytest.raises(HTTPException): m._account_id(H(None, "aaa"))

def test_try_again_hint():
    r = httpx.Response(429, json={"message": "Too many requests. Try again in 60 seconds."})
    assert m._backoff_delay(1, r, "POST") == 60

def test_audit_log_no_token(monkeypatch, caplog):
    import logging
    lines = []
    monkeypatch.setattr(m._log, "info", lambda msg: lines.append(msg))
    out, calls, _ = run([401, 200], monkeypatch=monkeypatch)
    assert out == {"ok": 200} and len(lines) == 1
    rec = __import__("json").loads(lines[0])
    assert rec["status"] == 200 and rec["refreshed"] is True and rec["attempts"] == 2
    assert "t1" not in lines[0] and "t2" not in lines[0] and "Bearer" not in lines[0]

def test_id_redaction():
    p = "/api/2.0/genie/spaces/0123456789abcdef0123456789abcdef/conversations/abcdefabcdefabcdefabcdefabcdefab/messages"
    assert m._ID.sub("/{id}", p) == "/api/2.0/genie/spaces/{id}/conversations/{id}/messages"


def test_feedback_payload_and_log_without_comment(monkeypatch):
    sent, lines = {}, []
    async def fake_db(request, method, path, events, body=None):
        sent.update(method=method, path=path, body=body); return {}
    monkeypatch.setattr(m, "_databricks", fake_db)
    monkeypatch.setattr(m._log, "info", lambda msg: lines.append(msg))
    r = Req(); r.session["conversations"] = ["a" * 32]
    out = asyncio.run(m.genie_feedback("a" * 32, "b" * 32, m.FeedbackRequest(rating="NEGATIVE", comment=" wrong total "), r))
    assert out["ok"] and sent["method"] == "POST" and sent["path"].endswith("/messages/" + "b" * 32 + "/feedback")
    assert sent["body"] == {"rating": "NEGATIVE", "comment": "wrong total"}
    assert "wrong total" not in lines[-1] and '"has_comment": true' in lines[-1]

def test_feedback_none_drops_comment(monkeypatch):
    sent = {}
    async def fake_db(request, method, path, events, body=None):
        sent.update(body=body); return {}
    monkeypatch.setattr(m, "_databricks", fake_db)
    r = Req(); r.session["conversations"] = ["a" * 32]
    asyncio.run(m.genie_feedback("a" * 32, "b" * 32, m.FeedbackRequest(rating="NONE", comment="x"), r))
    assert sent["body"] == {"rating": "NONE"}

def test_empty_response_body_ok(monkeypatch):
    out, calls, _ = run([204], "POST", monkeypatch)
    assert out == {}
