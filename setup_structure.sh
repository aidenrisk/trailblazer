#!/usr/bin/env bash

set -e

PKG="src/trailblazer"

AGENTS="scraper frontier form_filler replay_gen validator"

mkdir -p \
  "$PKG/agents" \
  "$PKG/contracts" \
  "$PKG/shared" \
  "$PKG/observability" \
  tests

touch \
  "$PKG/__init__.py" \
  "$PKG/agents/__init__.py" \
  "$PKG/contracts/__init__.py" \
  "$PKG/shared/__init__.py" \
  "$PKG/observability/__init__.py"

for agent in $AGENTS; do
  mkdir -p "$PKG/agents/$agent"
  touch \
    "$PKG/agents/$agent/__init__.py" \
    "$PKG/agents/$agent/$agent.py"
done

echo "Repository stub created successfully."
