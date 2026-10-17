---
name: new-repo
description: Set up a new personal project repository the way its siblings are set up - repo on the source-of-truth forge with an optional push mirror, repo identity, licence, a compiling skeleton, CI with a line-coverage floor, pinned toolchain, CLAUDE.md / README / ROADMAP / TODO / CHANGELOG, the brain/ knowledge base, and the project skills (brain, feature-treatment, release), all in layered green commits. Hosts, owner, identities, families, siblings, licences and the coverage floor come from the config. Asks the main decisions (language, monorepo, family, visibility, licence...) with AskUserQuestion first. Use when asked to "create a repo", "set up a new project", "new repo with a GitHub mirror", "scaffold X like the others", or "/new-repo".
---

# New repo

Everything between "we have a name and a design" and "the repo exists on every
host, CI is green, and the next session can start on milestone 1". This file
records the procedure; the config holds the user's hosts and conventions, and
the siblings hold the current templates.

## Config

Read `~/.config/skills/new-repo/config.md` before anything else. It sets where repos
live, the default branch and commit rules, the source-of-truth forge and its
CLI, the mirror and how it is wired, the families (identity, CI host, release
host), the sibling templates per stack, the licences in use, the coverage
floor, the roadmap shape, and the memories to read and update. Below,
`<forge-cli>`, `<owner>`, `<mirror-owner>`, `<branch>`, `<floor>` and
`<repos>` stand for those values. If it is missing, tell the user to copy `config.example.md` (next to this file) to `~/.config/skills/new-repo/config.md` and fill it in, and stop.

**Never invent a number, a convention or a "(Recommended)" label.** Every
default you propose must come from the config, a sibling repo or a memory, and
the option text says which one. When none settles a choice, ask without a
recommendation.

## 0. Inputs

- The project name. If there is none yet, settle it first (it must be free on
  every configured host and every package registry the project will publish
  to).
- The design: an artifact, a plan in `~/.claude/plans/`, or the conversation.
  The docs layer is written from it.

Check the name is free before anything else:

```bash
ls -d <repos>/<name>
<forge-cli> api repos/<owner>/<name>         # must 404
gh repo view <mirror-owner>/<name>           # must 404, when there is a GitHub mirror
```

plus `cargo search` / PyPI / npm as the stack needs.

## 1. Survey the siblings

The templates live in the sibling repos, not in this skill: they keep
evolving, so read them at run time.

```bash
cd <repos> && ls -t | head -30
for r in <3-5 recent siblings>; do ls -A $r; ls $r/.claude/skills; ls $r/brain; ls $r/.github/workflows $r/.forgejo/workflows; git -C $r config --local user.email; done
```

Pick the **closest sibling** by stack and family (the config's sibling table
is the starting point; verify each entry still holds) and read its scaffolding
in full: `CLAUDE.md`, `README.md`, `ROADMAP.md`, `TODO.md`, `CHANGELOG.md`,
`brain/BRAIN.md` and the two indexes, a leaf doc, `.claude/skills/*/SKILL.md`,
the CI workflow, `scripts/coverage.sh`, the toolchain pins.

Also read the memories the config lists.

## 2. Ask the main decisions

Use **AskUserQuestion** before creating anything public-facing. Up to four
questions per call; two calls is normal. Each option's description names the
sibling it copies. Ask only what the design, the config and the siblings leave
open, and skip a question whose answer is already given.

First call:

- **Language / stack**: options drawn from the siblings' stacks, plus other.
- **Layout**: single package, or monorepo / workspace (which members: crates,
  `apps/` + `shared/`...). Recommend from the design's component list.
- **Family**: one option per configured family, each naming its identity, CI
  host and release host. Skip when the config has a single family.
- **Visibility**: public everywhere, private until first release, or the
  source-of-truth forge only with no mirror yet.

Second call:

- **Licence**: the configured licences, each naming the siblings that use it,
  or the user's pick. Copy the file from the sibling.
- **Distribution**: what the release skill will target (GitHub Releases +
  installer, PyPI wheels, crates.io, Homebrew, forge releases, a deployed
  service, nothing yet). If packaging is a later milestone, the release skill
  stops early until it lands.
