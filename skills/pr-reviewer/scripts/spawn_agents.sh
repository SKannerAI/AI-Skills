#!/usr/bin/env bash
# spawn_agents.sh — For each PR URL, open a new pane to the right of the
# current cmux workspace and run a Claude Code review agent inside it.
# Usage: spawn_agents.sh [--reset-repo-context] <PR_URL_1> [PR_URL_2] ...
#
# --reset-repo-context deletes the saved repo knowledge for each PR's repo
# before reviewing, e.g. if it looks wrong or was poisoned by a bad PR.
#
# cmux is a workspace-oriented GUI multiplexer. Rather than creating a new
# workspace per PR, this script splits the active workspace horizontally
# (right-direction pane) so all reviews live alongside the orchestrating
# session. Panes appear in the cmux GUI immediately; there is no attach step.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_PROMPT_FILE="$SKILL_DIR/references/agent-prompt.md"
CHECKLISTS_FILE="$SKILL_DIR/references/checklists.md"
HELPER="$SKILL_DIR/scripts/review_helper.py"
RUN_LABEL="pr-reviews-$(date +%s)"

# Repo knowledge persists across reboots; per-PR files are throwaway and live
# in the per-user temp dir (TMPDIR is per-user on macOS; the uid suffix keeps
# them separate on Linux, where TMPDIR is often unset and /tmp is shared).
CACHE_ROOT="$HOME/.claude/pr-review-cache"
TMP_ROOT="${TMPDIR:-/tmp}"
TMP_ROOT="${TMP_ROOT%/}/pr-review-$(id -u)"

RESET_REPO_CONTEXT=false
if [[ "${1:-}" == "--reset-repo-context" ]]; then
  RESET_REPO_CONTEXT=true
  shift
fi

