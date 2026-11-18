import os
import sys
from pathlib import Path

# Dummy config so tests run on a clean clone (no .env, no network).
for k, v in {"AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000", "AZURE_CLIENT_ID": "test-client",
             "AZURE_CLIENT_SECRET": "test-secret", "SESSION_SECRET": "test-session", "GENIE_SPACE_ID": "0" * 32,
             "REDIRECT_URI": "http://localhost:8000/auth/callback", "HTTPS_ONLY": "false",
             "DATABRICKS_HOST": "https://adb-0000000000000000.0.azuredatabricks.net"}.items():
    os.environ[k] = v
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
