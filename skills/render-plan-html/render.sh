#!/usr/bin/env bash
# Render a Claude plan markdown file as styled standalone HTML and open it.
# Usage: render.sh [PLAN.md] ["optional subtitle"] ["optional eyebrow"]
#   - PLAN.md defaults to the most recently modified file in ~/.claude/plans/
#   - subtitle renders as the lede under the title; eyebrow is the mono kicker above it
#   - styling lives in style.html next to this script (edit there; renders stay consistent)
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="${1:-}"
if [ -z "$src" ]; then
  src="$(ls -t "$HOME"/.claude/plans/*.md 2>/dev/null | head -1 || true)"
fi
if [ -z "$src" ] || [ ! -f "$src" ]; then
  echo "render-plan-html: no plan file found (pass a path, or put one in ~/.claude/plans/)" >&2
  exit 1
fi
command -v pandoc >/dev/null || { echo "render-plan-html: pandoc not installed" >&2; exit 1; }

title="$(grep -m1 '^# ' "$src" | sed 's/^# //' || true)"
[ -n "$title" ] || title="$(basename "$src" .md)"
sub="${2:-}"
name="$(basename "$src" .md)"
eyebrow="${3:-Plan · $name}"
out="/tmp/$name.html"

# Strip the first H1 (it becomes the hero title via metadata) to avoid a duplicate heading.
sed '0,/^# /{/^# /d;}' "$src" \
  | pandoc -f gfm+attributes+bracketed_spans+fenced_divs+definition_lists \
      -s --toc --toc-depth=2 -H "$dir/style.html" \
      --metadata title="$title" \
      ${sub:+--metadata subtitle="$sub"} \
      --metadata date="$eyebrow" \
      -o "$out"

echo "wrote $out"

opener="$(command -v xdg-open || command -v wslview || command -v open || true)"
if [ -n "$opener" ]; then
  "$opener" "$out" >/dev/null 2>&1 &
  disown 2>/dev/null || true
  echo "opened in browser"
else
  echo "no opener found - open manually: $out"
fi