if [[ $# -eq 0 ]]; then
  echo "Usage: spawn_agents.sh [--reset-repo-context] <PR_URL_1> [PR_URL_2] ..."
  exit 1
fi

# Create an owner-only (0700) dir, refusing one that's a symlink or owned by
# someone else, since another local user could pre-create a predictable path.
secure_dir() {
  [[ -e "$1" ]] || mkdir -m 700 "$1"
  if [[ -L "$1" || ! -d "$1" || ! -O "$1" ]]; then
    echo "ERROR: $1 is a symlink, not a directory, or not owned by you. Remove it and retry."
    exit 1
  fi
  chmod 700 "$1"
}

# Verify cmux
if ! command -v cmux &>/dev/null; then
  echo "ERROR: cmux is not installed. Install the cmux app from https://cmux.app"
  echo "       and ensure its CLI shim is on PATH."
  exit 1
fi

# GitHub access for review_helper.py (the agents' only route to GitHub): the gh
# CLI if it's installed and logged in, else the REST API with GITHUB_TOKEN. The
# token stays in the helper's environment; it never goes into a prompt or the
# command typed into the pane, so it must be exported in your shell profile.
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  PR_REVIEW_BACKEND=gh
elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
  PR_REVIEW_BACKEND=token
  echo "gh not available or not logged in; using the GitHub REST API with GITHUB_TOKEN."
else
  echo "ERROR: no GitHub access. Run 'gh auth login' (https://cli.github.com), or export GITHUB_TOKEN."
  exit 1
fi
export PR_REVIEW_BACKEND

# Verify claude CLI
if ! command -v claude &>/dev/null; then
  echo "ERROR: claude CLI not found. Install Claude Code first: https://docs.claude.ai/code"
  exit 1
fi

# Determine target workspace: prefer the env var cmux exports inside its own
# surfaces, fall back to the active workspace.
TARGET_WORKSPACE="${CMUX_WORKSPACE_ID:-$(cmux current-workspace)}"
if [[ -z "$TARGET_WORKSPACE" ]]; then
  echo "ERROR: could not determine current cmux workspace. Open this script from inside cmux,"
  echo "       or set CMUX_WORKSPACE_ID to a workspace ref (e.g. workspace:1)."
  exit 1
fi

# Validate every URL before spawning anything. Only the captured org/repo/number
# are used afterwards; the raw URL never reaches a shell command or a path.
URL_RE='^(https?://)?(www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/pull/([0-9]+)(/(files|commits|checks))?/?$'
ORGS=(); REPOS=(); NUMS=()
for url in "$@"; do
  if [[ "$url" =~ $URL_RE ]]; then
    org="${BASH_REMATCH[3]}"; repo="${BASH_REMATCH[4]}"; num="${BASH_REMATCH[5]}"
  else
    repo="."
  fi
  if [[ "$repo" =~ ^\.+$ ]]; then
    echo "ERROR: not a GitHub PR URL: $url"
    echo "       Expected github.com/<org>/<repo>/pull/<number>, with URLs separated by spaces."
    exit 1
  fi
  ORGS+=("$org"); REPOS+=("$repo"); NUMS+=("$num")
done

echo "=== PR Review Run: $RUN_LABEL ==="
echo "Target workspace: $TARGET_WORKSPACE"
echo "PRs under review:"
for i in "${!NUMS[@]}"; do
  echo "  https://github.com/${ORGS[$i]}/${REPOS[$i]}/pull/${NUMS[$i]}"
done
echo ""

secure_dir "$CACHE_ROOT"
secure_dir "$TMP_ROOT"
RESET_DONE=" "

for i in "${!NUMS[@]}"; do
  ORG="${ORGS[$i]}"; REPO="${REPOS[$i]}"; PR_NUM="${NUMS[$i]}"
  PR_URL="https://github.com/${ORG}/${REPO}/pull/${PR_NUM}"
  PANE_LABEL="${REPO}#${PR_NUM}"
  REPO_SLUG="${ORG}/${REPO}"
  REPO_KEY="${ORG}__${REPO}"

  # Per-PR context dir. The agent works in its work/ subfolder, the only place
  # its file tools can touch; trusted pr.json and the rendered review stay here.
  CONTEXT_DIR="$TMP_ROOT/${REPO_KEY}-${PR_NUM}"
  secure_dir "$CONTEXT_DIR"
  secure_dir "$CONTEXT_DIR/work"
  # Resolve symlinks (macOS /var -> /private/var) so the paths in the prompt
  # match the cwd the ./** permission rules are evaluated against.
  CONTEXT_DIR="$(cd "$CONTEXT_DIR" && pwd -P)"
  WORK_DIR="$CONTEXT_DIR/work"

  # Shared repo-level context dir (one per <org>/<repo>, shared by all PRs of
  # that repo, kept across runs). Agents reach it only through
  # review_helper.py, which locks it. Reset at most once per repo per run.
  REPO_CONTEXT_DIR="$CACHE_ROOT/${REPO_KEY}"
  if $RESET_REPO_CONTEXT && [[ "$RESET_DONE" != *" $REPO_KEY "* ]]; then
    echo "Resetting saved repo context for $REPO_SLUG"
    rm -rf "$REPO_CONTEXT_DIR"
    RESET_DONE+="$REPO_KEY "
  fi
  secure_dir "$REPO_CONTEXT_DIR"

  export PR_REVIEW_SLUG="$REPO_SLUG" PR_REVIEW_NUM="$PR_NUM" \
         PR_REVIEW_DIR="$CONTEXT_DIR" PR_REVIEW_REPO_DIR="$REPO_CONTEXT_DIR"

  # Fetch PR metadata + diff and seed both context files before the agent
  # starts, so the agent itself never needs gh or the token.
  python3 "$HELPER" init
  cp "$CHECKLISTS_FILE" "$WORK_DIR/checklists.md"

  PROMPT=$(cat "$AGENT_PROMPT_FILE")
  FULL_PROMPT="${PROMPT//<HELPER>/$HELPER}

PR URL to review: $PR_URL
Repo slug: $REPO_SLUG
PR number: $PR_NUM
Working directory (pr.json, pr.diff, checklists.md, context.json): $WORK_DIR
Helper command: python3 $HELPER
"
  printf '%s' "$FULL_PROMPT" > "$CONTEXT_DIR/prompt.txt"

  # The agent reads PR content written by the PR author, so it runs locked
  # down instead of with bypassPermissions: --restricted ignores user/project
  # settings and confines file tools to WORK_DIR (the ./** rules say so
  # explicitly too; Edit rules also cover Write), --strict-mcp-config drops
  # MCP servers, and dontAsk denies any Bash command except the helper. The
  # colored review is rendered by the helper after the agent exits, so the
  # agent never prints terminal escapes itself; its raw output goes to a log.
  printf -v ENV_CMD 'export PR_REVIEW_SLUG=%q PR_REVIEW_NUM=%q PR_REVIEW_DIR=%q PR_REVIEW_REPO_DIR=%q PR_REVIEW_BACKEND=%q' \
    "$REPO_SLUG" "$PR_NUM" "$CONTEXT_DIR" "$REPO_CONTEXT_DIR" "$PR_REVIEW_BACKEND"
  printf -v CLAUDE_CMD 'claude -p --restricted --strict-mcp-config --permission-mode dontAsk --tools %q --allowedTools %q "Read(./**)" "Grep(./**)" "Glob(./**)" "Edit(./**)" < ../prompt.txt > ../agent-log.md 2>&1' \
    "Bash,Read,Grep,Glob,Write,Edit" "Bash(python3 $HELPER *)"
  printf -v RENDER_CMD 'python3 %q render | tee ../review-output.md' "$HELPER"
  printf -v PANE_CMD 'cd %q && %s && echo "Reviewing %s..." && %s; %s; exec $SHELL' \
    "$WORK_DIR" "$ENV_CMD" "$PANE_LABEL" "$CLAUDE_CMD" "$RENDER_CMD"

  echo "Spawning review pane for: $PR_URL  ->  $PANE_LABEL"
  echo "  Output: $CONTEXT_DIR/review-output.md"

  # Split the workspace to the right; capture the new surface ref so we can
  # pipe the claude command into it. Output is `OK surface:N pane:N workspace:N`.
  NEW_PANE_OUTPUT=$(cmux new-pane --direction right --workspace "$TARGET_WORKSPACE" --focus false)
  NEW_SURFACE=$(echo "$NEW_PANE_OUTPUT" | grep -oE 'surface:[0-9]+' | head -1)

  if [[ -z "$NEW_SURFACE" ]]; then
    echo "ERROR: failed to identify new surface from cmux new-pane output:"
    echo "       $NEW_PANE_OUTPUT"
    exit 1
  fi

  # Rename the new pane's tab to "Review <repo>#<PR_NUM>" so the repo is
  # visible in the cmux sidebar — handy when several PRs from different repos
  # are being reviewed at once.
  cmux rename-tab --workspace "$TARGET_WORKSPACE" --surface "$NEW_SURFACE" "Review ${PANE_LABEL}"

  # Send the command into the new pane and execute it (\n triggers Enter).
  cmux send --workspace "$TARGET_WORKSPACE" --surface "$NEW_SURFACE" "$PANE_CMD"$'\n'
done

echo ""
echo "All review panes spawned in workspace $TARGET_WORKSPACE."
echo "Each review is tee'd to the Output path printed above."
