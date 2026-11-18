#!/usr/bin/env bash
# Account groups for the row-filter personas + workspace assignment. Needs an account-admin CLI profile.
# Usage: [GROUP_PREFIX=genie_obo] setup/groups.sh <account-profile> <workspace-id> <member-user-email> [east|west]
#   Puts the member in <prefix>_<persona> (default east) and removes them from the other group.
set -euo pipefail
PREFIX=${GROUP_PREFIX:-genie_obo}
P=${1:?account profile}; WS=${2:?workspace id}; EMAIL=${3:?member email}; PERSONA=${4:-east}
[[ "$EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || { echo "invalid email"; exit 1; }
[[ "$PERSONA" == east || "$PERSONA" == west ]] || { echo "persona must be east|west"; exit 1; }
db() { databricks "$@" -p "$P"; }

UID_=$(db account users list --filter "userName eq \"$EMAIL\"" -o json | python3 -c "import json,sys;print(json.load(sys.stdin)[0]['id'])")

group_id() {
  db account groups list --filter "displayName eq \"$1\"" -o json | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[0]['id'] if d else '')"
}

for g in "${PREFIX}_east" "${PREFIX}_west"; do
  gid=$(group_id "$g")
  if [[ -z "$gid" ]]; then
    gid=$(db account groups create --display-name "$g" -o json | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
    echo "created $g ($gid)"
  fi
  db account workspace-assignment update "$WS" "$gid" --json '{"permissions":["USER"]}' -o json >/dev/null
  if [[ "$g" == "${PREFIX}_$PERSONA" ]]; then
    op='{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"add","path":"members","value":[{"value":"'"$UID_"'"}]}]}'
  else
    op='{"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"],"Operations":[{"op":"remove","path":"members[value eq \"'"$UID_"'\"]"}]}'
  fi
  db account groups patch "$gid" --json "$op" >/dev/null
  echo "$g: assigned to workspace $WS; $EMAIL $( [[ "$g" == "${PREFIX}_$PERSONA" ]] && echo added || echo removed )"
done

# Read back: the member must be in exactly one persona group (both = unmasked data + both regions).
in_groups=0
for g in "${PREFIX}_east" "${PREFIX}_west"; do
  gid=$(group_id "$g")
  if db account groups get "$gid" -o json | python3 -c "import json,sys;m=[x['value'] for x in json.load(sys.stdin).get('members',[])];sys.exit(0 if '$UID_' in m else 1)"; then
    in_groups=$((in_groups + 1)); echo "verified: member of $g"
  fi
done
[[ $in_groups == 1 ]] || { echo "ERROR: $EMAIL is in $in_groups persona groups (expected 1)"; exit 1; }
