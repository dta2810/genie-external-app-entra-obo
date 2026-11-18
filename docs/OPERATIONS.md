# Operations

Turn off, re-verify, rotate, tear down, troubleshoot and review a deployment.

## Re-verify

```bash
infra/verify.sh https://<app-fqdn>
```

The script sends unauthenticated requests to `/`, `/health`, `/api/me`, `/api/whoami`, `/api/genie/messages/x/y` (GET) and `/api/genie/ask` (POST). It fails on any 200. It retries up to 12 times while propagation completes, and reports the plain HTTP redirect of `/health`.

## Rotate the client secret

Each `infra/deploy.sh` run deletes this app's previous deploy credentials (matched by display name) and mints one new client secret valid 1 year. The secret goes into the Container App secret `client-secret`. Re-run the deploy to rotate.

## Turn off and on

Stop serving between demo sessions. A deactivated revision answers 404 and costs nothing.

```bash
REV=$(az containerapp revision list -n <ACA_APP_NAME> -g <AZ_RESOURCE_GROUP> --query "[?properties.active].name" -o tsv)
az containerapp revision deactivate -n <ACA_APP_NAME> -g <AZ_RESOURCE_GROUP> --revision "$REV"   # off
az containerapp revision activate   -n <ACA_APP_NAME> -g <AZ_RESOURCE_GROUP> --revision "$REV"   # on
infra/verify.sh https://<app-fqdn>                                                               # after turning on
databricks warehouses stop <warehouse-id>                                                        # optional
```

## Tear down

```bash
az group delete -n <AZ_RESOURCE_GROUP>          # Container App, environment, registry, secrets
az ad app delete --id <ENTRA_APP_ID>            # app registration and its enterprise application
databricks genie trash-space <GENIE_SPACE_ID>   # Genie space
```

Then drop the demo schema (`DROP SCHEMA <catalog>.<schema> CASCADE`) and delete the two account groups.

## Logs

The app writes one JSON line per sign-in and Databricks call: user, operation (ids redacted), status, attempts, refresh, latency. It never writes tokens, question text, or an access log.

The Container Apps environment sends no logs anywhere (`--logs-destination none`). Read console output with `az containerapp logs show`.

## Known gotchas

- **B2B guest users:** MSAL keys accounts by `home_account_id` (home tenant from `client_info`), not by the id_token `oid.tid` of the resource tenant. Look the account up from the MSAL cache after sign-in, as `app.py` does.
- **Same tenant:** pattern 1 tokens must come from the workspace's tenant.
- **Expired token ≠ always 401:** treat 403 "Invalid Token" as expiry too.
- **Users must exist in the workspace**, and the Genie space must use viewer credentials.
- **In-process cache:** a restart or redeploy signs everyone out; multiple replicas or workers need a shared cache.

## Security model

- **Edge:** Easy Auth rejects every unauthenticated request (browser → Entra sign-in, API clients → 401), including `/` and `/health`. Only assigned users can sign in. `infra/verify.sh` asserts that no path returns 200 without a session; the deploy keeps ingress internal until Easy Auth is enforced.
- **App:**
  - Server-side tokens. The session cookie is signed, `HttpOnly`, `Secure` in Azure, `SameSite=Lax`, 8 h max age.
  - PKCE, `state` and nonce on the auth-code flow.
  - After sign-in, the MSAL cache entry is matched exactly by the id_token `oid` and tenant. It fails closed otherwise.
  - Easy Auth principal and session user are cross-checked.
  - An `Origin` check on every POST (CSRF).
  - Logout is a POST. It removes the user's cached tokens and ends the Easy Auth session.
  - Headers: CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`. Not `no-referrer`: browsers then send `Origin: null` on POST, and Easy Auth's CSRF check answers 403.
  - Strict id validation.
  - Generic error messages to the client. Upstream detail goes to the log only.
  - The expiry simulator (`/api/debug/expire`) is off unless `ENABLE_DEBUG=true`.
- **Data:** Databricks validates every token; Unity Catalog applies row filters and masks for the calling user; queries appear in the audit log under the user.
- **Logs:** see [Logs](#logs). No tokens, no question text, no access log (it would record OAuth codes).

## Production recommendations

- Network isolation: an **internal** Container Apps environment (workload profiles, VNet) behind **Azure Front Door Premium with Private Link and WAF**, or Application Gateway with WAF. Easy Auth controls who gets in, not where traffic comes from. Behind Front Door, set the auth proxy convention to standard forwarded headers, register the Front Door host's callbacks, and don't cache `/.auth/*`.
- Shared, encrypted token cache (for example Azure Cache for Redis with Entra auth, keyed by user, with a distributed refresh lock) to run more than one replica or worker.
- Secrets in Key Vault (`keyvaultref:` Container App secrets) instead of passing them on the deploy command line, or a federated credential instead of a client secret; managed identity for the app's own Azure access; one identity per downstream call: the user for governed data, a service principal for the app's own jobs.
- A server-side session store (revocable sessions) instead of signed client-side cookies, and log shipping to Log Analytics (this demo uses `--logs-destination none`; see [Logs](#logs)).
- `deploy.sh` keeps one live client secret per Container App (it deletes the previous one on each run).

## Security checklist

- [ ] `infra/verify.sh https://<app-fqdn>` passes after every deploy.
- [ ] Ingress is external only after Easy Auth is enforced (the deploy order enforces this).
- [ ] `ENABLE_DEBUG=false` outside demos.
- [ ] `ENTRA_ASSIGNED_USERS` lists only the users who need access, and Assignment required is on.
- [ ] The Genie space runs with viewer credentials.
- [ ] Each user is in one persona group (`setup/groups.sh` checks this).
- [ ] The Container App runs one replica (the token cache is in-process).
- [ ] `infra/config.local.env` and `.env` stay out of version control (gitignored).
- [ ] The client secret is rotated by re-running the deploy before it expires (1 year).
