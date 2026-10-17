---
name: render-plan-html
description: Render a Claude plan (markdown in ~/.claude/plans/) as styled, standalone HTML and open it in the browser. Use when the user asks to view, render, preview, "make it pretty", or display a plan - and by default after writing or finalizing a plan.
---

# Render plan as HTML

Render a plan markdown file as a styled, standalone HTML page and open it in the browser.

## When to use

- The user asks to view / render / preview / display / "make it pretty" a plan.
- By default right after finalizing a plan written to `~/.claude/plans/` (render it, then tell the user the path).

## How

Run the bundled script. With no argument it picks the most recently modified plan in `~/.claude/plans/`:

```
bash ~/.claude/skills/render-plan-html/render.sh [PLAN.md] ["optional subtitle"] ["optional eyebrow"]
```

What it does:
- Produces `/tmp/<plan-name>.html` via `pandoc` (GFM input, standalone).
- Editorial dark sheet modelled on Claude artifacts: mono eyebrow kicker, serif display title, serif lede,
  "On this page" panel, hairline-ruled sections, mono uppercase table headers, accent-barred code and callouts.
- Title comes from the plan's first `# H1`; subtitle is the lede under it; eyebrow defaults to `Plan · <filename>`.
- Follows the OS light/dark preference, and has print styles.
- Opens it with `xdg-open` / `open` / `wslview`. Requires `pandoc`.

## Authoring constructs

`render.sh` enables `gfm+attributes+bracketed_spans+fenced_divs+definition_lists`, so plans can use these
on top of plain GFM. They degrade to readable plain text in any other markdown viewer.

**Badges** - inline status chips, usable anywhere including table cells:

```
[done]{.ok}  [partial]{.warn}  [missing]{.bad}  [superseded]{.info}  [n/a]{.mute}
```

**Callout boxes** - a bold-only first paragraph becomes the box title:

```
::: warn
**The pilot was never rolled out**

The plan called for a pilot on one controller, then a full run.
:::
```

`.note` violet, `.warn` amber, `.bad` terracotta, `.ok` sage. A plain `>` blockquote still works (amber).

**Stat grid** - one definition-list entry per stat: term = number, first definition = label, second = note.

```
::: stats
[**53**]{.bad}/232
: Routes documented
: 4 of 98 controllers were ever annotated

[**0**]{.ok}
: Auth attributes removed
: CheckLca / roles / ownership all intact
:::
```

Colour the number with `.bad` / `.warn` / `.ok` / `.info`; the default is terracotta.
The grid flows in columns and scrolls sideways, so keep it to about six stats.

**Wide tables** - wrap a table in `::: wide` to let it break the text measure on large screens.

## Customizing the look

The house CSS is `style.html` next to the script. Edit it there so every render stays consistent - do not paste inline CSS per render.
