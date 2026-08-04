#!/bin/bash
# Sync this deploy mirror from the capstone team repo's website/ directory,
# commit, and push (the push triggers the Vercel production deploy).
#
# Source of truth: mr-kelsey/su26-aai590-Group2, directory website/.
# This repo (Jungleislander/venue-economics) exists so Vercel has a repo it
# can watch; the team repo cannot be connected without the owner installing
# the Vercel GitHub App.
#
# Run:
#   /Users/Steve3/Projects/hyperfocus/venue-economics/scripts/sync-from-team-repo.sh
set -euo pipefail

TEAM=/Users/Steve3/Projects/personal/capstone/su26-aai590-group2
MIRROR=/Users/Steve3/Projects/hyperfocus/venue-economics

cd "$TEAM"
git fetch origin --prune
if ! git merge-base --is-ancestor origin/main main 2>/dev/null || [ "$(git rev-parse main)" != "$(git rev-parse origin/main)" ]; then
  git fetch origin main:main 2>/dev/null || true
fi
SRC_SHA=$(git rev-parse --short origin/main)

rsync -a --delete \
  --exclude '.git' \
  --exclude 'node_modules' \
  --exclude 'dist' \
  --exclude '.vercel' \
  --exclude '.astro' \
  --exclude '.env' \
  --exclude '.claude' \
  --exclude '.DS_Store' \
  "$TEAM/website/" "$MIRROR/"

cd "$MIRROR"
if git status --porcelain | grep -q .; then
  git add -A
  git commit -m "sync: website/ from su26-aai590-Group2 @ $SRC_SHA

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  git push
  echo "synced from team repo @ $SRC_SHA and pushed (Vercel will deploy)"
else
  echo "already in sync with team repo @ $SRC_SHA"
fi