- **Scope of the first commits**: docs + compiling skeleton (recommended:
  every sibling starts green), or docs and skills only.

Do not ask about the docs shape (always `brain/`), the coverage floor
(section 5) or the commit layering (global CLAUDE.md); those are settled.

## 3. Build it locally, in layers

```bash
mkdir -p <repos>/<name> && cd <repos>/<name> && git init -q -b <branch>
# when the family sets a repo-local identity:
git config user.name '<name>' && git config user.email '<email>'
```

Commit subjects and trailers follow the config. Each layer passes the full
gate (format, lint with warnings as errors, tests, and from layer 2 the
coverage check) before it is committed.

1. **`chore: workspace skeleton and licence`**: the licence file(s), the
   manifest(s) for the chosen layout, the toolchain pin, `.gitignore`, and a
   minimal entry point with one real test (e.g. the binaries print the
   package version). Pin the toolchain both ways the siblings do, e.g. for
   Rust `rust-toolchain.toml` and `.mise.toml` with
   `components = clippy, rustfmt, llvm-tools-preview` (the last one is for
   coverage). Strict lints at the workspace level (Rust: `clippy::pedantic`
   as warn, `-D warnings` in CI). Internal packages get a `<name>-` prefix
   when their bare names could collide on a registry.
2. **`ci: ...`**: the CI workflow copied from the closest sibling and trimmed
   to what exists. GitHub actions pinned to a commit SHA with the version in a
   comment, `persist-credentials: false`, `permissions: contents: read`. Rust
   CLIs: x86_64 and aarch64 runners. On Forgejo Actions, use
   `code.forgejo.org/forgejo/upload-artifact@v4` (the runner refuses GitHub's
   v4+ artifact actions).
3. **`ci: line coverage floor of <floor>%`**: see "Coverage" below.
4. **`docs: ...`** from the design:
   - `CLAUDE.md`: one paragraph on what it is and where to start
     (`brain/BRAIN.md`), the brain rules, the project's own invariants
     (security, shared services, wire formats...), and Conventions: the
     changelog rule, the gate commands, the coverage floor, which skill does
     releases.
   - `README.md`: tagline, a **Status** line ("not usable yet" until it is),
     planned usage, install, how it works in short, development commands,
     licence.
   - `ROADMAP.md` in the configured shape: intro, `## M0 - Foundation`
     already ticked for this setup, then milestones with decisions inline,
     `## Later`, `## Not planned`.
   - `TODO.md`: small work and the decisions to take before each milestone.
   - `CHANGELOG.md`: `# Changelog` and an empty `## [Unreleased]`.
   - `brain/`: `BRAIN.md` with a topic table and a **Find by
     question** table, `architecture/index.md`, `features/index.md`,
     `glossary.md`, `decisions.md` (including why the name), leaf docs ending
     in `## Sources`. Docs for parts not built yet carry **(Planned)** in the
     title and "Sources: none yet, lands with Mx".
5. **`chore(claude): brain, feature-treatment and release skills`**: adapt
   the closest sibling's three skills to this project. Rename paths and
   commands, and rewrite what is project-specific: the brain's
   update-after-a-change checklist, feature-treatment's review lenses (one per
   invariant in `CLAUDE.md`: e.g. crypto, interop, resource safety, plus
   correctness, reuse/simplify, tests), the release pipeline. Feature-treatment
   keeps the branch file limit and the coverage gate.

   **The release skill stays named `release`**: directory
   `.claude/skills/release/`, frontmatter `name: release`. Never rename it to
   `<project>-release`, `gh-release`, `ship` or anything else, even though a
   global `gh-release` may exist.
   Every sibling uses `release`, and the other project skills and `CLAUDE.md`
   point to it by that name. Only its description and body change per
   project. The same goes for `brain` and `feature-treatment`.
6. **`chore: wt config`** (only when new worktrees need gitignored files, as
   some siblings do): `.config/wt.toml` with the
   `wt step copy-ignored --require-include` post-start hook and a
   `.worktreeinclude` allowlist.

After the docs and skills layers, check before committing:

