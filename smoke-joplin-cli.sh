#!/bin/sh
# Smoke test for the baked joplin CLI, run by the Dockerfile right after
# `npm install`, so it exercises THIS image's node and THIS install tree.
#
# Why it exists: the CI `test` job runs only the Python suites and `buildkit`
# only builds, so an override that breaks the CLI merged on green twice
# (linkify-it 6, !93; nanoid 6 vs node 20, !97). The owner-share-sync CronJob is
# the only real user of the CLI and fails silently. This round-trips one note
# through a filesystem sync target (sync.target 2) between two fresh profiles:
# start-up, the note model, uuid/nanoid, markdown-it and the sync engine all
# have to work for the final grep to match.
#
# Usage: smoke-joplin-cli.sh [joplin-binary]   (default: joplin on PATH)
set -eu

J=${1:-joplin}
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT INT TERM
SYNC="$T/sync"
mkdir -p "$SYNC"

j() { profile=$1; shift; "$J" --profile "$T/$profile" "$@"; }

j a version
j a mkbook smoke-book
j a mknote smoke-note-1f3a
for p in a b; do
    j "$p" config sync.target 2
    j "$p" config sync.2.path "$SYNC"
done
j a sync
j b sync
j b use smoke-book

if j b ls | grep -qx 'smoke-note-1f3a'; then
    echo "joplin CLI smoke: OK (note round-tripped through a filesystem sync target)"
else
    echo "joplin CLI smoke: FAILED, the note did not arrive in the second profile" >&2
    j b ls >&2 || true
    exit 1
fi
