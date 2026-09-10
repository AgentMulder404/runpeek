# Release checklist — first public release

Everything in *Prepared locally* is done in this checkout. Everything below it
is a separate action that needs the owner's explicit go-ahead.

## Prepared locally (done)

- [x] Apache-2.0 `LICENSE` (unmodified text) and `NOTICE` (`Copyright 2026 NemulAI`,
      matching the existing public NemulAI repository).
- [x] `pyproject.toml`: license metadata, classifiers, `project.urls` pointing at
      the intended repository; sdist/wheel build clean.
- [x] README with two quickstarts, offline demo, integration matrix, measurement
      limits, privacy, migration, development.
- [x] CI workflow: tests, lint, types, build, clean-venv wheel smoke; `permissions:
      contents: read`; no publish job.
- [x] Tracked files scanned: no credentials, transcripts, stores, exports or
      machine-specific paths (the only pattern hit is the fake tokens in
      `tests/test_cli.py`'s redaction test).
- [x] Local `origin` remote set to `https://github.com/AgentMulder404/runpeek.git`
      (nothing pushed; the repository does not exist yet).

## 1. Create the repository (owner decision)

Owner: the `gh` CLI on this machine is authenticated as **AgentMulder404**;
that account is the intended owner. Repository name `runpeek` was free on
2026-09-09.

```bash
cd /Users/rizz/nemulai-harness
gh repo create AgentMulder404/runpeek --public --source . --remote origin --push
```

(`--push` publishes `main`; drop it to create an empty repository first.)

## 2. Enable private vulnerability reporting (after the repo exists)

GitHub → Settings → Code security → *Private vulnerability reporting* → Enable.
Then edit `SECURITY.md`: replace the "not yet set up" paragraph with the
reporting link `https://github.com/AgentMulder404/runpeek/security/advisories/new`
and commit. Do not do this before the setting is verified on.

## 3. Verify public links

After the first push, open the README on GitHub and click every link in it,
`SECURITY.md`, `CONTRIBUTING.md` and `docs/`. All local links were checked by
script before release; the repository URL in `pyproject.toml` and the clone
line in the README become live only once step 1 is done.

## 4. CI green on the hosted repository

The `ci` workflow runs on push and pull requests with read-only permissions.
Confirm both jobs (`test` on 3.10 and 3.12, `build`) pass on GitHub before
tagging anything. Add a status badge only after it has passed at least once.

## 5. Package metadata and artifact review

```bash
python -m build
tar tzf dist/runpeek-*.tar.gz | less        # no stores, transcripts, exports or venvs
unzip -l dist/runpeek-*.whl                  # 32 files: package, rates, schema, boot, LICENSE/NOTICE
```

First public version: `0.1.0a1` (a pre-release; `pip install` needs `--pre`).

## 6. Publishing the package (separate decision; optional trusted publishing)

Nothing is uploaded by this repository's workflows. If and when publication is
approved: reserve the name by uploading to TestPyPI first, then configure
PyPI *trusted publishing* for `AgentMulder404/runpeek` with a dedicated
release workflow (`permissions: id-token: write`, triggered by a tag) — added
only after the repository exists and CI has passed. Until then the README
must keep saying "not on PyPI yet".

## Not part of this release

- Real-provider smoke test (`examples/real_openai.py`) — unrun by maintainers;
  instructions in the README.
- Trademark clearance for "RunPeek" — not checked.
