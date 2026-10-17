---
name: merge-stack
description: "Merge a stack of dependent PRs bottom-up, one at a time, each through babysit-pr at one round per PR, triaging every review finding against the stack tip and handing the still-open ones back at the end. Flips the project's squash setting for the run and restores it even if the run dies. Use when asked to 'merge the stack', 'land the stacked PRs', 'merge all 13 MRs in order', or '/merge-stack'."
---

# Merge a stack

Drive a chain of dependent PRs onto the default branch, bottom-up, one at a time.
Each PR is merged by `babysit-pr`; this skill owns the ordering, the project
setting the chain needs, and putting that setting back.

The commands below spell out `~/.claude/skills/merge-stack/scripts/` in full. **Run
them exactly as written - keep the leading `~` and do not expand it.** A
permission rule matches the literal command string, so `/home/<you>/.claude/...`
(the tilde expanded), `python3 ~/...`, a relative path, and a `cd` first are each
a *different* string, and each one re-prompts. A tilde path is not "the absolute
path written informally" - to a permission rule it **is** the path.

This skill also calls `~/.claude/skills/babysit-pr/scripts/prctl.py`; the same
rule applies to it.

Read `babysit-pr`'s `references/forges.md` before touching a forge and
`references/ai-reviewers.md` before reading any bot's output. Everything this
skill does to an individual PR is that skill's job, not this one's.

## Parameters

| Name | Default | Meaning |
| --- | --- | --- |
| `stack` | inferred from the base chain | the PRs, bottom-up |
| `squash` | **ask** | `off` flips the project for the run; `keep` leaves it alone |
| `merge` | **auto** | `never` babysits to mergeable and stops without merging |
| `creds` | auto-detected | credential names to rotate across review triggers |
| `stop_on` | `blocker` | `blocker` stops the run; `never` continues to the next PR |

**`rounds` is not a parameter here. It is always 1.** `babysit-pr` takes a round
budget, and in a stack the right budget is one pass per PR, every time. Two
reasons, and neither is about saving effort:

- A second round on PR 1 holds up the twelve PRs behind it, and the reviewer that
  produced the finding has not seen the rest of the stack.
- Most findings on a lower branch are **already fixed further up**. Working them
  in place rewrites shared history mid-merge and creates conflicts on every
  branch above. The triage below is what replaces the extra rounds.

If a PR genuinely needs a second pass, that is a stop, reported as one - not a
budget you quietly raise.

Parameters come from the user's words or these defaults, and nowhere else. State
what you resolved and where each came from before starting. `babysit-pr`'s rule
against inventing a parameter and attributing it to the user applies here too.

## 0. Restore anything a previous run left behind

Always first, before reading the stack:

```bash
~/.claude/skills/merge-stack/scripts/squashctl.py pending
```

A non-empty `pending` means an earlier run changed a project setting and died.
Restore it and say so:

```bash
~/.claude/skills/merge-stack/scripts/squashctl.py restore
```

Do not start a new run on top of someone else's unrestored state.

## 1. Read the stack, do not assume it

Resolve the chain from the forge, bottom-up: the PR whose base is the default
branch is first, then whichever PR bases on that one, and so on.

**Count by source branch, not by target.** A previous run on this repo reported
"~30 missing MRs" because it counted targets; recounting by source gave 66. A PR
list keyed on the wrong end of the edge is wrong in a way that looks plausible.

Print the resolved order and the file count per PR before doing anything. Stop
and hand back if the chain has a gap, a fork, or a PR whose base is neither the
default branch nor another PR in the stack.

## 2. Decide the squash setting, out loud

With squash on, every merge puts a **new** commit on the default branch, so each
child's merge-base is no longer its parent's real commits. The child still merges
- the forge rebases it server-side and drops the already-applied patches, and one
child on this repo went 270 files to 46 that way - so squash-on is a cost, not a
blocker.

With squash off, the parent's commits stay ancestors, every merge-base is exact,
and each child's diff is only its own work.

That is the whole trade. It is the user's call, because turning squash off puts
**every commit in the stack** on the shared default branch permanently, which is
a change to what that branch's history looks like for everyone.

Ask once, naming the number:

> The stack is N commits across M PRs. Squash is currently `<value>`. Turning it
> off for this run puts all N commits on `<branch>`; leaving it on keeps one
> commit per PR and costs a server-side rebase each. Which?

Then, only if they chose `off`:

```bash
~/.claude/skills/merge-stack/scripts/squashctl.py show --repo <slug>
~/.claude/skills/merge-stack/scripts/squashctl.py set --repo <slug> --value never
```

