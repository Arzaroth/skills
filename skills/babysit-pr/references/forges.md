# Forges, credentials, and the prctl contract

`scripts/forge.py` is the only place that knows forge API shapes; `prctl.py` is
a thin CLI over it. Use them instead of hand-writing `gh api` / `glab api` calls,
so there is one thing to fix when a forge changes.

```
forge.py     detection, credentials, GitHubForge / GitLabForge
prctl.py     CLI over forge.py
classify.py  reviewer verdict classifier (pure; tested by test_classify.py)
prwatch.py   poll loop, imports both
```

Python code should `from forge import detect` rather than shelling out to the
CLI: `prwatch.py` does, which is why it spawns no subprocess per poll.

## A PR number is not an address

`#720` names a pull request only once you say in which repo. Both scripts used to
fill that in from the working directory, which is a guess about intent rather
than a statement of it - and a subagent inherits no working directory from the
prose that spawned it. Two watches returned `verdict=merged` for PRs in
unrelated repos that way; under `merge: auto` that verdict ends the job.

So every command that names a PR requires an explicit repo:

```
--repo git.example.com/Group/repo    full slug, works from anywhere
--repo owner/name                    borrows the host from the current checkout
--repo https://host/owner/name.git   a remote url, also `git@host:owner/name`
BABYSIT_REPO=...                     same values, for a shell that runs many
```

`prctl.py detect` prints `slug=`, which is exactly what `--repo` wants. Get it
once per PR and pass it to everything.

Refusing exits **64**, and the message names the repo the process is standing in,
so accepting the guess is one copy-paste. `detect`, `find` and `creds` describe
the current checkout and take no PR, so they need no address.

`prwatch.py` also takes `--expect-branch B` and refuses (`giveup reason=wrong_pr`)
if the PR's head branch is not `B`. The repo can be right while the number is
stale - a restack, a closed-and-reopened PR, a number copied from an earlier run.

The trigger budget lives in `~/.claude/babysit-pr/state` (`BABYSIT_STATE`), not
in git config, because git config follows the working directory rather than the
repo being watched.

## Commands

```
prctl.py --repo SLUG [--as NAME|--rotate] <command> [args]

detect                            forge, slug, host, path, cli, auth status
find <branch>                     open PR number for a head branch ("" if none)
create --head B --base B --title T [--body X|--body-file F] [--draft]   -> number
state <pr>                        KV snapshot
threads <pr> [--unresolved]       NDJSON, one review thread per line
reviews <pr>                      NDJSON, top-level / review-body comments, oldest first
approvals <pr>                    NDJSON of approvals and change-requests
reply <pr> <thread_id> <body|-|--body-file F>   answer an existing thread
review-comment <pr> --file F --line N --body B   open a NEW line-anchored thread
resolve <pr> <thread_id>
comment <pr> <body|-|--body-file F>          top-level, non-resolvable
trigger <pr> [coderabbit|coderabbit-full] [--force]
                                  skipped (exit 0) while the reviewer's commit
                                  status says it is already reviewing the head
review-status <pr>                sha= state= busy= stuck= description= at=
                                  the reviewer's commit status on the head
body <pr>                         read the PR/MR description
set-body <pr> <body|-|--body-file F> [--drop-bot-sections]
                                  replace it, keeping reviewer-owned sections
rebase <pr>                       update head branch against its base
refresh-diff <pr>                 GitLab only: recompute a stale diff
merge <pr> [--squash] [--keep-branch]
creds                             credential names usable for this host
```

`state` prints:

```
number= url= head= head_branch= base= state= draft= ci= mergeable=
merge_state= threads_total= unresolved=
```

plus, on GitLab: `rebase_in_progress= merge_error= blocking_discussions_resolved= changes=`.

`threads` records are uniform across forges:

```json
{"id","author","source":"bot|human","file","line","resolved","outdated",
 "url","replies","body","thread":[{"author","body"}]}
```

Every body-taking command accepts prose, `-` for stdin, or `--body-file`. A body
that is **only** a filesystem path is refused: passing the path where the content
belonged posted `/tmp/.../reply1.md` to a live MR, and the reviewer could only
answer that it cannot read local paths.

`id` is the stable thread handle: a GraphQL `PullRequestReviewThread` id on
GitHub, a discussion id on GitLab. Pass it straight back to `reply` / `resolve`.

## Forge detection