```bash
grep -rn $'\u2014' --include='*.md' .            # no em-dashes
python3 - <<'EOF'                               # relative links resolve
import glob, os, re
for f in glob.glob("**/*.md", recursive=True):
    for m in re.finditer(r"\]\(([^)#]+)", open(f).read()):
        t = os.path.normpath(os.path.join(os.path.dirname(f), m.group(1)))
        if not m.group(1).startswith("http") and not os.path.exists(t):
            print("broken:", f, m.group(1))
EOF
```

## 4. Hosts and the mirror

The configured source of truth is `origin`, and nothing else is.

```bash
cd <repos>/<name>
D='<one-line description>'
<forge-cli> repo create <owner>/<name> --public -d "$D" --default-branch <branch> --source . --push
```

(`--private` for a private repo.) When the source of truth is a Forgejo and
the family runs CI on GitHub, turn Forgejo Actions off; booleans go through
`--input -` as JSON, since `-f key=false` sends a string:

```bash
echo '{"has_actions": false}' | <forge-cli> api -X PATCH repos/<owner>/<name> --input -
gh repo create <mirror-owner>/<name> --public -d "$D"
```

A Forgejo push mirror to GitHub, over SSH with a per-repo deploy key, using
the interval and key title from the config:

```bash
key=$(echo '{"remote_address":"ssh://git@github.com/<mirror-owner>/<name>.git","use_ssh":true,"interval":"<interval>","sync_on_commit":true}' \
  | <forge-cli> api -X POST repos/<owner>/<name>/push_mirrors --input - \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["public_key"])')
gh api -X POST repos/<mirror-owner>/<name>/keys -f title="<key title>" -f key="$key" -F read_only=false
<forge-cli> api -X POST repos/<owner>/<name>/push_mirrors-sync
```

The sync is asynchronous and can time out. Poll until the mirror's
`<branch>` is the local HEAD, re-requesting the sync whenever `last_error` is
set:

```bash
<forge-cli> api repos/<owner>/<name>/push_mirrors   # last_update, last_error
gh api repos/<mirror-owner>/<name>/commits/<branch> --jq .sha
```

Then watch the first CI run to green on every job, on the family's CI host:

```bash
run=$(gh run list -R <mirror-owner>/<name> --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$run" -R <mirror-owner>/<name> --exit-status
```

## 5. Coverage

The configured floor, across the whole project, enforced in CI from the first
push. The only exclusion is the configured one (test infrastructure); no
excluding awkward modules, no lower bar for "glue" code. Never pick a lower
number because the code is not there yet: on a fresh skeleton the floor is
easy, and the gate then holds every later branch to it.

- A `scripts/coverage.sh` modelled on the configured reference script: default
  mode prints the summary then the files ranked by uncovered lines; `--check`
  fails below `<NAME>_COVERAGE_FLOOR` (default `<floor>`); `--html`, `--lcov`;
  checks its tools are installed and says how to get them.
- A `coverage` CI job running `shellcheck scripts/coverage.sh` then
  `scripts/coverage.sh --check`.
- Tooling per stack: Rust `cargo-llvm-cov` (`--fail-under-lines`, needs
  `llvm-tools-preview` in the pin, CLI tests that run the built binaries
  count); TypeScript vitest coverage with `thresholds.lines: <floor>`; for a
  stack no sibling has covered yet, propose a tool and say it is new.
- Document it in `CLAUDE.md` Conventions, `brain/architecture/testing.md`, and
  feature-treatment's gate.

## 6. Record and report

- Update the memories the config lists for after a new repo.
- Anything new learnt about setting a repo up (a host quirk, a user
  preference) goes into this skill, its config or a memory, not only the
  transcript.

Report to the user: every URL, visibility, identity and CI host, the commit
layers (one line each), CI status per job, the coverage figure, and what was
left for later (release workflow, decisions in `TODO.md`).

## Done when

- Every configured host has the repo; the mirror's `<branch>` equals the
  source of truth's.
- The first CI run is green on every job, coverage at or above the floor.
- Every commit is a green layer following the configured commit rules.
- `CLAUDE.md`, `README.md`, `ROADMAP.md`, `TODO.md`, `CHANGELOG.md`, the docs
  base and the three project skills exist and describe this project, not the
  sibling they came from (grep for the sibling's name).
- The project skills are named exactly `brain`, `feature-treatment` and
  `release`.
- The configured memories list the new repo.
