# Git hooks

These hooks run when you use this repo (e.g. on push).

## Version check for release tags

The **pre-push** hook checks that when you push a tag matching `v*` (e.g. `v2.0.6`), the commit that tag points to has the same version in both:

- `pyproject.toml` — `version = "X.Y.Z"`
- `installer.iss` — `#define AppVersion "X.Y.Z"`

If they don’t match the tag, the push is aborted so the CI build doesn’t fail.

## Architecture compliance on branch pushes

When you push a branch, the **pre-push** hook runs `acf-check-diff --base origin/master --call-site precommit`. This checks the branch's changes against the mandates in `config/architecture/mandates.yml` that declare the `precommit` site, for example a bare `except:`. A finding blocks the push. So does a refusal, such as an `origin/master` it cannot resolve.

- Install the gate into the Python that has the project's dependencies: `pip install "git+https://github.com/IanDBallard/arch-compliance-for-superpowers@v0.3.4"`. That is the same plugin version CI runs.
- The hook compares with your local `origin/master` and doesn't fetch it. It also reads changed files from the working tree, so uncommitted edits can move what it reports. The pull request's CI job checks exactly what merges.
- Where the package can't be installed (a cloud environment, say), set `ACF_PREPUSH_OPTIONAL=1` in that environment's own settings, never in a committed file. CI is then the only gate.

## Enable the hooks

From the repo root, run once:

```bash
git config core.hooksPath .githooks
```

After that, the hooks in this directory are used for this repo. To stop using them:

```bash
git config --unset core.hooksPath
```
