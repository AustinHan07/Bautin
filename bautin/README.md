# Bautin

A personal, lane-scoped build of [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT, NousResearch).
Hermes core is unmodified. Everything Bautin adds lives in this `bautin/` directory, so `hermes update` keeps merging upstream cleanly.

## The idea

One generalist agent that sees everything rots its own context and costs money. Bautin runs **lanes** instead: each lane is a Hermes
profile with its own identity, a small tool allowlist, one preloaded skill, its own memory, its own secrets, its own sandbox container,
and its own scheduled jobs. Knowledge the lane can look up lives in a private git vault and grows without limit; what is injected into
every prompt stays small and fixed. Deterministic scripts do everything that can be written as a rule; the model only judges and writes.

## How it plugs into Hermes (no core changes)

| Hermes mechanism | What Bautin uses it for |
|---|---|
| Profiles (`hermes -p <lane>`) | One profile per lane: config, `.env`, SOUL.md, skills, memory, cron, plugins, sandbox |
| Config keys | Tool allowlists, memory caps, prompt caching, compression/pruning, docker sandbox, write-approval gates, cheap auxiliary models |
| Skills (`SKILL.md` + `references/`) | The lane's one auto-loaded procedure; long material read on demand |
| Memory provider ABC | `plugins/vault`: one fact per markdown file, ripgrep search, nothing auto-injected |
| Plugin hooks and tools | `plugins/lane_guards`: approval markers, submission gate, draft checks, Notion push, a host-side mail tool |
| Review-prompt class attributes | `plugins/learning_loop`: a "when in doubt, save nothing" rule appended to the background review prompts |
| `pending/` write approval | Agent-proposed skills and memory edits wait for `/skills approve` or `/memory approve` |
| Cron with a pre-check script | Every tick runs deterministic scripts first; the model runs only when they say `{"wakeAgent": true}` |
| Docker terminal backend | `docker/Dockerfile`: Playwright + Chromium sandbox; the vault is mounted at `/vault`, this folder at `/bautin` |

## Directory map

```
bautin/
  scripts/link-vault.sh          wire a profile to a vault lane (symlinks, plugins, config, cron pre-check wrapper)
  scripts/install-hooks.sh       gitleaks pre-commit for this repo
  docker/                        sandbox image (Playwright, Chromium, ripgrep, PyYAML)
  plugins/vault/                 memory provider
  plugins/lane_guards/           deterministic guards + per-lane rules (rules.py) + mail_verification tool
  plugins/learning_loop/         review-prompt tilt
  scripts/internships/           the internships lane's scripts (see below)
  tests/                         unit tests (host) + a browser test that runs inside the sandbox image
  vault-template/                generic empty vault to copy into a private repo
  config.example.yaml, .env.example
```

### Internships lane scripts

| Script | Runs on | Does |
|---|---|---|
| `discover.py` | host (cron pre-check) | SimplifyJobs list + Greenhouse/Lever/Ashby boards + extra sources → filters → dedupe → queue rows with stable ids; prints the wake decision |
| `mail.py` | host | Gmail over IMAP: Emploive alert parsing into a source file; newest verification link or code from a sender domain |
| `ig_session.py`, `ig_stories.py`, `ig_extract.py` | host / sandbox / host | Instagram session from browser cookies; headless story reader; cheap-vision extraction into a source file and a leads list |
| `notion_sync.py` | host | Pull the tracker before discovery; push Queued rows; push Applied/Skipped after submissions and decisions; refresh the counter |
| `auto_approve.py` | host | Rules-based approval markers when `auto_apply: true` (tier, track, freshness, not in the tracker, daily cap) |
| `submit.py`, `open_link.py` | sandbox | Form filler with dry run, approval gate, captcha hand-off, tracker row; verification-link opener |

The vault holds the lane's editable rules (`state/<lane>/filters.md`, `notion.md`), its working data (queue, seen-list, drafts,
approvals, sources, logs), the identity, the skill, and the reference files the model may quote from. Nothing personal is in this repo.
