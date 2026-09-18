# vault-template

A generic, empty vault for Bautin. Copy it to a **private** repo, open it in Obsidian, and wire a Hermes profile to a lane with `bash bautin/scripts/link-vault.sh <vault-path> <profile>`.

| Folder | Written by | Injected every turn? |
|---|---|---|
| `profiles/<lane>/SOUL.md` | you (agent may propose) | yes, keep it under ~800 characters |
| `profiles/<lane>/memories/` | Hermes built-in memory (capped) | yes |
| `skills/<lane>/<skill>/` | you, or the agent after approval | no, loaded on demand; one `auto_load` skill per lane |
| `proposed/<lane>/` | Hermes pending writes awaiting `/skills approve` or `/memory approve` | no |
| `memory/<lane>/` | the vault memory provider, one fact per file | no, retrieved with `vault_memory_search` |
| `sessions/` | nightly job summaries | never searched by default |
| `log/` | cron and guard logs, append-only | no |
| `state/<lane>/` | working data for that lane's scripts | no |

Every markdown file except `SOUL.md` starts with the same frontmatter: `lane`, `date`, `source`, `type`.
