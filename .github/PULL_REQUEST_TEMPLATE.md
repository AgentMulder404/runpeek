## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Checks

- [ ] `pytest`, `ruff check src tests examples` and `mypy` pass locally
- [ ] Tests cover the change (offline; no provider credentials)
- [ ] No prompts, tool payloads, commands or file contents can reach the store, exports or output
- [ ] Unknown usage or prices stay unknown (never `$0`)
- [ ] README / docs updated if a supported surface or a claim changed
- [ ] `CHANGELOG.md` updated under *Unreleased*
