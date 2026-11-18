from fastapi.testclient import TestClient
import app as m

client = TestClient(m.app, base_url="http://localhost:8000")

def test_post_cross_origin_rejected():
    r = client.post("/api/genie/ask", json={"question": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403

def test_security_headers():
    r = client.get("/health")
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    # no-referrer would make the browser send Origin: null on POST -> Easy Auth CSRF 403
    assert r.headers["referrer-policy"] == "same-origin"

def test_debug_disabled_by_default(monkeypatch):
    monkeypatch.setattr(m, "ENABLE_DEBUG", False)
    assert client.post("/api/debug/expire", headers={"origin": m.APP_ORIGIN}).status_code == 404

def test_logout_is_post_only():
    assert client.get("/auth/logout").status_code == 405

def test_bad_ids_rejected():
    assert client.get("/api/genie/messages/../x/y").status_code in (400, 404)
    assert client.get("/api/genie/messages/" + "a" * 32 + "/ZZZ").status_code in (400, 401)

def test_foreign_conversation_rejected():
    # No session conversations: polling or continuing someone else's conversation id is refused.
    r = client.get("/api/genie/messages/" + "a" * 32 + "/" + "b" * 32)
    assert r.status_code in (401, 404)

def test_host_allowlist():
    import re as _re
    pat = r"https://[A-Za-z0-9-]+(\.[0-9]+)?\.azuredatabricks\.net"
    assert _re.fullmatch(pat, "https://adb-123.4.azuredatabricks.net")
    assert not _re.fullmatch(pat, "https://evil.example/x.azuredatabricks.net")
    assert not _re.fullmatch(pat, "https://adb-1.azuredatabricks.net.evil.io")

class FakeMsal:
    def __init__(self, accounts): self.accounts = accounts
    def get_accounts(self, username=None): return self.accounts
    def acquire_token_by_auth_code_flow(self, flow, params):
        return {"id_token_claims": {"oid": "o1", "tid": "t1", "preferred_username": ""}}

def _callback(monkeypatch, accounts):
    monkeypatch.setattr(m, "_msal", FakeMsal(accounts))
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/auth/callback", "headers": [], "query_string": b"",
             "session": {"auth_flow": {"x": 1}}}
    req = Request(scope)
    return m.auth_callback(req), req.session

def test_callback_picks_exact_account_even_without_username(monkeypatch):
    accts = [{"home_account_id": "other", "local_account_id": "o2", "realm": "t1"},
             {"home_account_id": "mine", "local_account_id": "o1", "realm": "t1"}]
    resp, sess = _callback(monkeypatch, accts)
    assert resp.status_code in (302, 307) and sess["account_id"] == "mine"

def test_callback_fails_closed_when_no_exact_match(monkeypatch):
    resp, sess = _callback(monkeypatch, [{"home_account_id": "other", "local_account_id": "o2", "realm": "t1"}])
    assert resp.status_code == 500 and "account_id" not in sess


def test_feedback_rejects_bad_rating_and_foreign_conversation():
    o = {"origin": m.APP_ORIGIN}
    conv, msg = "a" * 32, "b" * 32
    assert client.post(f"/api/genie/messages/{conv}/{msg}/feedback", json={"rating": "LOVE"}, headers=o).status_code == 422
    assert client.post(f"/api/genie/messages/{conv}/{msg}/feedback", json={"rating": "POSITIVE", "comment": "x" * 1001},
                       headers=o).status_code == 422
    # valid body, but this session never started the conversation
    assert client.post(f"/api/genie/messages/{conv}/{msg}/feedback", json={"rating": "POSITIVE"}, headers=o).status_code == 404
    assert client.post(f"/api/genie/messages/{conv}/{msg}/feedback", json={"rating": "POSITIVE"},
                       headers={"origin": "https://evil.example"}).status_code == 403
