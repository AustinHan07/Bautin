#!/usr/bin/env bash
# Wire a Hermes profile to a lane in the private vault.
#   bash bautin/scripts/link-vault.sh <vault-path> <profile>
# Links: SOUL.md, memories/, skills/<profile>/ (category), pending/ -> proposed/<profile>/
#        plugins/vault -> bautin/plugins/vault (the vault memory provider)
# Sets:  bautin.vault, memory.provider=vault, skills.create_dir,
#        terminal.docker_volumes (vault at /vault, bautin/ at /bautin:ro)
# Fails loudly if the vault, the profile, or the lane's SOUL.md is missing.
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <vault-path> <profile>" >&2; exit 2; }
VAULT=$(cd "$1" 2>/dev/null && pwd) || { echo "vault not found: $1" >&2; exit 1; }
PROFILE=$2
HH=${HERMES_HOME:-$HOME/.hermes}
PH="$HH/profiles/$PROFILE"
FORK=$(cd "$(dirname "$0")/../.." && pwd)
[ -d "$PH" ] || { echo "profile not found: $PH (run: hermes profile create $PROFILE)" >&2; exit 1; }
[ -f "$VAULT/profiles/$PROFILE/SOUL.md" ] || { echo "missing $VAULT/profiles/$PROFILE/SOUL.md" >&2; exit 1; }
for d in "profiles/$PROFILE/memories" "skills/$PROFILE" "proposed/$PROFILE" "state/$PROFILE" "memory/$PROFILE"; do
  mkdir -p "$VAULT/$d"
done
link() {  # link <target> <linkpath>
  local t=$1 l=$2
  if [ -e "$l" ] && [ ! -L "$l" ]; then
    if [ -f "$l" ]; then mv "$l" "$l.pre-vault"; echo "  kept old $l as $l.pre-vault"
    elif [ -d "$l" ] && [ -z "$(ls -A "$l")" ]; then rmdir "$l"
    else echo "refusing to replace non-empty $l" >&2; exit 1; fi
  fi
  ln -sfn "$t" "$l"; echo "  $l -> $t"
}
echo "linking profile $PROFILE to $VAULT"
link "$VAULT/profiles/$PROFILE/SOUL.md"   "$PH/SOUL.md"
link "$VAULT/profiles/$PROFILE/memories"  "$PH/memories"
link "$VAULT/skills/$PROFILE"             "$PH/skills/$PROFILE"
link "$VAULT/proposed/$PROFILE"           "$PH/pending"
mkdir -p "$PH/plugins"
link "$FORK/bautin/plugins/vault"         "$PH/plugins/vault"
hermes -p "$PROFILE" config set bautin.vault "$VAULT" --force >/dev/null
hermes -p "$PROFILE" config set memory.provider vault >/dev/null
hermes -p "$PROFILE" config set skills.create_dir "$VAULT/skills/$PROFILE" >/dev/null
hermes -p "$PROFILE" config set terminal.docker_volumes "[\"$VAULT:/vault\", \"$FORK/bautin:/bautin:ro\"]" >/dev/null
echo "config: bautin.vault, memory.provider, skills.create_dir, terminal.docker_volumes set for $PROFILE"
