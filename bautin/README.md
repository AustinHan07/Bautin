# Bautin

A personal, lane-scoped build of [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT, NousResearch).
Everything Bautin adds lives in this `bautin/` directory so upstream merges stay clean; Hermes core is unmodified.

| Path | What |
|---|---|
| `bautin/plugins/` | Small Hermes plugins (vault memory provider, lane guards, request trimmer, output verifier) |
| `bautin/scripts/` | Deterministic jobs run by cron or registered as tools (discovery, submit, nightly merge, spend alert) |
| `bautin/vault-template/` | Generic example vault: profile stubs, one sample skill, frontmatter schema |
| `bautin/config.example.yaml` | Annotated per-profile config (no secrets) |
| `bautin/.env.example` | Names of the env vars a deployment needs (no values) |

Personal content (real profiles, skills, memories, sessions, logs) never lands here; it lives in a private vault repo.
Secrets never land here; a gitleaks pre-commit hook enforces it (`bash bautin/scripts/install-hooks.sh`).
