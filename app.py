"""Genie Conversation API from an external app (Azure Container Apps / App Service) as the Entra user.

Backend-for-frontend: MSAL keeps the user's tokens server-side; the browser only holds a session cookie.
See README.md and docs/.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

import httpx
import msal
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

TENANT_ID = os.environ["AZURE_TENANT_ID"]
CLIENT_ID = os.environ["AZURE_CLIENT_ID"]
CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
REDIRECT_URI = os.environ["REDIRECT_URI"]
DATABRICKS_HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
# User tokens are sent to this host: refuse anything that is not an Azure Databricks workspace URL.
if not re.fullmatch(r"https://[A-Za-z0-9-]+(\.[0-9]+)?\.azuredatabricks\.net", DATABRICKS_HOST):
    raise SystemExit("DATABRICKS_HOST must be https://<workspace>.azuredatabricks.net")
GENIE_SPACE_ID = os.environ["GENIE_SPACE_ID"]
HTTPS_ONLY = os.environ.get("HTTPS_ONLY", "true").lower() != "false"
MAX_ROWS = int(os.environ.get("MAX_ROWS", "200"))

# AzureDatabricks first-party resource; MSAL adds openid/offline_access/profile itself.
DATABRICKS_SCOPE = ["2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}
# Backoff: POST start/create-message is not idempotent, so it only retries on 429 (request not accepted).
RETRY_STATUSES_GET = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
BACKOFF_BASE_SECS = 1.0
BACKOFF_CAP_SECS = 16.0
# Genie throttles new questions per workspace (limit not published); a throttled POST gets ~60 s of budget.
POST_BACKOFF_BASE_SECS = 4.0
POST_BACKOFF_CAP_SECS = 30.0
HINT_CAP_SECS = 60.0
# When Easy Auth fronts the app, require its principal header and match it to the MSAL session.
REQUIRE_EASY_AUTH = os.environ.get("REQUIRE_EASY_AUTH", "false").lower() == "true"
ENABLE_DEBUG = os.environ.get("ENABLE_DEBUG", "false").lower() == "true"
SESSION_MAX_AGE_SECS = int(os.environ.get("SESSION_MAX_AGE_SECS", str(8 * 3600)))
APP_ORIGIN = REDIRECT_URI.split("/auth/", 1)[0]
GENIE_ID = re.compile(r"^[0-9a-f]{32}$")
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

# In-memory MSAL cache: valid for 1 replica / 1 worker. Multi-replica needs a shared cache (Redis).
_token_cache = msal.SerializableTokenCache()


class _LazyMsal:
    # Created on first use: no network call (authority discovery) at import time.
    _app = None

    def __getattr__(self, name):
        if _LazyMsal._app is None:
            _LazyMsal._app = msal.ConfidentialClientApplication(
                CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET, token_cache=_token_cache)
        return getattr(_LazyMsal._app, name)


_msal = _LazyMsal()
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# Structured audit log: one JSON line per Databricks call / login. Never tokens or question text.
_log = logging.getLogger("genie_obo")
_log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(message)s"))
_log.addHandler(_h)
_log.propagate = False
_ID = re.compile(r"/[0-9a-f]{32}|/[0-9a-zA-Z]{20,}")


def _audit(event: str, request: Request, **fields) -> None:
    user = (request.session.get("user") or {}).get("email", "")
    _log.info(json.dumps({"ts": round(time.time(), 3), "event": event, "user": user, **fields}))


app = FastAPI(title="Genie external OBO demo")


@app.middleware("http")
async def security(request: Request, call_next):
    # CSRF: state-changing requests must come from this app's origin.
    if request.method == "POST" and request.headers.get("origin", APP_ORIGIN) != APP_ORIGIN:
        _log.info(json.dumps({"ts": round(time.time(), 3), "event": "csrf_rejected", "path": request.url.path,
                              "origin": request.headers.get("origin", "")[:100], "expected": APP_ORIGIN}))
        return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
    resp = await call_next(request)
    if resp.status_code >= 400:
        _log.info(json.dumps({"ts": round(time.time(), 3), "event": "http_error", "method": request.method,
                              "path": _ID.sub("/{id}", request.url.path), "status": resp.status_code}))
    resp.headers.update({"Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                         "X-Content-Type-Options": "nosniff",
                         # Not "no-referrer": it makes browsers send Origin: null on POST, which Easy Auth's CSRF check rejects.
                         "Referrer-Policy": "same-origin"})
    return resp


app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=HTTPS_ONLY, same_site="lax",
                   max_age=SESSION_MAX_AGE_SECS)


def _lock_for(account_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(account_id, threading.Lock())


def _get_token(account_id: str, force_refresh: bool = False) -> str:
    # Per-user lock so parallel polls don't all hit the refresh token at once.
    with _lock_for(account_id):
        account = next((a for a in _msal.get_accounts() if a["home_account_id"] == account_id), None)
        if not account:
            raise HTTPException(401, "Session expired, please log in again")
        result = _msal.acquire_token_silent(DATABRICKS_SCOPE, account=account, force_refresh=force_refresh)
    if not result or "access_token" not in result:
        raise HTTPException(401, "Could not refresh the Databricks token, please log in again")
    return result["access_token"]


def _is_expired_token_error(r: httpx.Response) -> bool:
    # Non-MCP Databricks REST paths can answer an expired token with 403 "Invalid Token", not 401.
    if r.status_code == 401:
        return True
    if r.status_code == 403:
        body = r.text.lower()
        return "invalid token" in body or "expired" in body or "invalidtoken" in body
    return False


def _check_easy_auth(request: Request) -> None:
    # Easy Auth injects X-MS-CLIENT-PRINCIPAL-ID (Entra oid); clients cannot set it while Easy Auth is on.
    principal = request.headers.get("x-ms-client-principal-id")
    if REQUIRE_EASY_AUTH and not principal:
        raise HTTPException(401, "Platform authentication missing")
    oid = request.session.get("oid")
    if principal and oid and principal.lower() != oid.lower():
        request.session.clear()
        raise HTTPException(401, "Signed-in user changed, please log in again")


def _account_id(request: Request) -> str:
    _check_easy_auth(request)
    account_id = request.session.get("account_id")
    if not account_id:
        raise HTTPException(401, "Not authenticated")
    return account_id


def _backoff_delay(attempt: int, r: Optional[httpx.Response], method: str = "GET") -> float:
    # Honor Retry-After; GET = full jitter 1-16 s; POST 429 = equal jitter 4-30 s (~60 s total budget).
    cap = POST_BACKOFF_CAP_SECS if method == "POST" else BACKOFF_CAP_SECS
    if r is not None and (ra := r.headers.get("retry-after", "")).isdigit():
        return min(float(ra), cap)
    # Genie sends no Retry-After; some 429 bodies say "Try again in 60 seconds".
    if r is not None and (hint := re.search(r"try again in (\d+) seconds?", r.text, re.I)):
        return min(float(hint.group(1)), HINT_CAP_SECS)
    if method == "POST":
        d = min(cap, POST_BACKOFF_BASE_SECS * 2 ** (attempt - 1))
        return d / 2 + random.uniform(0, d / 2)
    return random.uniform(0, min(cap, BACKOFF_BASE_SECS * 2 ** (attempt - 1)))


async def _databricks(request: Request, method: str, path: str, events: list, body: Optional[dict] = None) -> dict:
    """Call Databricks as the user: fresh token per call, one forced refresh on expiry,
    exponential backoff on throttling/transient errors (GET only, POST only on 429)."""
    account_id = _account_id(request)
    token = await run_in_threadpool(_get_token, account_id)
    if request.session.pop("simulate_expiry", False):
        token = "expired-token-simulated"
        events.append("simulated expired token sent")
    retryable = RETRY_STATUSES_GET if method == "GET" else {429}
    refreshed = False
    r = None
    t0 = time.monotonic()
    op = f"{method} {_ID.sub('/{id}', path)}"
    attempt = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = await client.request(method, f"{DATABRICKS_HOST}{path}", json=body,
                                         headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as e:
                if method != "GET" or attempt == MAX_ATTEMPTS:
                    _audit("databricks_call", request, op=op, status=502, attempts=attempt, refreshed=refreshed,
                           latency_ms=round((time.monotonic() - t0) * 1000), error=type(e).__name__, auth_events=events)
                    raise HTTPException(502, f"Databricks unreachable: {type(e).__name__}")
                delay = _backoff_delay(attempt, None)
                events.append(f"network error, backoff {delay:.1f} s (attempt {attempt})")
                await asyncio.sleep(delay)
                continue
            if not refreshed and _is_expired_token_error(r):
                events.append(f"HTTP {r.status_code} token rejected, forcing refresh via MSAL refresh token")
                token = await run_in_threadpool(_get_token, account_id, True)
                refreshed = True
                events.append("new access token acquired, retrying once")
                continue
            if r.status_code in retryable and attempt < MAX_ATTEMPTS:
                delay = _backoff_delay(attempt, r, method)
                events.append(f"HTTP {r.status_code}, backoff {delay:.1f} s (attempt {attempt})")
                await asyncio.sleep(delay)
                continue
            break
    _audit("databricks_call", request, op=op, status=r.status_code, attempts=attempt, refreshed=refreshed,
           latency_ms=round((time.monotonic() - t0) * 1000), auth_events=events)
    if r.status_code >= 400:
        # Upstream detail goes to the log only; the client gets a generic message.
        _audit("databricks_error", request, op=op, status=r.status_code, detail=r.text[:300])
        raise HTTPException(r.status_code, _client_error(r.status_code))
    return r.json() if r.content else {}


def _client_error(status: int) -> str:
    if status == 429:
        return "Genie is busy, please try again in a minute"
    if status in (401, 403):
        return "Databricks denied the request for your user"
    if status == 404:
        return "Not found"
    return "Databricks request failed"


def _jwt_claims(token: str) -> dict:
    # Display only, never used for authorization (Databricks validates the token).
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    keep = ("iss", "aud", "idtyp", "upn", "unique_name", "scp", "appid", "exp")
    out = {k: claims.get(k) for k in keep if k in claims}
    out["expires_in_min"] = round((claims.get("exp", 0) - time.time()) / 60, 1)
    return out


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text())


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/auth/login")
def login(request: Request):
    flow = _msal.initiate_auth_code_flow(DATABRICKS_SCOPE, redirect_uri=REDIRECT_URI, state=str(uuid.uuid4()))
    request.session["auth_flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@app.get("/auth/callback")
def auth_callback(request: Request):
    flow = request.session.pop("auth_flow", None)
    if not flow:
        return JSONResponse({"error": "no login in progress"}, status_code=400)
    result = _msal.acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "error" in result:
        _audit("login_failed", request, error=result.get("error"), detail=(result.get("error_description") or "")[:300])
        return JSONResponse({"error": "sign-in failed"}, status_code=400)
    claims = result.get("id_token_claims", {})
    # Pick exactly this user's cache entry: oid + tenant of the id_token. home_account_id (from client_info,
    # home tenant) differs from oid.tid for B2B guests, and username can be missing or ambiguous.
    oid, tid = claims.get("oid"), claims.get("tid")
    matches = [a for a in _msal.get_accounts() if oid and a.get("local_account_id") == oid and a.get("realm") == tid]
    if len(matches) != 1:
        _audit("login_failed", request, error="account_match", matches=len(matches))
        return JSONResponse({"error": "sign-in failed"}, status_code=500)
    request.session.clear()
    request.session["account_id"] = matches[0]["home_account_id"]
    request.session["oid"] = claims.get("oid", "")
    request.session["user"] = {"name": claims.get("name", ""), "email": claims.get("preferred_username", "")}
    _audit("login", request, tenant=claims.get("tid"))
    return RedirectResponse("/")


@app.post("/auth/logout")
def logout(request: Request):
    _audit("logout", request)
    account_id = request.session.get("account_id")
    for a in _msal.get_accounts():
        if a["home_account_id"] == account_id:
            _msal.remove_account(a)  # drop cached access + refresh tokens
    request.session.clear()
    # Behind Easy Auth, its logout also ends the platform session and signs out of Entra.
    target = "/.auth/logout?post_logout_redirect_uri=/" if REQUIRE_EASY_AUTH else \
        f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={APP_ORIGIN}"
    return RedirectResponse(target, status_code=303)


@app.get("/api/me")
def me(request: Request):
    _check_easy_auth(request)
    user = request.session.get("user")
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return {**user, "debug": ENABLE_DEBUG}


@app.get("/api/whoami")
async def whoami(request: Request):
    events: list = []
    token = await run_in_threadpool(_get_token, _account_id(request))
    me = await _databricks(request, "GET", "/api/2.0/preview/scim/v2/Me", events)
    return {"databricks_user": me.get("userName"), "token_claims": _jwt_claims(token), "auth_events": events}


@app.post("/api/debug/expire")
def simulate_expiry(request: Request):
    if not ENABLE_DEBUG:
        raise HTTPException(404, "Not found")
    _account_id(request)
    request.session["simulate_expiry"] = True
    return {"ok": True, "next_call": "will send an invalid token first, then refresh and retry"}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{32}$")


@app.post("/api/genie/ask")
async def genie_ask(body: AskRequest, request: Request):
    """Start (or continue) a conversation and return ids immediately; the client polls."""
    events: list = []
    base = f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}"
    owned = request.session.get("conversations", [])
    if body.conversation_id and body.conversation_id not in owned:
        raise HTTPException(404, "Not found")  # only conversations this session started
    if body.conversation_id:
        msg = await _databricks(request, "POST", f"{base}/conversations/{body.conversation_id}/messages",
                                events, {"content": body.question})
        conv_id = body.conversation_id
    else:
        data = await _databricks(request, "POST", f"{base}/start-conversation", events, {"content": body.question})
        conv_id, msg = data["conversation_id"], data["message"]
        request.session["conversations"] = (owned + [conv_id])[-20:]
    return {"conversation_id": conv_id, "message_id": msg["id"], "status": msg.get("status"), "auth_events": events}


@app.get("/api/genie/messages/{conv_id}/{msg_id}")
async def genie_message(conv_id: str, msg_id: str, request: Request):
    """One poll step. Token is re-acquired on every poll, so long answers survive token rotation."""
    if not (GENIE_ID.match(conv_id) and GENIE_ID.match(msg_id)):
        raise HTTPException(400, "invalid id")
    if conv_id not in request.session.get("conversations", []):
        raise HTTPException(404, "Not found")
    events: list = []
    base = f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}/conversations/{conv_id}/messages/{msg_id}"
    msg = await _databricks(request, "GET", base, events)
    status = msg.get("status")
    out = {"status": status, "text": None, "sql": None, "columns": None, "rows": None,
           "feedback": (msg.get("feedback") or {}).get("rating"), "auth_events": events}
    if status not in TERMINAL:
        return out
    if status == "FAILED":
        out["text"] = (msg.get("error") or {}).get("error") or "Genie could not answer"
        return out
    att_id = None
    for att in msg.get("attachments") or []:
        if "text" in att:
            out["text"] = att["text"].get("content")
        if "query" in att:
            out["sql"] = att["query"].get("query")
            out["text"] = out["text"] or att["query"].get("description")
            att_id = att.get("attachment_id")
    if att_id and GENIE_ID.match(str(att_id)):
        qr = await _databricks(request, "GET", f"{base}/attachments/{att_id}/query-result", events)
        stmt = qr.get("statement_response", {})
        out["columns"] = [c["name"] for c in stmt.get("manifest", {}).get("schema", {}).get("columns", [])]
        out["rows"] = (stmt.get("result", {}).get("data_array") or [])[:MAX_ROWS]
    return out


class FeedbackRequest(BaseModel):
    rating: Literal["POSITIVE", "NEGATIVE", "NONE"]  # NONE clears a previous rating
    comment: Optional[str] = Field(default=None, max_length=1000)


@app.post("/api/genie/messages/{conv_id}/{msg_id}/feedback")
async def genie_feedback(conv_id: str, msg_id: str, body: FeedbackRequest, request: Request):
    """Thumbs up/down on a Genie answer, sent as the signed-in user (shows in the space's Monitoring tab)."""
    if not (GENIE_ID.match(conv_id) and GENIE_ID.match(msg_id)):
        raise HTTPException(400, "invalid id")
    if conv_id not in request.session.get("conversations", []):
        raise HTTPException(404, "Not found")
    events: list = []
    payload = {"rating": body.rating}
    comment = (body.comment or "").strip()
    if comment and body.rating != "NONE":
        payload["comment"] = comment  # Public Preview field
    await _databricks(request, "POST", f"/api/2.0/genie/spaces/{GENIE_SPACE_ID}/conversations/{conv_id}"
                      f"/messages/{msg_id}/feedback", events, payload)
    # Audit: rating only; the comment text is never logged.
    _audit("feedback", request, rating=body.rating, has_comment=bool(payload.get("comment")))
    return {"ok": True, "rating": body.rating, "auth_events": events}
