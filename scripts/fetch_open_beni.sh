#!/usr/bin/env bash
# Fetch the robot model and policy from Mondo-Robotics/open-beni (MIT) at a pinned commit.
set -euo pipefail

REPO=https://github.com/Mondo-Robotics/open-beni.git
COMMIT=4545b86a131d05519050df11a424368ef9f06b4f
DEST="$(cd "$(dirname "$0")/.." && pwd)/third_party/open-beni"

if [ -f "$DEST/.commit" ] && [ "$(cat "$DEST/.commit")" = "$COMMIT" ]; then
  echo "open-beni $COMMIT already present at $DEST"
  exit 0
fi

rm -rf "$DEST"
mkdir -p "$DEST"
git -C "$DEST" init -q
git -C "$DEST" remote add origin "$REPO"
git -C "$DEST" sparse-checkout set --no-cone /LICENSE /assets/beni_base/ /assets/models/policy.onnx
git -C "$DEST" fetch -q --depth 1 origin "$COMMIT"
git -C "$DEST" checkout -q FETCH_HEAD
rm -rf "$DEST/.git"
echo "$COMMIT" > "$DEST/.commit"
echo "Fetched open-beni $COMMIT into $DEST"
