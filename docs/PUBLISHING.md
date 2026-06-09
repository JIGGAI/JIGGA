# Publishing JIGGA to PyPI

Once published, users install the global command with:

```bash
pipx install jigga      # isolated, recommended for a CLI
# or
pip install jigga
```

The wheel bundles the example recipes (under `jigga/examples/`, via the
`force-include` in `pyproject.toml`), so `jigga init --examples` and
`jigga recipes` work from a clean install — verify after any packaging change:

```bash
python -m build
python -m twine check dist/*
# install the wheel in a throwaway venv and confirm the bundled recipes load:
python -m venv /tmp/jv && /tmp/jv/bin/pip install dist/*.whl
/tmp/jv/bin/jigga --home /tmp/jh init --examples && /tmp/jv/bin/jigga --home /tmp/jh recipes list
```

## Cutting a release (automated — recommended)

1. Bump `version` in `pyproject.toml`.
2. Commit, merge to `main`.
3. Create a GitHub Release with a tag matching the version (e.g. `v0.1.0`).

The **`.github/workflows/publish.yml`** workflow then builds the sdist + wheel
and publishes to PyPI via **Trusted Publishing (OIDC)** — no API token is stored
in GitHub.

### One-time PyPI setup (Trusted Publishing)

On PyPI → the `jigga` project → **Publishing** → add a GitHub trusted publisher:

- Owner: `JIGGAI` · Repository: `JIGGA` · Workflow: `publish.yml` · Environment: `pypi`

For the **very first** upload (before the project exists on PyPI), add it as a
**pending publisher** at <https://pypi.org/manage/account/publishing/> with the
same values; the first workflow run creates the project.

Also create a GitHub environment named `pypi` (Settings → Environments) — you
can add required reviewers there to gate releases.

## Manual publish (fallback, uses a token)

```bash
python -m build
python -m twine upload dist/*          # prompts for a PyPI API token
```

Create the token at <https://pypi.org/manage/account/token/> (scope it to the
`jigga` project after the first upload). To use a token in CI instead of OIDC,
drop the `permissions:`/`environment:` blocks in the workflow and pass
`password: ${{ secrets.PYPI_API_TOKEN }}` to the publish step.
