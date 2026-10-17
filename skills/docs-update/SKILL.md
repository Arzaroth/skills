---
name: docs-update
description: "Check whether the repo's own docs still describe the code after recent changes, and update the ones that went stale. Scopes itself to the last merged branch, the last commit, this session's work, or a full sweep. Use when asked to 'update the docs', 'check the docs', 'are the docs still accurate', 'docs sweep', 'refresh the documentation', or '/docs-update'. Every line it writes goes through the unslop skill. Run it before cutting a release or merging a feature, so docs ship with the change instead of trailing it."
---

# docs-update

Docs rot silently: nothing fails, the text just stops being true. This skill
finds the docs the recent changes should have touched and brings them in line.

The commands below spell out `~/.claude/skills/docs-update/scripts/` in full. **Run
them exactly as written - keep the leading `~` and do not expand it.** A
permission rule matches the literal command string, so `/home/<you>/.claude/...`
(the tilde expanded), `python3 ~/...`, a relative path, and a `cd` first are each
a *different* string, and each one re-prompts. A tilde path is not "the absolute
path written informally" - to a permission rule it **is** the path.

**Docs ship with the change, not after it.** If you are on a feature branch,
commit the docs onto that branch so the merge carries them. A docs follow-up PR
is a docs PR nobody writes.

## 1. Pick the scope

In precedence order:

**The user said so.** "full sweep", "since v2.1", "just this branch" - use it.
A full sweep is `--full` and reads everything; it is the right choice after a
long gap or when adopting this skill on a repo for the first time.

**A doc records its own last-updated point:**

```bash
~/.claude/skills/docs-update/scripts/docscheck.py markers
```

If a doc carries `Last updated: <date|sha>`, scope *that doc* from there
(`--since`). Docs that disagree get their own scopes; there is no single answer.

**Otherwise, derive it from git:**

```bash
~/.claude/skills/docs-update/scripts/docscheck.py scope
```

It prefers the **last merged branch** (a branch's worth of work is the unit
people mean), falling back to the **last commit**. Override with `--since <ref>`.

**Work done in this session** is the case the script cannot see. If this session
made the changes, you already know the files; pass the pre-session ref as
`--since`, or work from your own edit list.

`scope` separates `kind=code` from `kind=doc-already-touched`. The second list
matters: those docs were updated alongside the change, so check them for
correctness rather than absence.

## 2. Find the docs the change actually reaches

```bash
~/.claude/skills/docs-update/scripts/docscheck.py impacted
```

For each changed code file, this reports the docs that **name that exact path**.
A doc citing a file you changed is the doc most likely to be describing something
that is no longer true, and it is a far better starting point than rereading
every doc.

It matches full paths only, deliberately. Matching parent directories as well
sounds more generous and is useless: on a monorepo `apps/web-nuxt/` appears in 55
of 62 docs, so everything impacts everything.

`result=none` is normal and does not mean "nothing to do" - it means no doc names
these paths, so judge by subject instead. Which is step 3.

## 3. Judge by subject, using the repo's own doc taxonomy

`impacted` finds path citations. It cannot find a doc that describes a behaviour
you changed without naming a file. So read the change and ask what it means.

First learn how this repo organises docs - a `brain/`, a `docs/`, a CONTEXT.md,
an ADR directory - and follow **that** structure rather than inventing one. If a
project skill covers docs (`.claude/skills/*`), it wins over this one.

The usual mapping:

| The change | The doc |
| --- | --- |
| new user-visible feature | feature doc + its index/catalog row; README feature list |
| changed existing behaviour | that feature's doc |
| schema, auth, build, storage, realtime, providers | the architecture doc for it |
| a non-obvious decision, or one surfaced in review | the decisions/ADR log, **with its why** |
| new term | the glossary |
| new or changed env var, command, script | README and any setup/ops doc |
| new dependency | any stack/tech-list doc (these are hand-maintained and always forgotten - diff the manifest) |
| user-visible change | CHANGELOG `[Unreleased]` |

Two traps:

- **Derived facts embedded in prose.** A doc saying "current version is 2.16.2"
  or listing the tech stack is a copy of something else that moved. Diff the
  source of truth (the manifest, the version file) rather than trusting the prose.
- **Docs that are meant to be historical or aspirational.** CHANGELOG entries,
  TODO and ROADMAP cite files that were deleted or never written. Do not "fix"
  them.

## 4. Check the citations still resolve

```bash
~/.claude/skills/docs-update/scripts/docscheck.py links
```

Two kinds:

- `kind=link` - a relative markdown link whose target is gone. Always a real
  problem.
- `kind=source` - an inline-code path that no longer exists, usually because the
  file moved or was renamed. Narrative docs are excluded, and brace expansions,
  `<placeholders>`, globs and gitignored build output are filtered out, so what
  survives is worth looking at.

Fix what is in scope. Findings elsewhere in the repo are worth *reporting* but do
not silently widen the job into a full sweep.

## 5. Write

### Unslop everything you write

**Apply the `unslop` skill to every line you add or change.** Documentation is
where slop does the most damage: it is read by people trying to learn something,
it is the first thing a newcomer meets, and unlike a chat message it stays.

The tells that show up specifically in docs:

- **Throat-clearing openers.** "This document provides an overview of...",
  "In this section we will explore...". Delete them and start with the fact.
- **Restating the code in prose.** "The `getUser` function gets a user." If the
  sentence is the identifier expanded into English, it is worth nothing. Write
  the part the code cannot say: the constraint, the why, the gotcha.
- **Empty intensifiers**: comprehensive, robust, seamless, powerful, flexible,
  simply, easily, just. "Simply run X" is false whenever X is not simple.
- **Hedging where the answer is definite**: "generally", "typically", "should
  normally". Either it does or it depends on something nameable - name it.
- **Note-that padding**: "It's important to note that", "Please note", "Keep in
  mind". The sentence after it is the content; ship that.
- **Summary sections that summarise the page above them.** If the doc needs a
  recap, it is too long.
- **Description columns that repeat the name.** A table row
  `| --verbose | Enables verbose output |` costs a line and teaches nothing.
- **Rule-of-three filler** - a third example added for rhythm rather than because
  it differs from the first two.

### Match the file, not the slop

Match the file's existing voice: its tense, its density, whether entries are
one-liners or prose. A doc where one section suddenly reads differently is worse
than one that is uniformly terse.

These two rules only collide when the existing doc is itself sloppy. Then: match
its **structure and density**, not its padding, and **do not rewrite sections you
did not otherwise touch.** A docs update that also restyles the whole file is an
unreviewable diff, and it buries the change that actually mattered. If a doc
needs a prose pass throughout, say so and offer it as its own job.

### The rest

- Link source paths rather than restating code, so the next change breaks the
  link instead of quietly contradicting the prose.
- Describe what is true now, in the present tense. Docs are not a changelog: "we
  replaced X with Y" belongs in the CHANGELOG, "the system uses Y" in the doc.
- If a doc has a last-updated marker, move it.

## 6. Report

- the scope used, and why that one
- docs updated, and what changed in each
- docs checked and deliberately left alone
- broken links or citations found outside the scope, listed but not fixed
- anything you could not judge without the user
