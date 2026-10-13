#!/usr/bin/env python3
"""review_helper.py — the only command a pr-reviewer agent may run.

Review agents read untrusted PR content, so they get no general shell. Every
GitHub read, shared-context write, and the final colored render goes through
this script. The target PR comes from env vars set by spawn_agents.sh, not
from arguments, so an agent can't point it at a different repo or folder.

GitHub access uses the gh CLI, or, when spawn_agents.sh found gh missing or
logged out, the REST API with GITHUB_TOKEN (PR_REVIEW_BACKEND=token).

Subcommands:
  init                 (spawn script only) fetch pr.json + pr.diff, seed context files
  file <path> [--ref base|head]   print a repo file with line numbers
  tree [--depth N]     print the repo file tree at the PR head (default depth 2)
  repo-ctx             print the shared repo-level context JSON
  repo-update <patch>  merge a JSON patch file into the repo-level context under flock
  render               (spawn script only) print the colored review from context.json
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.parse import quote

SLUG = os.environ["PR_REVIEW_SLUG"]          # <org>/<repo>
NUM = int(os.environ["PR_REVIEW_NUM"])
PR_DIR = os.environ["PR_REVIEW_DIR"]          # $TMPDIR/pr-review-<uid>/<org>__<repo>-<num>
REPO_DIR = os.environ["PR_REVIEW_REPO_DIR"]   # ~/.claude/pr-review-cache/<org>__<repo>
USE_GH = os.environ.get("PR_REVIEW_BACKEND", "gh") == "gh"

# The agent's cwd is WORK_DIR, the only folder its file tools can write. pr.json
# and the rendered review stay one level up, out of the agent's reach.
WORK_DIR = os.path.join(PR_DIR, "work")
PR_JSON = os.path.join(PR_DIR, "pr.json")
PR_CTX = os.path.join(WORK_DIR, "context.json")
REPO_CTX = os.path.join(REPO_DIR, "repo-context.json")
LOCK = os.path.join(REPO_DIR, ".lock")


class GitHubError(Exception):
    pass


def gh(*args):
    try:
        return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as e:
        raise GitHubError(e.stderr.strip()) from e


def api(path, accept="application/vnd.github+json"):
    """GET a GitHub REST path via gh, or directly with GITHUB_TOKEN."""
    if USE_GH:
        return gh("api", "-H", f"Accept: {accept}", path)
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise GitHubError(f"GET {path}: {e}") from e


def pr_info():
    fields = "title,author,baseRefName,baseRefOid,headRefOid,additions,deletions,files,body,url"
    if USE_GH:
        return json.loads(gh("pr", "view", str(NUM), "--repo", SLUG, "--json", fields))
    p = json.loads(api(f"/repos/{SLUG}/pulls/{NUM}"))
    files = []
    for page in range(1, 31):  # GitHub lists at most 3000 files per PR
        batch = json.loads(api(f"/repos/{SLUG}/pulls/{NUM}/files?per_page=100&page={page}"))
        files += [{"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"],
                   "changeType": {"removed": "DELETED"}.get(f["status"], f["status"].upper())}
                  for f in batch]
        if len(batch) < 100:
            break
    # Same shape as `gh pr view --json`, so the rest of the helper is unchanged.
    return {"title": p["title"], "author": {"login": p["user"]["login"]},
            "baseRefName": p["base"]["ref"], "baseRefOid": p["base"]["sha"],
            "headRefOid": p["head"]["sha"], "additions": p["additions"],
            "deletions": p["deletions"], "files": files, "body": p["body"] or "",
            "url": p["html_url"]}


def pr_diff():
    if USE_GH:
        return gh("pr", "diff", str(NUM), "--repo", SLUG)
    return api(f"/repos/{SLUG}/pulls/{NUM}", accept="application/vnd.github.v3.diff")


def load(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


@contextmanager
def repo_lock():
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def empty_repo_ctx():
    return {
        "repo": SLUG, "topology_built": False, "default_branch": "", "languages": [],
        "frameworks": [], "infra_tooling": [], "db_drivers": [], "test_layout": "",
        "conventions": {}, "readme_summary": "", "directory_tree": [], "reviewed_prs": [],
    }


# Saved repo topology is re-mapped once it's this old, or the base branch has
# moved this many commits past the commit it was mapped at. Each convention
# also expires this many days after it was last written.
STALE_DAYS, STALE_COMMITS = 14, 100
SHA_RE = re.compile(r"[0-9a-f]{40}")


def stale_reason(ctx, pr):
    built_at, sha = ctx.get("topology_built_at"), ctx.get("topology_base_sha")
    if not built_at or not sha:
        return "no record of when it was mapped"
    age_days = (time.time() - built_at) / 86400
    if age_days > STALE_DAYS:
        return f"mapped {age_days:.0f} days ago"
    try:
        ahead = json.loads(api(f"/repos/{SLUG}/compare/{sha}...{pr['baseRefOid']}"))["ahead_by"]
    except (GitHubError, KeyError, ValueError):
        return "the commit it was mapped at is no longer comparable"
    if ahead > STALE_COMMITS:
        return f"base branch is {ahead} commits ahead"
    return None


def cmd_init():
    os.makedirs(WORK_DIR, exist_ok=True)
    pr = pr_info()
    for ref in ("headRefOid", "baseRefOid"):
        if not SHA_RE.fullmatch(pr[ref]):
            sys.exit(f"unexpected {ref}: {pr[ref]!r}")
    # The previous run's pr.json sits outside the agent's reach, so it reliably
    # records which commit context.json was built against. Start over if the
    # PR has new commits (or the provenance is unknown), so a re-run never
    # resumes, or shows, a review of an older revision.
    reviewed = load(PR_JSON, {}).get("headRefOid")
    if os.path.exists(PR_CTX) and reviewed != pr["headRefOid"]:
        print(f"PR head changed since the last review ({str(reviewed)[:7]} -> {pr['headRefOid'][:7]}); starting a fresh review.")
        os.remove(PR_CTX)
    # Drop the previous rendered review so the orchestrator's poll can't pick
    # it up before this run's render replaces it.
    if os.path.exists(os.path.join(PR_DIR, "review-output.md")):
        os.remove(os.path.join(PR_DIR, "review-output.md"))
    save(PR_JSON, pr)
    save(os.path.join(WORK_DIR, "pr.json"), pr)  # read-only copy for the agent
    with open(os.path.join(WORK_DIR, "pr.diff"), "w") as f:
        f.write(pr_diff())
    if not os.path.exists(PR_CTX):
        save(PR_CTX, {"pr_url": pr["url"], "repo": SLUG, "pr_number": NUM,
                      "passes_completed": [], "domains_touched": [], "findings": [],
                      "deep_dives": {}, "summary": "", "verdict": "", "confidence": "",
                      "reason": ""})
    with repo_lock():
        ctx = load(REPO_CTX, empty_repo_ctx())
        if NUM not in ctx["reviewed_prs"]:
            ctx["reviewed_prs"].append(NUM)
        # Expire conventions individually, so a poisoned one can't outlive
        # STALE_DAYS even while the topology stays fresh.
        written = ctx.setdefault("conventions_written_at", {})
        for name in list(ctx["conventions"]):
            if time.time() - written.get(name, 0) > STALE_DAYS * 86400:
                del ctx["conventions"][name]
                written.pop(name, None)
                ctx.get("sources", {}).pop(f"conventions.{name}", None)
        reason = ctx["topology_built"] and stale_reason(ctx, pr)
        if reason:
            # Re-map from scratch; conventions expire on their own clock above.
            print(f"Saved repo topology is stale ({reason}); it will be re-mapped.")
            fresh = empty_repo_ctx()
            for key in TOPOLOGY_KEYS:
                ctx[key] = fresh[key]
                ctx.get("sources", {}).pop(key, None)
        save(REPO_CTX, ctx)


def ref_sha(which):
    pr = load(PR_JSON, {})
    return pr["headRefOid"] if which == "head" else pr["baseRefName"]


def cmd_file(path, ref="head"):
    if path.startswith("/") or ".." in path.split("/"):
        sys.exit("path must be relative to the repo root")
    body = api(f"/repos/{SLUG}/contents/{quote(path)}?ref={quote(ref_sha(ref))}",
               accept="application/vnd.github.raw")
    for i, line in enumerate(body.splitlines(), 1):
        print(f"{i:6}\t{line}")


def cmd_tree(depth=2):
    tree = json.loads(api(f"/repos/{SLUG}/git/trees/{ref_sha('head')}?recursive=1"))
    for entry in tree["tree"]:
        if entry["path"].count("/") < depth:
            print(entry["path"] + ("/" if entry["type"] == "tree" else ""))


# Keys an agent may change in the shared repo context. repo and reviewed_prs
# are owned by init.
REPO_KEYS = {"topology_built", "default_branch", "languages", "frameworks", "infra_tooling",
             "db_drivers", "test_layout", "conventions", "readme_summary", "directory_tree"}
TOPOLOGY_KEYS = REPO_KEYS - {"conventions"}


# Shared context is read by every later review of the repo, so cap what one
# (possibly prompt-injected) agent can put there.
MAX_TEXT, MAX_ITEMS = 500, 200


def bounded(value, depth=0):
    # Below the top level, nested lists/dicts are flattened to capped text.
    if isinstance(value, str) or (depth > 0 and isinstance(value, (list, dict))):
        return clean(value if isinstance(value, str) else json.dumps(value))[:MAX_TEXT]
    if isinstance(value, list):
        return [bounded(v, depth + 1) for v in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {clean(k)[:100]: bounded(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    return value if isinstance(value, (bool, int, float)) or value is None else clean(value)[:MAX_TEXT]


def cmd_repo_update(patch_path):
    patch_path = os.path.realpath(patch_path)
    if os.path.dirname(patch_path) != os.path.realpath(WORK_DIR):
        sys.exit(f"patch file must be in {WORK_DIR}")
    patch = {k: bounded(v) for k, v in load(patch_path, {}).items() if k in REPO_KEYS}
    with repo_lock():
        ctx = load(REPO_CTX, empty_repo_ctx())
        # sources lists every PR whose agent changed each field or convention,
        # so a poisoned entry can be traced and cleared. Agents can't write it.
        sources = ctx.setdefault("sources", {})
        was_built = ctx.get("topology_built") is True

        def credit(name):
            prs = sources.setdefault(name, [])
            if NUM not in prs:
                prs.append(NUM)

        for key, val in patch.items():
            if key == "topology_built" and val is not True:
                # Only init clears topology_built (on staleness), so an agent
                # can't toggle it to restart the 14-day clock.
                continue
            if isinstance(ctx.get(key), list) and isinstance(val, list):
                for v in val:
                    if v not in ctx[key] and len(ctx[key]) < MAX_ITEMS:
                        ctx[key].append(v)
                        credit(key)
            elif isinstance(ctx.get(key), dict) and isinstance(val, dict):
                for name, v in val.items():
                    if ctx[key].get(name) != v and (name in ctx[key] or len(ctx[key]) < MAX_ITEMS):
                        ctx[key][name] = v
                        credit(f"{key}.{name}")
                        if key == "conventions":
                            ctx.setdefault("conventions_written_at", {})[name] = time.time()
            elif ctx.get(key) != val:
                ctx[key] = val
                credit(key)
        if ctx.get("topology_built") is True and not was_built:
            # Stamped by the helper from trusted pr.json, not by the agent.
            ctx["topology_built_at"] = time.time()
            ctx["topology_base_sha"] = load(PR_JSON, {}).get("baseRefOid", "")
        save(REPO_CTX, ctx)


C = {"CRITICAL": "\033[38;5;196m", "WARNING": "\033[38;5;208m", "SUGGESTION": "\033[38;5;75m",
     "PRAISE": "\033[38;5;120m", "HEADER": "\033[1;38;5;255m", "RESET": "\033[0m"}
V = {"APPROVE": C["PRAISE"], "REQUEST CHANGES": C["CRITICAL"], "NEEDS DISCUSSION": C["WARNING"]}
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(value):
    # Agent-written text may echo PR content; strip control bytes so it can't
    # inject terminal escape sequences when the review is cat'ed.
    return CONTROL.sub("", str(value if value is not None else ""))


def cmd_render():
    pr, ctx = load(PR_JSON, {}), load(PR_CTX, {})
    bar = C["HEADER"] + "═" * 63 + C["RESET"]
    print(bar)
    print(f"{C['HEADER']}PR REVIEW: {SLUG}#{NUM} — {clean(pr.get('title'))}{C['RESET']}")
    print(f"Author: {clean(pr.get('author', {}).get('login'))}  |  Base: {clean(pr.get('baseRefName'))}"
          f"  |  +{int(pr.get('additions', 0))}/-{int(pr.get('deletions', 0))}")
    print(f"Domains: {clean(', '.join(map(str, ctx.get('domains_touched', []))))}")
    print(bar + "\n")
    print("## SUMMARY\n" + clean(ctx.get("summary")) + "\n")
    sections = [("CRITICAL", "## CRITICAL  (block merge)"), ("WARNING", "## WARNINGS  (should address)"),
                ("SUGGESTION", "## SUGGESTIONS  (optional improvements)"), ("PRAISE", "## PRAISE  (good patterns)")]
    for sev, heading in sections:
        found = [f for f in ctx.get("findings", []) if f.get("severity") == sev]
        if not found:
            continue
        print(f"{C[sev]}{heading}{C['RESET']}")
        for f in found:
            loc = clean(f.get("file")) + (f":{clean(f['line'])}" if f.get("line") is not None else "")
            print(f"{C[sev]}[{clean(f.get('id'))}]{C['RESET']} {loc} — {clean(f.get('title'))}")
            print(f"     Why: {clean(f.get('explanation'))}")
            print(f"     Fix: {clean(f.get('fix'))}")
        print()
    if ctx.get("deep_dives"):
        print("## DOMAIN DEEP-DIVES\n")
        for domain, text in ctx["deep_dives"].items():
            print(f"### {clean(domain)}\n{clean(text)}\n")
    verdict = clean(ctx.get("verdict")) or "NEEDS DISCUSSION"
    print("## VERDICT")
    print(f"{V.get(verdict, '')}{verdict}{C['RESET']}")
    print(f"Confidence: {clean(ctx.get('confidence')) or 'LOW'}")
    print(f"Reason: {clean(ctx.get('reason'))}")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "init":
        cmd_init()
    elif cmd == "file" and args:
        cmd_file(args[0], args[2] if args[1:2] == ["--ref"] and len(args) > 2 else "head")
    elif cmd == "tree":
        cmd_tree(int(args[1]) if args[:1] == ["--depth"] and len(args) > 1 else 2)
    elif cmd == "repo-ctx":
        print(json.dumps(load(REPO_CTX, empty_repo_ctx()), indent=2))
    elif cmd == "repo-update" and args:
        cmd_repo_update(args[0])
    elif cmd == "render":
        cmd_render()
    else:
        sys.exit(__doc__)
