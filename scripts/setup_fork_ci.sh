#!/usr/bin/env bash
# Prepare the fork for automated remediation PRs:
#   1. Disable every workflow inherited from apache/superset, so the only CI
#      signal on PRs is ours ("green" stays well-defined and quota stays safe).
#   2. Install the focused pin-audit-ci workflow on the default branch.
# Idempotent: safe to re-run.
set -euo pipefail

FORK="${GS_FORK:-agrimsingh/superset}"
BRANCH="${GS_DEFAULT_BRANCH:-master}"
WORKFLOW_PATH=".github/workflows/pin-audit-ci.yml"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_FILE="$SCRIPT_DIR/../fork-ci/pin-audit-ci.yml"

echo "==> enabling Actions on $FORK"
gh api -X PUT "repos/$FORK/actions/permissions" \
  -F enabled=true \
  -f allowed_actions=selected \
  -F sha_pinning_required=true
printf '%s\n' \
  '{"github_owned_allowed":true,"verified_allowed":false,"patterns_allowed":[]}' |
  gh api -X PUT "repos/$FORK/actions/permissions/selected-actions" --input -
policy_ok=$(gh api "repos/$FORK/actions/permissions" --jq \
  'select(.enabled == true and .allowed_actions == "selected" and .sha_pinning_required == true) | "ok"')
selected_ok=$(gh api "repos/$FORK/actions/permissions/selected-actions" --jq \
  'select(.github_owned_allowed == true and .verified_allowed == false and ((.patterns_allowed // []) | length) == 0) | "ok"')
if [ "$policy_ok" != ok ] || [ "$selected_ok" != ok ]; then
  echo "Actions permissions on $FORK are broader than the required policy" >&2
  exit 1
fi
echo "verified Actions allow only SHA-pinned GitHub-owned actions"

echo "==> pushing $WORKFLOW_PATH to $BRANCH"
existing_sha=$(gh api "repos/$FORK/contents/$WORKFLOW_PATH?ref=$BRANCH" --jq '.sha' 2>/dev/null || true)
args=(-f message="ci: add focused pin-audit workflow" -f branch="$BRANCH"
      -f content="$(base64 < "$LOCAL_FILE")")
[ -n "$existing_sha" ] && args+=(-f sha="$existing_sha")
gh api -X PUT "repos/$FORK/contents/$WORKFLOW_PATH" "${args[@]}" --jq '.commit.sha'
if ! cmp -s "$LOCAL_FILE" <(
  gh api "repos/$FORK/contents/$WORKFLOW_PATH?ref=$BRANCH" \
    -H "Accept: application/vnd.github.raw+json"
); then
  echo "uploaded workflow does not match $LOCAL_FILE" >&2
  exit 1
fi
echo "verified $WORKFLOW_PATH matches the tracked template"

echo "==> disabling inherited workflows"
gh api --paginate "repos/$FORK/actions/workflows" --jq '.workflows[] | "\(.id) \(.state) \(.path)"' |
while read -r id state path; do
  if [ "$path" = "$WORKFLOW_PATH" ]; then
    gh api -X PUT "repos/$FORK/actions/workflows/$id/enable" >/dev/null
    echo "enabled  $path"
  elif [ "$state" = "active" ]; then
    gh api -X PUT "repos/$FORK/actions/workflows/$id/disable" >/dev/null
    echo "disabled $path"
  else
    echo "skipped  $path ($state)"
  fi
done
echo "==> done"