`set` records the previous value before it changes anything, so a crash is
recoverable.

**Never change `merge_method`.** It rewrites how every unrelated MR on the
project lands, and a previous attempt was blocked for that reason. If the chain
seems to need it, that is a finding for the user, not a setting to flip.

## 3. Merge one PR, bottom-up

For each PR in order, invoke `babysit-pr` with `pr`, `rounds: 1`, the resolved
`merge`, and any `creds`. It owns the reviewing, the fixing and the merge.

Two things belong to this skill, both **after** the parent merges:

**Verify the child's diff survived the retarget.** When a parent merges, the
forge retargets the child and may delete the parent branch at the same moment.
On GitLab that race leaves the child's cached diff refs pointing at a branch that
no longer exists: the PR reads "contains no changes" while git is perfectly fine.

```bash
~/.claude/skills/babysit-pr/scripts/prctl.py --repo <slug> state <child>
```

A `changes` of 0 on a PR you know has commits is that race, not an empty PR.
`prctl.py refresh-diff <child>` repairs it. Check this every time; do not hand a
PR to `babysit-pr` before its diff is real, or the reviewer will be asked to
review nothing and the wait will look like progress.

**Expect no automatic review while the base is not the default branch.** Bots
commonly skip those, and the retarget that fixes it is not a push event, so
nothing fires on its own. `babysit-pr` triggers the reviewer explicitly; that is
why each PR is babysat *after* its parent lands and not before.

## 3b. Triage every finding against the tip, not against the PR

This is the part that makes one round per PR enough, and it is the part a stack
needs that a single PR does not.

A finding raised on branch 3 is a finding about a tree that thirteen branches of
work have already moved on from. Before touching anything, check what the finding
looks like **at the stack tip**. Five outcomes, and only the first two cost a
commit:

1. **Still open at the tip, and dangerous while it waits.** Fix it here. Say in
   the commit message why it could not wait - what is live on master meanwhile
   and how many branches until something else would have fixed it.
2. **Still open at the tip, small and self-contained.** Fix it here.
3. **Fixed by a later branch in this stack.** Leave it. Name the branch that
   fixes it and how wide the window is. A short window on a path nothing reaches
   is not worth rewriting shared history for.
4. **Already fixed at the tip.** Skip it, and say so, so nobody re-reviews it.
5. **Invalid.** Reject it with evidence, not with an opinion. If the claim is
   about runtime behaviour, run it. If it is about a test, measure it: one of
   these on the psr7 stack was rejected after trying the suggestion and watching
   the suite go from 7 failures to 125.

Log every finding under all five outcomes as you go, in a `deferred-findings.md`
in the scratchpad, grouped by PR:

```markdown
## From !42 (feat/example-branch) - 8 findings, 1 fixed

FIXED here: <what, why it could not wait>

Short-window, left alone (fixed by the next branches):
- <finding> ... gains the attribute at branch #8.

Fixed at tip, skipped: <list>

STILL OPEN at tip:
- **<finding>** ... <why it is not a merge-round fix>
```

Write it as you merge, not from memory afterwards. It is the deliverable at the
end of the run, and the "STILL OPEN at tip" entries are the only part the user
has to act on.

## 4. When a PR will not land

Stop the run at that PR, by default. Then, in this order:

1. Restore the setting: `~/.claude/skills/merge-stack/scripts/squashctl.py restore`.
2. Report which PRs merged, which is stuck, and what it needs.

Restore even when the user will resume in an hour. A project-wide setting left
flipped overnight affects every unrelated MR, and step 0 costs nothing on resume.

## 5. Finish

When the last PR merges:

```bash
~/.claude/skills/merge-stack/scripts/squashctl.py restore
~/.claude/skills/merge-stack/scripts/squashctl.py pending   # must print an empty pending
```

## 6. Report

- the order merged, with each merge commit
- the setting: what it was, what it became, and that it is back, shown by
  `pending` being empty rather than asserted
- per PR: findings fixed, rejected, deferred
- **the deferred findings**, as the report's own section, not a file path: every
  "STILL OPEN at tip" entry, with what it would take to close it. This is what
  the user gets out of the run besides a merged stack
- **any PR merged without a review that covered its final head**, named
- what is left: PRs not merged and why, and anything that now needs restacking
- confirmation that nothing is still running

If the run stopped short, the first line says so. A stack that is half-merged
with the setting restored is a good outcome to report plainly; a stack that is
half-merged with the setting still flipped is an incident, and reads as one.
