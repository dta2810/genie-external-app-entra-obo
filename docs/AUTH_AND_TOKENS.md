# Auth and tokens

How the app signs users in, which token pattern it uses, how it handles expiry, and how it paces Genie calls. Code: `app.py`.

## Sign-in flow

1. Easy Auth signs the user in at the platform, before the request reaches the container.
2. The app runs its own MSAL authorization-code flow for the scope `<AzureDatabricks-app-id>/user_impersonation` (`DATABRICKS_SCOPE` in `app.py`). It is silent, in the same Entra session.
3. The resulting access token is a delegated user token (`idtyp=user`). Databricks sees the person, not a service principal.
4. The app cross-checks the Easy Auth principal (`X-MS-CLIENT-PRINCIPAL-ID`) against the MSAL session user.

The callback uses `response_mode=query`. `form_post` is a cross-site POST, so a `SameSite=Lax` session cookie would not be sent and the login would break. Switching to `SameSite=None` would weaken the cookie.

## Choosing a token pattern

| Pattern | How | Use when |
|---|---|---|
| **1. Entra token directly** (this repo) | MSAL auth-code flow for the AzureDatabricks resource; send the access token as Bearer | App and workspace in the **same Entra tenant**; calls go to SQL, Genie or REST APIs |
| **2. Token federation exchange** | Account federation policy (issuer, dedicated audience `api://<client-id>`, subject claim) + `POST /oidc/v1/token` with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, no `client_id` | Different tenant or IdP, or you also call Databricks Apps / MCP servers hosted on Apps (they don't accept raw Entra tokens) |

Pattern 1 only works when the Entra token is issued by the **same tenant** as the workspace. A token from another tenant is rejected with an issuer mismatch. Use pattern 2 for cross-tenant access.

This repo does not implement pattern 2.

The Easy Auth token store (`X-MS-TOKEN-AAD-ACCESS-TOKEN`) is not used. Its audience depends on the sign-in scopes, refresh is driven by the browser session, and it needs a Blob Storage token store. MSAL on the server gives explicit control over refresh and retries.

## How token expiry is handled

1. Sign-in stores the user's access and refresh tokens in the MSAL cache on the server. The session cookie only carries an account key.
2. Every Databricks call (start, follow-up, each poll, query result) calls `acquire_token_silent`, which returns the cached token or redeems the refresh token. A per-user lock stops parallel polls from hitting the refresh token at once.
3. If Databricks still rejects the token (401, or 403 with "Invalid Token", which REST APIs can return for expired tokens), the app forces a refresh and retries once.
4. If the refresh token is gone (revoked, Conditional Access), the API returns 401 and the UI asks the user to sign in again. No retry loops.

To watch steps 3 and 4 in the app, use the simulator in [EXAMPLE_USAGE.md](EXAMPLE_USAGE.md#4-simulate-token-expiry-demo).

## Throttling and polling

Each new question is asynchronous. The backend starts the Genie message and returns its ids at once. The browser then polls.

### Client polling

- Starts at 2 s, grows ×1.5 per poll, capped at 15 s between polls.
- Stops after 10 minutes. Genie keeps the conversation.
- Each poll is a new authenticated call, so a long answer survives token rotation.

### Server GET retries

- Up to 5 attempts on 429, 500, 502, 503, 504, or network errors.
- Full-jitter exponential backoff, 1–16 s.

### Server POST retries

Applies to new questions, follow-ups, and feedback.

- Retried **only on 429**, because the request was not accepted. Budget about 60 s (4 → 30 s, equal jitter).
- Never retried after a timeout or 5xx, because the message may already exist.

### Retry hints

- Genie returns no `Retry-After` header. The app honors `Retry-After` if a service sends one.
- When a 429 body says "Try again in N seconds", the app waits N seconds, capped at 60 s.

Question throughput is limited per workspace and is not published as a fixed number. Confirm your expected peak with your Databricks account team before load tests.

## References

- [Use the Genie Conversation API](https://docs.databricks.com/aws/en/genie-agents/conversation-api): polling cadence, backoff, 10-minute limit
- [Get Microsoft Entra ID tokens manually](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/aad-token-manual): AzureDatabricks resource and scopes
- [Authenticate with an identity provider token (token federation)](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/oauth-federation-exchange)
- [Authentication and authorization in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/authentication)
- [Networking in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/networking) and [Front Door with Private Link](https://learn.microsoft.com/en-us/azure/container-apps/how-to-integrate-with-azure-front-door)
- [Restrict an Entra app to a set of users](https://learn.microsoft.com/en-us/entra/identity-platform/howto-restrict-your-app-to-a-set-of-users)
