# Git hooks

These hooks run when you use this repo (e.g. on push).

## Version check for release tags

The **pre-push** hook checks that when you push a tag matching `v*` (e.g. `v2.0.6`), the commit that tag points to has the same version in both:

- `pyproject.toml` — `version = "X.Y.Z"`
- `installer.iss` — `#define AppVersion "X.Y.Z"`

If they don’t match the tag, the push is aborted so the CI build doesn’t fail.

## Enable the hooks

From the repo root, run once:

```bash
git config core.hooksPath .githooks
```

After that, the hooks in this directory are used for this repo. To stop using them:

```bash
git config --unset core.hooksPath
```
