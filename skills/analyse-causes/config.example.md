# analyse-causes config

Copy to `~/.config/skills/analyse-causes/config.md` and fill it in.

## Tracker

- Tool: Jira, through the Atlassian MCP (`mcp__atlassian__getJiraIssue`, `mcp__atlassian__editJiraIssue`)
- Site / cloudId: `example.atlassian.net`
- Project: `PROJ`
- Ticket key pattern: `PROJ-\d+` (example: `PROJ-1234`)
- Field format: ADF (rich text). Pass `contentFormat: "adf"` and ADF documents (`{ "type": "doc", "version": 1, "content": [...] }` with `heading` / `paragraph` / `bulletList`, `code` marks for identifiers). A markdown string is rejected ("Operation value must be an Atlassian Document").

## Write-up

- Language: English
- Root-cause heading: `## Root cause`
- Fix heading: `## Fix`

## Fields

- Write-up field: `customfield_10001` (**Technical analysis**)
- Deflected fields, each set to a one-paragraph document `See dev notes`:
  - `customfield_10002` (**Root cause**)
  - `customfield_10003` (**Solution**)
- MR link fields (never put the MR link in the write-up, it goes here):
  - `customfield_10004` (Merge request)
