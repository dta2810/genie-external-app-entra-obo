"""Idempotent demo setup: claims table + row filter + mask, Genie space.

Usage: DATABRICKS_HOST=<ws> DATABRICKS_AUTH_TYPE=azure-cli python setup/setup_workspace.py \
         --catalog <catalog> --warehouse <id> [--schema genie_obo_demo]
Groups (account level, see setup/groups.sh): <prefix>_east, <prefix>_west (--group-prefix, default genie_obo).
Grants are printed, not applied (run them yourself).
"""

import argparse
import json
import uuid

from databricks.sdk import WorkspaceClient

p = argparse.ArgumentParser()
p.add_argument("--catalog", required=True)
p.add_argument("--schema", default="genie_obo_demo")
p.add_argument("--warehouse", required=True)
p.add_argument("--space-id", help="update this space instead of creating one")
p.add_argument("--group-prefix", default="genie_obo")
a = p.parse_args()
fq = f"{a.catalog}.{a.schema}"
east, west = f"{a.group_prefix}_east", f"{a.group_prefix}_west"
w = WorkspaceClient()


def sql(stmt: str):
    r = w.statement_execution.execute_statement(statement=stmt, warehouse_id=a.warehouse, wait_timeout="50s")
    if r.status.state.value != "SUCCEEDED":
        raise SystemExit(f"FAILED: {stmt[:80]}… {r.status.error}")
    return r.result.data_array if r.result else None


for stmt in [
    f"CREATE SCHEMA IF NOT EXISTS {fq}",
    f"""CREATE OR REPLACE FUNCTION {fq}.region_filter(region STRING) RETURN
        (region = 'east' AND is_account_group_member('{east}'))
        OR (region = 'west' AND is_account_group_member('{west}'))""",
    f"""CREATE OR REPLACE FUNCTION {fq}.mask_national_id(v STRING) RETURN
        CASE WHEN is_account_group_member('{east}') AND is_account_group_member('{west}')
             THEN v ELSE concat('***-**-', right(v, 4)) END""",
    f"""CREATE OR REPLACE TABLE {fq}.claims (
        id INT, region STRING, customer STRING, national_id STRING, amount DECIMAL(12,2), claim_date DATE)""",
    f"""INSERT INTO {fq}.claims VALUES
        (1,'east','Acme Retail','123-45-6701',1200.50,'2026-09-03'),
        (2,'east','Blue Harbor','123-45-6702',830.00,'2026-09-17'),
        (3,'east','Cedar Labs','123-45-6703',4410.75,'2026-10-02'),
        (4,'east','Acme Retail','123-45-6704',260.00,'2026-10-21'),
        (5,'west','Delta Foods','987-65-4301',3150.00,'2026-09-08'),
        (6,'west','Evergreen Co','987-65-4302',990.90,'2026-09-29'),
        (7,'west','Fjord Systems','987-65-4303',5600.00,'2026-10-11'),
        (8,'west','Delta Foods','987-65-4304',415.25,'2026-11-02')""",
    f"ALTER TABLE {fq}.claims SET ROW FILTER {fq}.region_filter ON (region)",
    f"ALTER TABLE {fq}.claims ALTER COLUMN national_id SET MASK {fq}.mask_national_id",
]:
    sql(stmt)
print("table ready:", f"{fq}.claims")

h = lambda: uuid.uuid4().hex
space = {
    "version": 2,
    "data_sources": {"tables": [{"identifier": f"{fq}.claims",
                                 "description": ["Insurance claims by region (east/west). Amounts in USD."]}]},
    "config": {"sample_questions": sorted([
        {"id": h(), "question": ["How many claims are there by region?"]},
        {"id": h(), "question": ["What is the total claim amount by customer?"]},
    ], key=lambda x: x["id"])},
    "instructions": {"text_instructions": [{"id": h(), "content": ["Answer only from the claims table."]}]},
}
body = {"warehouse_id": a.warehouse, "title": "Genie External OBO Demo (claims)",
        "description": "External Azure app calls Genie as the Entra user; row filter per region group.",
        "serialized_space": json.dumps(space)}
if a.space_id:
    r = w.api_client.do("PATCH", f"/api/2.0/genie/spaces/{a.space_id}", body=body)
else:
    r = w.api_client.do("POST", "/api/2.0/genie/spaces", body=body)
print("genie space:", r.get("space_id"))

print("\n-- Grants to run yourself (warehouse SQL editor or CLI):")
for g in (east, west):
    print(f"GRANT USE CATALOG ON CATALOG {a.catalog} TO `{g}`;")
    print(f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{g}`;")
    print(f"GRANT SELECT ON TABLE {fq}.claims TO `{g}`;")
print("-- + warehouse CAN_USE and Genie space CAN_RUN for both groups")
