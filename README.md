# Genie Conversation API from an external Azure app, as the signed-in user

A reference implementation of a customer-hosted web app (FastAPI on Azure Container Apps) that chats with a Databricks Genie space as the signed-in Microsoft Entra ID user. Unity Catalog row filters, column masks and audit logs apply per user.

> Example code, not a product. Some features it uses are Preview or Beta and can change.

## Why

1. **Refresh on the server, no 1-hour cliff.** The Entra token lasts about 1 hour. The server gets a fresh one for every Databricks call and refreshes once on expiry, so users stay signed in.
2. **No token in the browser or per request.** The app is a backend-for-frontend (BFF). MSAL keeps the tokens on the server. The browser holds only a session cookie.
3. **Platform gate.** Easy Auth and Entra "Assignment required" block every path before the request reaches the container. Nothing answers without sign-in.

## Architecture

```
Browser ──HTTPS──▶ Container Apps ingress ──▶ Easy Auth sidecar ──▶ FastAPI (BFF) ──Bearer (user token)──▶ Genie Conversation API
  session cookie      (TLS, HTTP→HTTPS)       Entra sign-in,          MSAL: auth code + PKCE,               ▲ runs SQL as the user
  only                                         assignment required     token cache, refresh,                 │ UC row filter + mask
                                                                       backoff, audit log                    │
                                                                                                        SQL warehouse
```

| Layer | Component | What it controls |
|---|---|---|
| Experience | Chat UI (`static/index.html`) in the browser | Questions, answers, feedback; holds only the session cookie |
| Platform gate | Container Apps Easy Auth + Entra "Assignment required" | Who can sign in; unauthenticated requests never reach the app |
| Identity | App's MSAL flow (`app.py`) | Delegated user token per call; token refresh; identity cross-check |
| Data | Genie space (viewer credentials), SQL warehouse, Unity Catalog | Row filter and column mask per user |
| Audit | JSON log lines in the app; Databricks audit log | Who asked, as whom, with what status |

**Token pattern:**

- The app gets a delegated Entra token for Azure Databricks and sends it as a Bearer token from the server.
- This works only when the app and the workspace share one Entra tenant (pattern 1).
- Cross-tenant access, and Databricks Apps or MCP servers, need token federation (pattern 2). See [docs/AUTH_AND_TOKENS.md](docs/AUTH_AND_TOKENS.md).

## How to start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r setup/requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q                                  # unit tests, no network
cp infra/config.example.env infra/config.local.env    # fill in; gitignored
```

Then follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): workspace demo, persona groups, Azure deploy.

## Docs

| Document | Read it to |
|---|---|
| [docs/PREREQUISITES.md](docs/PREREQUISITES.md) | Check Azure, Entra, Azure Databricks and laptop requirements, and who must grant each |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Deploy in phases: workspace demo, persona groups, Azure, local run, configuration |
| [docs/EXAMPLE_USAGE.md](docs/EXAMPLE_USAGE.md) | Run the demo: example questions, personas, token-expiry button, feedback |
| [docs/AUTH_AND_TOKENS.md](docs/AUTH_AND_TOKENS.md) | Choose a token pattern; learn how sign-in, expiry, throttling and backoff work |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Re-verify, rotate the secret, read logs, fix known gotchas, review the security model |

## Repository layout

| Path | Contents |
|---|---|
| `app.py` | FastAPI BFF (auth, token handling, Genie proxy, backoff, audit log) |
| `static/index.html` | Chat UI with async polling and an identity/token panel |
| `Dockerfile` | `python:3.12-slim`, non-root, 1 worker, no access log |
| `infra/deploy.sh` | Parametrized Azure deploy (internal ingress → Easy Auth → external → verify) |
| `infra/verify.sh` | Negative tests: nothing reachable without authentication |
| `infra/config.example.env` | Template for every deploy value (copy to `infra/config.local.env`) |
| `setup/setup_workspace.py` | Demo table, row filter, mask and Genie space |
| `setup/groups.sh` | Account persona groups and workspace assignment |
| `requirements*.txt` | Pinned runtime deps; `requirements-dev.txt` adds pytest |
| `tests/` | Unit tests: refresh, backoff, identity checks, CSRF, headers, logging (no network) |
| `docs/` | This documentation |

## License and disclaimer

Licensed under the [Apache License 2.0](LICENSE). This is a personal reference implementation for educational and demo purposes. It is not an official Databricks or Microsoft product, comes with no support or SLA, and should be reviewed and hardened (see [docs/OPERATIONS.md](docs/OPERATIONS.md), "Production recommendations") before any production use.
