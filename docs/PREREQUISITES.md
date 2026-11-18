# Prerequisites

Check every row before step 1 of [DEPLOYMENT.md](DEPLOYMENT.md). Each row names the role that usually holds it. In a customer tenant, the Entra rows need an Entra admin.

## Azure infrastructure

Created by `infra/deploy.sh`.

| Resource | Purpose | Notes | Role |
|---|---|---|---|
| Resource group | Holds everything | Any region with Container Apps | Azure subscription Contributor |
| Container Apps environment | Hosts the app | Consumption plan is enough for the demo | Azure subscription Contributor |
| Azure Container Registry (Basic) | Image built in the cloud with `az acr build` | No local Docker needed; app pulls with its system-assigned identity | Azure subscription Contributor |
| Container App | The FastAPI BFF | 1 replica / 1 worker in this version (in-process token cache) | Azure subscription Contributor |
| Container App secrets | Entra client secret, session-cookie secret | Never stored in the repo | Azure subscription Contributor |
| Easy Auth (built-in authentication) | Microsoft Entra provider, Require authentication, HTTPS only | Configured before ingress becomes external | Azure subscription Contributor |

## Microsoft Entra ID

| Requirement | Detail | Role |
|---|---|---|
| App registration (single tenant) | Created by the script, or reused via `ENTRA_APP_ID`. Must be in the **same tenant as the workspace**. | Application developer, or owner of the existing app |
| API permission | Delegated **Azure Databricks → `user_impersonation`**. The script adds it; it does not grant consent. | Added by the script run |
| Admin consent | Users consent on first sign-in if your tenant allows user consent for it. Otherwise an admin grants consent once. | Cloud application admin (if user consent is off) |
| Redirect URIs | `https://<app-fqdn>/auth/callback` (app) and `https://<app-fqdn>/.auth/login/aad/callback` (Easy Auth). ID token issuance on. The script adds them. | Owner of the app registration |
| Assignment required | Set on the enterprise application, with the users or groups allowed to sign in (`ENTRA_ASSIGNED_USERS`). | Application admin, or owner of the enterprise app |

## Azure Databricks

| Requirement | Detail | Role |
|---|---|---|
| Workspace with Unity Catalog | In the same Entra tenant as the app. | Workspace admin |
| SQL warehouse | Serverless recommended. Users need `CAN USE` (`--warehouse`). | Workspace admin |
| Genie space with viewer credentials | Each user's own identity runs the SQL. If the space embeds the publisher's credentials, the SQL runs as the publisher and row filters never see the user. | Workspace admin |
| Users in the workspace | Must exist in the workspace (SCIM or automatic identity management). Users added only through a group may need provisioning or one sign-in. | Workspace admin |
| Account-level persona groups | Two groups, `genie_obo_east` and `genie_obo_west` by default. Unity Catalog does not accept workspace-local groups. Created by `setup/groups.sh`. | Account admin (CLI profile) |
| Table grants | `SELECT` on the demo table, plus `USE CATALOG` and `USE SCHEMA`, for both groups. `setup/setup_workspace.py` prints the grants; you run them. | Catalog owner, or a user with `MANAGE` on the catalog |

## Local tools

| Tool | Tested with | Used for |
|---|---|---|
| Azure CLI + `containerapp` extension | 2.91.0 / 1.3.0b6 | `az login` to the target tenant; deploy |
| Databricks CLI | v1.26.0 | Account profile for `setup/groups.sh` |
| Python | 3.12 (app image) | Local venv for setup scripts and local run |
| bash, curl, openssl | macOS default bash 3.2 is supported | `infra/deploy.sh`, `infra/verify.sh` |
