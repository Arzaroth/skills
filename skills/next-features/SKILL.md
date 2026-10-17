---
name: next-features
description: Explore the project and propose what features to build next, including ideas not in the roadmap. Use when the user asks "what should we do next", "what features should we build", "propose next features", "what's next", or wants feature ideas / a backlog brainstorm grounded in the actual codebase.
disable-model-invocation: false
---

# Next Features

Explore the project, understand what it is and what already exists, then propose
a ranked set of features to build next. Propose ideas **beyond** what any
roadmap already lists - the roadmap is a floor, not a ceiling.

## Goal

Answer one question well: **"What should we build next, and why?"** - grounded
in what the code actually is, not generic product advice.

## Procedure

1. **Map the project.** Build a real picture before proposing anything:
   - Read the entry index docs first if they exist (`README`, `BRAIN.md` /
     `brain/`, `docs/`, `ARCHITECTURE.md`). Navigate via indexes, don't grep the
     whole tree blindly.
   - Identify: what the product is, who uses it, the core domain, the stack, and
     the main feature surfaces already shipped.
   - Skim existing planning docs (`ROADMAP.md`, `TODO.md`, changelog, issues) so
     you know what's already planned, in-progress, or recently shipped. Don't
     re-propose those.
   - Delegate broad fan-out exploration to subagents (Explore / investigator)
     and keep only the conclusions - don't pull whole files into context.

2. **Find the gaps.** Look for leverage, not noise:
   - Half-finished surfaces, TODO/FIXME clusters, deferred items, stubbed flows.
   - Natural extensions of existing features (a feature that begs for a sibling).
   - Friction in core user journeys (onboarding, empty states, error paths,
     mobile, a11y, perf).
   - Data already collected but not surfaced; integrations the stack makes cheap.
   - Differentiators vs. obvious competitors in the same space.

3. **Propose features.** 5-8 candidates. Mix safe wins with one or two ambitious
   bets. For each, give:
   - **Title** - short, concrete.
   - **What** - one or two sentences, user-facing.
   - **Why now** - the gap/leverage it exploits, tied to something real in the
     code or docs.
   - **Effort** - rough S / M / L, with the main risk or unknown.
   - **Touches** - the files/subsystems it would hit (cite real paths).
   - Mark `[not in roadmap]` on net-new ideas vs `[extends roadmap]` on ones that
     deepen a planned item.

4. **Rank and recommend.** End with a short ranked list (highest
   value-to-effort first) and a single clear recommendation for what to start
   next, with the one-line reason.

## Rules

- Ground every proposal in the actual codebase - cite real files, features, or
  docs. No generic "add AI" / "add analytics" filler that ignores what's there.
- Respect the project's own conventions and constraints (read its CLAUDE.md /
  contributing docs if present).
- Don't just restate the roadmap. The explicit ask is to surface ideas it
  doesn't already list.
- Don't write code or start building. This skill scopes and proposes; the user
  decides what to pick up.
- Be honest about effort and risk. A tempting feature with a nasty unknown gets
  that unknown called out.
