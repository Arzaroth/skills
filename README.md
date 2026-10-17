# skills

Agent skills I use every day with Claude Code, published for anyone to install.

## Install

With [`skills`](https://skills.sh):

```bash
npx skills add Arzaroth/skills                     # pick from the list
npx skills add Arzaroth/skills -s babysit-pr -g    # one skill, globally
```

## The skills

| Skill | What it does |
| --- | --- |
| [babysit-pr](skills/babysit-pr/SKILL.md) | Opens a pull/merge request and stays with it until it merges: triggers the reviewers, watches for their verdict, works each round of findings, pushes, re-triggers, merges when clean. GitHub (`gh`) and GitLab (`glab`), AI reviewers (CodeRabbit) and humans. |
| [address-review-findings](skills/address-review-findings/SKILL.md) | Works one round of review comments: verifies each finding against the current code, fixes what is still valid, replies with a justification to what is not, pushes and answers every thread. |
| [merge-stack](skills/merge-stack/SKILL.md) | Merges a stack of dependent PRs bottom-up, each through babysit-pr, triaging every finding against the stack tip. Flips the project's squash setting for the run and restores it. |
| [gh-release](skills/gh-release/SKILL.md) | Cuts a GitHub release: changelog, version bump (preferring the repo's own release task), tag, push, then watches the release workflow until the Release exists. |
| [docs-update](skills/docs-update/SKILL.md) | Checks whether the repo's own docs still describe the code after recent changes, and updates the stale ones. Its prose goes through the [`unslop`](https://github.com/cursor/plugins) skill. |
| [next-features](skills/next-features/SKILL.md) | Explores the project and proposes what to build next, including ideas not in the roadmap. |
| [render-plan-html](skills/render-plan-html/SKILL.md) | Renders a Claude Code plan (`~/.claude/plans/*.md`) as styled standalone HTML and opens it. |
| [thermo-nuclear-code-quality-review](skills/thermo-nuclear-code-quality-review/SKILL.md) | An extremely strict maintainability review: abstraction quality, giant files, spaghetti conditions, "code judo" restructurings. |
| [analyse-causes](skills/analyse-causes/SKILL.md) | Writes a ticket's root cause and fix into one tracker field and points the other analysis fields at it. Configurable. |
| [new-repo](skills/new-repo/SKILL.md) | Sets up a new project repository the way its siblings are: forge + mirror, licence, green skeleton, CI with a coverage floor, docs, project skills, in layered commits. Configurable. |
| [time-tracking](skills/time-tracking/SKILL.md) | Reconstructs a timesheet for a date range from Claude Code session logs, shell history and browser history. Configurable. |

babysit-pr, address-review-findings and merge-stack work together: the last
two call babysit-pr's scripts, so install them side by side.

## Configuration

The configurable skills read `~/.config/skills/<skill>/config.md`. Each ships a
`config.example.md` to copy there and fill in; the skill stops and says so when
its config is missing. Keeping the config outside the skill directory means an
`npx skills update` never overwrites it.

## Credits

[thermo-nuclear-code-quality-review](skills/thermo-nuclear-code-quality-review/)
is adapted from `thermo-nuclear-review` in Cursor's
[thermos plugin](https://github.com/cursor/plugins/tree/fd6dd6f7276956a532bb78a748a8d2818b6eb5f4/thermos),
MIT, Copyright (c) 2026 Cursor. Its directory carries the original licence and
a notice.

## Licence

MIT OR WTFPL, at your option (see [LICENSE](LICENSE)), except where a skill's
directory carries its own licence.
