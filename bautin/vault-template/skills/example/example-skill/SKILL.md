---
name: example-skill
description: Template for a lane skill; replace with the one procedure this lane almost always runs
metadata:
  hermes:
    category: example
    tags: [template]
  bautin:
    lane: example
    date: "2026-09-18"
    source: hand-written
    type: skill
---
# Example skill

Keep the always-loaded body short: targets, the stages in order, the approval rule, and the silence rule. Put long procedures, rubrics, and personal facts in `references/` and read them with the skill tool only when a stage needs them.

1. Discover with a script, never by browsing.
2. Judge with the model, recording a score and a one-line reason.
3. Report one digest. If nothing changed, reply exactly `[SILENT]`.
4. Act only after the user's explicit approval, and only for the items named.
5. Track every outcome in `/vault/state/<lane>/`.
