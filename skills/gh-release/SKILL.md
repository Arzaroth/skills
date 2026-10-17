---
name: gh-release
description: "Cut a release on GitHub: update the changelog, bump the version (preferring the repo's own release task or script over a manual edit), tag, push, then watch the release workflow until the GitHub Release exists. Use when asked to 'cut a release', 'release version X', 'ship a new version', 'bump the version and tag', 'publish a release', or '/gh-release'. For repos where a tag push triggers a GitHub Actions workflow that creates the release."
---

# gh-release

Bump, changelog, tag, push, watch.

The commands below spell out `~/.claude/skills/gh-release/scripts/` in full. **Run
them exactly as written - keep the leading `~` and do not expand it.** A
permission rule matches the literal command string, so `/home/<you>/.claude/...`
(the tilde expanded), `python3 ~/...`, a relative path, and a `cd` first are each
a *different* string, and each one re-prompts. A tilde path is not "the absolute
path written informally" - to a permission rule it **is** the path.

Two things make releases go wrong, and both happen before the tag exists:

1. **Reinventing a bump the repo already automates.** A hand-rolled edit touches
   files the repo's own task would have handled, misses the ones it knows about,
   and skips its gate.
2. **A tag that triggers nothing.** It looks exactly like a slow CI queue, and
   you find out twenty minutes later.

Steps 1 and 3 exist to catch those.

## 0. Decide the version

Read the tags (`git tag --sort=-v:refname | head -5`) to learn the repo's format
(`v1.2.3` vs `1.2.3`) and the current version. If the user named a version, use
it. Otherwise propose one from the commits since the last tag - breaking change
is major, `feat:` is minor, otherwise patch - and say which and why.

Refuse to proceed on a dirty tree, a detached HEAD, or a branch behind its
remote. Releasing uncommitted work produces a tag nobody can reproduce.

## 1. Use the repo's own release machinery

**Always look before doing anything by hand:**

```bash
~/.claude/skills/gh-release/scripts/versiontool.py discover
```

It reports project skills, mise tasks, npm scripts, Make targets, just recipes,
`scripts/`-`bin/` entries, and tool configs (`cargo-release`, `bumpversion`,
`release-please`, `git-cliff`). Precedence:

**`kind=project-skill`** - stop reading this skill and follow that one. It is the
repo's own procedure and it knows things this generic skill cannot.

**A task or script** (`kind=mise-task|npm-script|make-target|just-recipe|script`)
- **read it before running it**, then run it. You need to know what it covers so
you do not duplicate or skip work. Typical tasks do some but not all of: bump,
changelog promotion, gate, commit, tag, push. Note which, then do only the rest.

Many take the bare version and add the prefix themselves (`mise run release
0.13.0` tagging `v0.13.0`), so do not pass a tag where a version is wanted. If it
has `--dry-run`, use it first and show the user the plan.

**A tool config with no task** - use that tool's own command
(`cargo release <version>`, `bump-my-version bump ...`), not a manual edit.

**`result=none`** - only then fall back to step 2.

## 2. Fallback: find every place the version lives

Only when step 1 found nothing.

```bash
~/.claude/skills/gh-release/scripts/versiontool.py locations <current-version>
```

It searches for **the literal version string, not the word "version"**. Measured
on a real repo: a `version`-key search found 5 files, the literal string found 8.
The misses spelled the key `"version-name"` (GNOME metadata) and `"Version"`
(Plasma). Anything you miss ships a binary reporting the wrong version.

Output is classified: `edit` (change it), `regenerate` (lockfile - run the hint's
command, never hand-edit, or `--locked` fails in CI), `changelog` (step 4).

Then check the **previous** version too - a hit there is a file missed last
release and stale since. Fix it in this one.

## 3. Preflight the tag

```bash
~/.claude/skills/gh-release/scripts/relwatch.py preflight <tag>
```

