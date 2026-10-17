# time-tracking config

Copy to `~/.config/skills/time-tracking/config.md` and fill it in. Every list can be empty.

## Sources

- Claude sessions: `~/.claude/projects/*/*.jsonl`
- Shell history: `~/.zsh_history` (extended format `: <unix_ts>:<duration>;<cmd>`, encoding latin1)
- Browser history: Brave, `~/.config/BraveSoftware/Brave-Browser/Default/History` (Chromium SQLite; Chrome is `~/.config/google-chrome/Default/History`)

## Tickets

- Ticket pattern: `PROJ-\d+`
- Tracker URL pattern: `example.atlassian.net/browse/(PROJ-\d+)`, fallback bucket `admin:jira`
- Tracker page title: `[PROJ-NNNN] <title> - Jira` -> title after the bracket
- Forge: GitLab (`/-/merge_requests/N` -> `MR#N`, `/-/issues/N` -> `issue#N`, else `gitlab:misc`); MR title `<title> (!N) · Merge requests · ...` -> title before `(!N)`
- Collapse line for small ticket touches: "ticket review"

## Claude project dirs

- Work project: `my-work-repo` (any project dir containing it). Session-level ticket inheritance; no ticket -> `my-work-repo:no-ticket` (work)
- Home dir (dotfiles, mixed): `-home-user`. Topic regexes:
  - ticket pattern -> work, bucket = ticket
  - `dotfiles|keybinding|terminal` -> `personal:dotfiles`
  - `time.track|timesheet|billable|breakdown|hours worked` -> `admin:time-tracking` (work)
  - else inherit
- Personal projects (any dir under `-home-user-repos-*`): `personal:<project>`

## Work domains (browser)

Domain -> bucket overrides:

- `mail.example.com` -> `admin:mail`
- `chat.example.com` -> `admin:chat`
- `sentry` -> `admin:sentry`

Allowlist:

```
localhost
app.example.com
git.example.com
example.atlassian.net  # tracker - strong work signal, mine URL paths and titles for ticket IDs
```

## Personal (browser)

- Home infra, bucket `personal:home-infra`: home.example.net
- Always leisure (exclude even if mixed): reddit, youtube, x.com
- Mixed use (inherit from context): github.com, claude.ai, google.com
  - github title regex `my-hobby-project` -> `personal:home-infra`

## Output

- Path: `~/time-tracking-YYYY-MM-DD-to-YYYY-MM-DD.md`
