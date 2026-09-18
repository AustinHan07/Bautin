#!/usr/bin/env bash
# Build the Bautin sandbox image and point a profile's terminal backend at it.
#   bash bautin/docker/build.sh [profile]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
docker build -t bautin-sandbox:latest "$HERE"
if [ $# -ge 1 ]; then
  hermes -p "$1" config set terminal.docker_image bautin-sandbox:latest >/dev/null
  echo "terminal.docker_image=bautin-sandbox:latest set for profile $1 (existing sandbox container is replaced on next use)"
fi