From `git remote get-url origin`. github.com and gitlab.com by hostname;
self-hosted by probing `/api/v4/version` (GitLab) then `/api/v3/` (GitHub
Enterprise). The answer is cached in `git config babysit.forge`. Override with
`BABYSIT_FORGE=github|gitlab`, the remote with `BABYSIT_REMOTE`, and the target
repo with `BABYSIT_REPO=owner/repo`.

**You never need to export `GH_REPO`.** `gh` and `glab` are used as transport and
auth only: every request names its own repo (`repos/<path>/...`, or a
url-encoded project id), so nothing depends on a CLI's idea of what the working
directory means. There are no `gh pr` subcommands left - `create`, `rebase` and
`merge` all go through the REST API, which is what made them vulnerable to gh's
own repo resolution in the first place.

Only **gh** and **glab** are implemented. For anything else, write an adapter
that satisfies the contract above rather than scattering API calls through the
skill.

## Credentials

Default location `~/.claude/babysit-pr/credentials`, override with
`BABYSIT_CREDENTIALS`. Plaintext, **mode 0600** (prctl warns otherwise):

```
# name      host                 token
alt1        git.example.com      glpat-xxxxxxxxxxxxxxxxxxxx
alt2        git.example.com      glpat-yyyyyyyyyyyyyyyyyyyy
ci-bot      github.com           ghp_zzzzzzzzzzzzzzzzzzzz
```

`*` in the host column matches any host. Tokens need API scope for comments and
notes; a token used for `merge` also needs write access.

`--as NAME` uses one specific credential. `--rotate` advances a per-host counter,
where **slot 0 is the user's own default auth**, so a missing credentials file
degrades to normal behaviour instead of failing. The counter and the quota state
live in `~/.claude/babysit-pr/state` (`BABYSIT_STATE`), not in git config, which
would follow the working directory rather than the host.

### A rate limit belongs to an account, not to a PR

CodeRabbit's quota is per user and hourly, so "Review limit reached" is a fact
about whoever asked. `prwatch.py` records it against that identity, keyed by host
and account with the reviewer's own stated window as the expiry (`limited_until`,
one hour when it does not say). Two things follow, both unless the watch runs with `--no-rotate`:

- **Moving to an account with quota left is not a repeat nudge**, so it does not
  wait out `--trigger-gap`. The gap stops us asking the *same* account twice; a
  different account's allowance is untouched. One notice is answered once
  (`Verdict.blocked_id`) - it stays the newest bot comment for the whole window,
  and reacting on every poll would walk through every credential in minutes.
- **A watch never starts on an account it knows is spent.** `avoid_limited()`
  steps off it before the first trigger, and rotation skips spent slots rather
  than visiting them in order. Asking as an account that said "review limit
  reached" ten minutes ago buys nothing and the trigger slot is gone either way.

`--max-triggers` still binds. Rotation multiplies the quota, not the number of
comments on the PR. When every account is spent there is nothing to switch to,
and the answer is to wait out the stated window.

### What rotation is and is not for

Rotation exists because CodeRabbit's review quota is per user, so triggering as
different accounts your organisation holds seats for multiplies the usable
allowance.

**Trigger only.** Never `--as` or `--rotate` a `merge`, an approval, or anything
that carries authorship weight. That is not a quota workaround, it is forging a
reviewer's identity on the record.

## Forge quirks worth remembering

**GitLab keeps stale diff refs** after a retarget or a force-push: the MR shows
zero changes while the branch clearly differs. `prctl.py refresh-diff` runs the
close/reopen cycle that makes GitLab recompute. Check for it whenever
`changes=0` on an MR you know has commits.

**GitLab merge is a PUT that can return 200 with a non-merged state.** Always
read the returned `state`; `prctl.py merge` exits non-zero and prints
`MERGE_FAILED` when it is not `merged`.

**GitHub thread ids are GraphQL node ids**, not comment ids. A REST comment id
will not resolve a thread.

**Branch protection can reject a merge** that `mergeable=true` promised. Both
forges signal this differently and neither is an HTTP error you can rely on, so
`Forge.merge` reads the response and raises `ForgeError` unless it actually says
merged. Never assume a merge call succeeded.

**Bots skip non-default base branches.** See `ai-reviewers.md`. This bites every
stacked PR.

## Background processes

Long poll loops must run in the **foreground** of whatever process owns them.
Backgrounded watchers on this machine get killed by the memory guard, and a
killed watcher looks exactly like a PR that went quiet.
