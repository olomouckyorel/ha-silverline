# CLAUDE.md — AI working notes for ha-silverline

Supplemental instructions for Claude Code and other AI assistants working in this repo.
Extends the global `~/.claude/CLAUDE.md`; if they conflict, this file wins.

---

## Release procedure

### Overview

There are **two git remotes**:

| remote   | URL                                       | purpose                          |
|----------|-------------------------------------------|----------------------------------|
| `origin` | `ssh://git.alpha-labs.net:2222/...`       | internal Gitea (primary dev)     |
| `github` | `git@github.com:christianreiss/ha-silverline.git` | public GitHub (HACS / PyPI) |

GitHub Actions only run on the **`github`** remote. Always push to both.

### Two independent release pipelines (both tag-driven on `github`)

| workflow                  | trigger tag          | produces                      |
|---------------------------|----------------------|-------------------------------|
| `release.yaml`            | `v*.*.*`             | GitHub Release + zip asset    |
| `pysilverline-pypi.yaml`  | `pysilverline-v*.*.*`| PyPI package `pysilverline`   |

**Cutting a release = pushing the matching tag to `github`.**
Bumping a version number in a commit does nothing on its own.

### Correct upgrade path (step by step)

1. Bump versions:
   - `pysilverline/pyproject.toml` → `version = "X.Y.Z"`
   - `pysilverline/src/pysilverline/__init__.py` → `__version__ = "X.Y.Z"`
   - `custom_components/poolex_silverline/manifest.json` → `"version": "A.B.C"` and `"requirements": ["pysilverline==X.Y.Z"]`

2. Commit the version bump (one commit, both bumps together).

3. Push to **both** remotes — but **never push tags manually to `github`**:

   ```bash
   git push github main
   git push origin main
   # optionally keep Gitea tags in sync:
   git push origin --tags
   ```

4. The `auto-release.yaml` workflow on GitHub Actions detects the version bump
   and creates the tags (`vA.B.C` and `pysilverline-vX.Y.Z`) itself.

5. Those tags trigger `release.yaml` (GitHub Release) and `pysilverline-pypi.yaml` (PyPI).

### Ordering constraint

The integration `manifest.json` pins `pysilverline==X.Y.Z`.
**PyPI must be live before the HACS release is usable.**
The tag-based pipelines both fire from the same commit push, so they
race — in practice PyPI finishes first (~49 s) before anyone installs,
but be aware of this if something goes wrong.

### RELEASE_PAT — why it matters

`auto-release.yaml` pushes tags with `secrets.RELEASE_PAT` (falls back to
`github.token`). GitHub's anti-recursion rule: **a tag pushed with
`GITHUB_TOKEN` does NOT trigger other workflows**. Without `RELEASE_PAT`:

- tags are created on GitHub ✓
- `release.yaml` and `pysilverline-pypi.yaml` do NOT fire ✗

**Setup (one-time):**
1. Create a fine-grained PAT scoped to this repo,
   `Repository permissions → Contents: Read and write`.
2. Add it as a repository secret named `RELEASE_PAT`
   (Settings → Secrets and variables → Actions).

### Manual recovery (when RELEASE_PAT is absent)

If tags exist on GitHub but releases didn't fire, trigger them by hand:

- **GitHub Release:** Actions → Release → "Run workflow" → enter tag (e.g. `v0.8.2`) → Run
- **PyPI:** Actions → Publish pysilverline to PyPI → "Run workflow" → Run
  (no input needed; builds from the current HEAD's `pyproject.toml` version)

Both workflows have `workflow_dispatch` enabled for exactly this scenario.

---

## Pre-commit hook

The repo uses a custom hooks path (`git config core.hooksPath`).
The pre-commit hook runs the full pysilverline test suite (v3.3 + v3.5 API tests).
**Never use `--no-verify`** — the hook catches protocol-level regressions.

---

## Key files

| path | purpose |
|------|---------|
| `custom_components/poolex_silverline/` | HA integration (HACS) |
| `pysilverline/` | standalone library published to PyPI |
| `.github/workflows/auto-release.yaml` | creates tags on version bump |
| `.github/workflows/release.yaml` | builds GitHub Release from `v*` tag |
| `.github/workflows/pysilverline-pypi.yaml` | publishes library to PyPI |
| `GUIDELINES.md` | HA integration idioms and quality-scale rules |