It reads `.github/workflows/*.yml`, extracts every `on.push.tags` pattern, and
tells you whether your tag matches one and whether that workflow looks like it
creates a release. `result=NO_MATCH` means pushing the tag does nothing at all.

Run this **before** committing, while changing the tag is free.

Also confirm by eye that the workflow's release step uses the tag you are about
to push. Many workflows do not verify that the tag matches the version in the
code, so a mismatch produces a release whose binaries report the wrong version,
silently and with green CI.

## 4. Changelog

If step 1's task promotes the changelog itself, let it, and skip to step 5.

**Keep a Changelog** (there is an `## [Unreleased]` heading): promote it.

- `## [Unreleased]` becomes `## [X.Y.Z] - YYYY-MM-DD`
- add a fresh empty `## [Unreleased]` above it
- update the link definitions at the bottom, including the `[Unreleased]`
  compare link's new base

**No changelog, or no Unreleased section**: write the entry from the commits
since the last tag, grouped the way the file already groups them. Match the
existing voice exactly - if entries are one-liners, write one-liners; if they
are prose with bolded leads, write that.

Either way: describe what changed for a user of the project, not what you did to
the repo.

**Apply the `unslop` skill to every entry.** A changelog is read for years, by
people deciding whether to upgrade. "Various improvements and bug fixes" is worse
than no entry; so is an entry that restates the commit subject in longer words.
The doc-specific tells are listed in `docs-update/SKILL.md` step 5 and apply here
unchanged: no throat-clearing, no empty intensifiers, no hedging where the answer
is definite.

## 5. Verify before tagging

```bash
~/.claude/skills/gh-release/scripts/versiontool.py verify <old-version>    # must print result=clean
```

**Run this even when a repo task did the bump.** A bumper written six months ago
does not know about the file someone added last week, and that is exactly how a
version goes stale in one corner of the tree. Changelog history is ignored; only
editable files count.

Then run the project gate (build, test, lint). A tag pointing at a red commit
has to be deleted and re-cut, and deleting a pushed tag is messy for anyone who
already fetched it.

## 6. Commit, tag, push

Skip whatever step 1's task already did. If it committed, tagged and pushed,
go straight to step 7.

Otherwise: one commit for the release, subject prefixed with the branch name per
`~/.claude/CLAUDE.md`. The bump and changelog belong in the same commit; they are
one change.

```bash
git commit -m "[<branch>] chore(release): <version>"
git tag -a <tag> -m "<tag>"
git push && git push origin <tag>
```

Push the commit **before** the tag. A tag that arrives first can start a build
against a commit the remote does not have yet.

Note the push time; it bounds the watch.

## 7. Watch until the release exists

```bash
~/.claude/skills/gh-release/scripts/relwatch.py watch <tag> --timeout 1800
```

| Verdict | Meaning |
| --- | --- |
| `released url=...` | done |
| `ci_failed run=... conclusion=...` | step 8 |
| `no_run` | nothing was triggered. The tag matched no pattern - run `preflight` |
| `still_running` | not a failure. Watch again |

Green CI is not a release: the publish step can lag the build, so the watcher
keeps waiting until the Release object actually exists.

Same subagent rule as `babysit-pr`: a single watch inside one Bash call
(`--timeout 540`) is cheaper inline; delegate only when the wait spans several
calls. Do not stop before the release exists or you have a blocker.

## 8. When CI fails after the tag

The tag is wrong, so retract it rather than patching around it:

```bash
git push --delete origin <tag>
git tag -d <tag>
```

Fix, then re-cut from step 5 with the **same** version number. Do not bump to
skip past a failure: a version nobody ever released is not worth burning, and
the gap invites someone to go looking for the missing release.

If the tag has been public for a while, or a partial release exists, say so and
let the user choose between deleting it and shipping a patch on top.

## 9. Report

PR/compare link, the tag, the release URL, what changed, and every file bumped.
If anything was left for the user, name it.
