# Checklist: before changing GitHub visibility to public

Do **not** flip the repo to public until every item below is green.

## Secrets and local state

- [ ] `.env` is gitignored and not tracked (`git check-ignore -v .env`)
- [ ] `data/`, `*.db`, `*.db-journal` are gitignored
- [ ] No real tokens (`sk-`, `ghp_`, `glpat-`, Bearer values) in tracked files
- [ ] `pre-commit` / CI runs **gitleaks** on the tree

## Registry (SoT)

- [ ] Live `config/registry.json` is **gitignored** (deploy-only)
- [ ] Public template is `config/registry.json.example` with `${VAR}` placeholders only
- [ ] Fresh clone path works: `cp config/registry.json.example config/registry.json` then edit `.env`
- [ ] No LAN IPs (`192.168.x.x`) or internal hostnames in tracked registry / examples

## Documentation

- [ ] README / docs use `<ROUTER_HOST>` or `localhost`, not private LAN addresses
- [ ] Internal GitLab/other hostnames are env placeholders (`${GITLAB_API_URL}`), not hard-coded
- [ ] Any remaining site-specific notes are explicitly marked as private deploy examples

## Git history (important)

Untracking a file does **not** remove it from old commits. Before `visibility=public`:

- [ ] Search history: `git log -p --all -S '192.168.0' -- '*.json' '*.md' '*.env*'`
- [ ] Search for token shapes: `git log -p --all -S 'sk-' -S 'ghp_' -S 'glpat-'`
- [ ] If old commits still contain LAN registry or secrets, rewrite/purge history (e.g. `git filter-repo` / BFG) **or** accept residual exposure and rotate anything that ever lived in those blobs

## Clean repo / residual PR refs (recreate gate)

GitHub keeps `refs/pull/*/head` after ordinary history rewrites; those refs are
read-only via the API (422). A repo that once held LAN registry blobs or secrets
in **any** reachable tip (including old PR heads) is not safe to flip public
until those refs are gone.

- [ ] Default branch tip is the intended orphan/clean history (not a pre-purge tip)
- [ ] `git ls-remote origin 'refs/pull/*/head'` returns **empty** (0 lines)
- [ ] If pull refs remain: recreate the GitHub repo from the clean orphan `main`
      (archive the old repo private; do **not** import old PRs / mirror the dirty remote)
- [ ] After recreate, Multica / local remotes and any bare cache point at the **new**
      `remdek/mcp-router` and `refs/heads/main` matches `origin/main`

## Final gate

- [ ] `git status` clean; PR merged
- [ ] One last `gitleaks detect --source . --no-git` (or pre-commit) on the default branch
- [ ] Pull-refs check above is green (clean repo)
- [ ] Only then change GitHub visibility
