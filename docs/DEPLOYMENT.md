# Deployment

Deploy in phases, in this order. Go through [PREREQUISITES.md](PREREQUISITES.md) first.

## 1. Laptop setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r setup/requirements.txt -r requirements-dev.txt
az login --tenant <entra-tenant-id>
cp infra/config.example.env infra/config.local.env    # fill in; gitignored
```

`tests/` holds unit tests that need no network: run `.venv/bin/python -m pytest -q`. pytest is in `requirements-dev.txt`, not in the image.

## 2. Workspace demo: data, row filter, mask, Genie space

```bash
DATABRICKS_HOST=https://adb-<workspace-id>.<n>.azuredatabricks.net DATABRICKS_AUTH_TYPE=azure-cli \
  .venv/bin/python setup/setup_workspace.py --catalog <catalog> --warehouse <warehouse-id>
```

The script is idempotent. It:

1. Creates the schema `<catalog>.genie_obo_demo` (change with `--schema`).
2. Creates the row filter function `region_filter` and the mask function `mask_national_id`.
3. Creates and loads `claims`: 8 rows, 4 in `east` and 4 in `west`.
4. Sets the row filter on `region` and the mask on `national_id`.
5. Creates the Genie space "Genie External OBO Demo (claims)". Use `--space-id` to update an existing space instead.

It prints the grants and does not run them. Run them after step 3 (the groups must exist). If you changed the prefix with `--group-prefix`, use the same prefix in the grants.

- For both groups: `USE CATALOG`, `USE SCHEMA`, and `SELECT` on `claims`.
- Warehouse `CAN USE` and Genie space `CAN RUN` for both groups.

## 3. Persona groups

```bash
setup/groups.sh <account-profile> <workspace-id> <you@company.com> east
```

The script:

- Creates `genie_obo_east` and `genie_obo_west` at account level if they do not exist. Set `GROUP_PREFIX` to change the prefix.
- Assigns both groups to the workspace as `USER`.
- Puts the member in the chosen persona group and removes them from the other.
- Reads back the groups and fails unless the member is in exactly one persona group.

Switch persona during a demo by running it again with `west`. See [EXAMPLE_USAGE.md](EXAMPLE_USAGE.md).

## 4. Azure deploy

```bash
infra/deploy.sh infra/config.local.env
```

Run it again to update an existing deploy. The script runs these steps in order:

1. **Internal ingress first.** Creates the resource group, the Container Apps environment (`--logs-destination none`), and the ACR image (Basic SKU, cloud build with `az acr build`).
2. **Entra app.** Creates the app registration if `ENTRA_APP_ID` is empty. Adds the redirect URIs and turns on ID token issuance. Deletes this app's previous deploy credentials and mints one client secret valid 1 year.
3. **Container App.** Created with `--ingress internal`. Secrets: `client-secret` and `session-secret` (random, from `openssl`). The env vars include `REQUIRE_EASY_AUTH=true` and `HTTPS_ONLY=true`.
4. **Easy Auth.** Sets the Microsoft provider, require authentication (redirect to login), and HTTPS only.
5. **Enforcement check.** Reads the Easy Auth state back. If it is not on with redirect to login, the script exits and ingress stays internal.
6. **Assignment required.** Sets `appRoleAssignmentRequired=true` on the service principal. Assigns each UPN in `ENTRA_ASSIGNED_USERS` (skips existing assignments).
7. **External ingress.** Turns on external ingress on port 8000.
8. **Verify.** Runs `infra/verify.sh https://<app-fqdn>`, the negative tests. See [OPERATIONS.md](OPERATIONS.md).

The script prints the app URL as `https://<app-fqdn>`.

## 5. Run locally (optional)

```bash
cp .env.example .env    # fill in; gitignored
.venv/bin/uvicorn app:app --port 8000 --no-access-log
```

- The Entra app needs `http://localhost:8000/auth/callback` as a redirect URI. The deploy script does not add it.
- `.env.example` sets `HTTPS_ONLY=false` and `REQUIRE_EASY_AUTH=false`. A local run has no platform gate. Keep it on localhost.

## Configuration

`infra/config.example.env` holds every deploy value: subscription, tenant, region, names, replicas, Entra app, assigned users, Databricks host, Genie space. The app reads only environment variables:

| Variable | Meaning |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Entra app registration (secret via Container App secret) |
| `REDIRECT_URI` | `https://<fqdn>/auth/callback` |
| `SESSION_SECRET` | Signs the session cookie |
| `DATABRICKS_HOST`, `GENIE_SPACE_ID` | Target workspace and Genie space. The app exits at start unless `DATABRICKS_HOST` is `https://<workspace>.azuredatabricks.net` |
| `HTTPS_ONLY` | `true` in Azure, `false` for local HTTP |
| `REQUIRE_EASY_AUTH` | `true` behind Easy Auth: requires and cross-checks the platform principal |
| `MAX_ATTEMPTS`, `MAX_ROWS` | Retry attempts (default 5), rows returned to the UI (default 200) |
| `ENABLE_DEBUG` | `true` only for demos: turns on the token-expiry simulator |
| `SESSION_MAX_AGE_SECS` | Session cookie lifetime (default 8 h) |
