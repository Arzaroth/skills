---
name: time-tracking
description: >
  Reconstruct work done over a given time period by cross-referencing Claude Code session logs,
  shell history, and browser history. Outputs a per-day breakdown with active-time blocks,
  topics inferred from prompts/commands/URL titles, and times rounded to the half-hour for
  timesheet entry. Use when the user asks for a work log, time tracking report, daily breakdown,
  "what did I work on", timesheet, billable hours summary, or anything similar referencing a
  date range. Triggers: "time tracking", "what did I do", "work log", "daily breakdown",
  "time sheet", "billable", "activity report".
---

# Time Tracking Skill

Reconstruct work timeline for a given period from local activity signals. Output: per-day blocks with topics + rounded duration per task.

## Config

Read `~/.config/skills/time-tracking/config.md` before anything else. It holds everything user-specific: source paths, the ticket pattern and tracker/forge URL and title patterns, the Claude project dirs (work, home, personal) with their topic regexes, the work-domain allowlist and bucket overrides, the personal and leisure lists, and the output path. Every rule below that says "configured" reads from it. If it is missing, tell the user to copy `config.example.md` (next to this file) to `~/.config/skills/time-tracking/config.md` and fill it in, and stop.

## Sources

| Source | Notes |
|--------|-------|
| Claude sessions | Per-event `timestamp` ISO8601. Contains user prompts, tool uses, sub-agent (`Agent`) invocations. |
| Shell | zsh extended format `: <unix_ts>:<duration>;<cmd>`. Encoding latin1 (not utf8). |
| Browser (Chromium-based) | SQLite. Tables `visits`, `urls`. `visit_time` = chromium epoch microseconds. Convert: `unix = visit_time/1000000 - 11644473600`. **Copy to /tmp first** if the browser is running (DB lock). |

## Method

### 1. Collect events with timestamps from all 3 sources

- Claude: every JSONL line with `timestamp`. Track `session` (= filename) and `dir` (= project dir) for ticket inheritance.
- Shell: every history line matching `^: (\d+):\d+;`.
- Browser: visits since cutoff. Do NOT pre-filter - keep ALL events for classify step. Filtering happens in `classify()` below.

**Extend END past midnight** when user worked late. If user says "until ~1:30am", set `END = next_day 03:00` and let clustering handle it. Tag events past midnight with `+HH:MM` in output.

### 2. Classify each event (work/personal/inherit)

Per-event `classify(e) -> {work, bucket}`:
- `work: true` = billable, has explicit bucket (ticket/MR/admin tag)
- `work: false` = personal/leisure, has `personal:*` bucket
- `work: null, bucket: null` = ambiguous (shell, mixed domains, tool_result) -> inherits last classified bucket during attribution walk

Classification rules:
- **Claude personal project dirs** (configured): `personal:<project>`
- **Claude home dir** (configured, dotfiles / mixed): the configured topic regexes, in order (ticket pattern -> work with bucket = ticket; personal topics; `admin:time-tracking`), else `null` (inherit)
- **Claude work project dir** (configured):
  - session-level ticket inheritance: pre-pass each session JSONL, build `[{ts, ticket}]` timeline of user prompts matching the ticket pattern. Resolve ticket-at-ts by binary search/walk.
  - else: `<work-project>:no-ticket` (work)
- **Browser tracker**: configured tracker URL pattern > ticket pattern in title > configured fallback bucket
- **Browser forge**: configured MR/issue URL patterns -> `MR#N` / `issue#N`, else `<forge>:misc`
- **Browser work domains**: configured domain -> bucket overrides, then the allowlist
- **Browser leisure/personal-home-infra**: `personal:<host>` (false)
- **Browser mixed** (configured list): configured title regexes for personal projects -> personal, else `null` inherit

### 3. Cluster into blocks (30-minute gap rule)

Sort events by timestamp. New block when gap > 30min from previous event. Block duration = `last_ts - first_ts`.

### 4. Per-bucket span attribution (DEFAULT - handles multitasking + SPAs)

For each block, group events by bucket (resolving `null` inherit-from-context). For each bucket compute **span** = sum of (last_event - first_event) for runs of bucket events where intra-bucket gap < `BUCKET_INTERNAL_GAP = 10min`. Each run gets credit. Single-event bucket gets minimum 60s.

```javascript
function spanBucket(events, internalGap = 600) {
  if (events.length === 0) return 0;
  events.sort((a, b) => a.ts - b.ts);
  let total = 0, runStart = events[0].ts, runEnd = events[0].ts;
  for (let i = 1; i < events.length; i++) {
    if (events[i].ts - runEnd > internalGap) { total += runEnd - runStart; runStart = events[i].ts; }
    runEnd = events[i].ts;
  }
  total += runEnd - runStart;
  return total || 60;
}
```

**Why span over per-event-cap:**
- Allows parallel work + personal sessions to both get credit (multitasking honest)
- Bypasses SPA visit-burst bug naturally (first/last span ignores burst density)
- Sum across buckets can exceed block wall time - that's correct, reflects "time engaged with task X" not "exclusive time"
- For non-multitask blocks, span ≈ per-event-cap result

**Trade-off:** for billing, span may over-state if user truly was "100% on personal" with only sporadic work tab checks. Compare with strict per-event-cap (§4b) when in doubt.

### 4b. Per-event 5min-cap attribution (SECONDARY view, mutually-exclusive)

For each event ordered in block:
- `dur = min(next_event.ts - this.ts, ATTR_CAP)` where `ATTR_CAP = 300` (5min)
- Last event: `dur = 60` (1min trailing)
- `bucket = e.bucket || lastClassifiedBucket`
- Add `dur` to `buckets[bucket]`

Gives strict "what was foreground" attribution. Under-counts SPAs without burst-dedup (see §5). Under-counts work in multitask blocks. Useful as a lower-bound figure.

### 5. SPA visit-burst (only relevant if using §4b strict mode)

If using strict per-event attribution, SPAs (chat / webmail / Notion) emit clustered visits with near-0s gaps -> bucket gets near-0min attribution despite real usage.

**Fix for §4b:** dedupe same-bucket events within 30s window before attribution walk:

```javascript
const dedup = []; const lastBurst = {};
for (const e of events) {
  const b = e.bucket;
  if (b && lastBurst[b] !== undefined && (e.ts - lastBurst[b]) < 30) continue;
  dedup.push(e);
  if (b) lastBurst[b] = e.ts;
}
```

Span method (§4) handles this naturally - no dedup needed.

### 6. Rounding

- **Per-task r30** (`max(30, ceil(min/30)*30)`): inflates - 12 small touches × 30m floor = 6h fake.
- **Per-task r15** (`max(15, ceil(min/15)*15)`): less inflation.
- **Suggested timesheet collapse** (RECOMMENDED): group small tickets (<10min raw) into a single line named by the configured collapse label at 30-45m combined. Bigger tasks rounded individually. Total stays close to raw work hours.

Show all three in output, recommend collapse.

## Domain lists

The work-domain allowlist, the personal home-infra hosts, the always-leisure list (excluded even if mixed) and the mixed-use list (return `{work: null, bucket: null}` to inherit the last classified bucket, except configured title regexes) all come from the config. Home services stay personal even when they look like infra.

## Tooling pattern

Use `mcp__plugin_context-mode_context-mode__ctx_execute` (javascript) for analysis. Never `cat`/`grep` raw JSONL into context.

Full pipeline:

```javascript
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const START = new Date('YYYY-MM-DDT00:00:00').getTime() / 1000;
const END = new Date('YYYY-MM-DDT23:59:59').getTime() / 1000;  // extend if user worked late
const GAP = 30 * 60;
const BUCKET_INTERNAL_GAP = 10 * 60;  // 10min - splits sub-spans within bucket
const ATTR_CAP = 300;  // 5min - only used if computing §4b secondary view
const BURST_WINDOW = 30;  // 30s - only used if computing §4b secondary view

// Pre-pass: per-session ticket timeline (for claude work-project events)
const sessionTickets = {};  // {sessionId: [{ts, ticket}, ...]}
// Walk each claude jsonl, match user.message.content for the configured ticket pattern, append.

function sessionTicketAt(sid, ts) {
  const arr = sessionTickets[sid];
  if (!arr) return null;
  let cur = null;
  for (const x of arr) { if (x.ts <= ts) cur = x.ticket; else break; }
  return cur;
}

// classify(e) -> {work, bucket} | null   (see Method §2)
function classify(e) { /* ... */ }

// Collect: claude + shell + browser -> events[]
// Sort, classify each
// Cluster with GAP -> blocks
// Per block: group by bucket (resolving null inherit), compute spanBucket(events, BUCKET_INTERNAL_GAP)
// Aggregate per-task totals across blocks
```

No reference implementation ships with this skill; derive it from the pipeline above.

## Output format

Write to the configured output path. Structure:

1. **Summary table**: date | raw work / personal hours | block 1 / 2 / ... split | recommended billable rounded.
2. **Per-task table** (day-level across all blocks): bucket | raw min | r15 | r30 | MR/ticket title resolved from browser page titles.
3. **Suggested timesheet collapse** (RECOMMENDED): hand-merged table grouping small touches into the collapse line etc. Total close to raw work hours, not inflated r30 sum.
4. **Per-block sections**: each block with time range (use `+HH:MM` for past-midnight events), raw/work/personal minutes, top work buckets, top personal buckets, notable claude prompts, top shell cmds, top browser titles.
5. **Notes**: method caveats, SPA undercount risk if applicable, ambiguous buckets (e.g. `<work-project>:no-ticket` and what it likely covers based on prompt content + adjacent MR visits).

**Ticket IDs + MR titles are gold for timesheets** - resolve titles from browser page titles. Use the MR's title, not just `MR#164`.

**Ticket-title resolution:** parse browser titles to extract human-readable name. Patterns:
- Tracker and forge: the configured title patterns

## Caveats to surface to user

- **Wall-clock measured, not focus time.** Includes reasoning, tool exec, sub-agent wait, idle <30min.
- **Span overlap is intentional** (§4 default). Sum of work + personal can exceed block raw when user multitasks. Reflects "time engaged with task X" not "exclusive time". Confirm with user if billable figure should be span (multitask honest) or per-event-cap (strict exclusive lower-bound).
- **Verify multitask blocks:** if block has both work and personal spans both >50% of block raw, user probably multitasked. Mention in report and offer strict view alongside.
- **SPA burst undercount** only applies to §4b strict mode. Span method handles SPAs naturally.
- **Mixed-domain events inherit** last classified bucket (github/google/etc.). Risk: inherits wrong context if user pivoted silently.
- **Per-task r30 rounding inflates** when many small ticket touches. Always present the collapse table.
- **Personal/work split:** the configured personal projects, home-dir personal topics and home-infra hosts are personal. Time-tracking work itself (running this skill) is `admin:time-tracking` = work-adjacent.
- **`<work-project>:no-ticket` bucket** is real work but couldn't pin to a single ticket. Manual inference from prompt content + adjacent MR visits needed. Document the split in the suggested collapse.
- **Past-midnight work** clusters into same evening block if gap to last event <30min. Tag with `+HH:MM`.
- **Day boundary:** if a block spans midnight (e.g. 22:00 -> +02:00), assign it to the PRIOR day (the user-perceived "yesterday"). For next day's report, START at first event >= 03:00 or similar wake-up time. Don't double-count the overnight block.

## Common variations user may request

- **Different period:** parametrize START/END.
- **Extend past midnight:** set END = next_day 03:00 if user worked late. Late events tagged `+HH:MM`.
- **Per-MR or per-ticket breakdown:** use per-event attribution (default). Already gives this.
- **Exclude personal/leisure entirely from totals:** filter `bucket.startsWith('personal:')` from per-task table; show personal as a separate footnote line.
- **Different gap threshold:** 15min stricter, 60min looser.
- **Different rounding:** r15 (`ceil/15*15`), r30 (`ceil/30*30`), nearest, floor. Suggested-collapse always recommended over auto-rounded.
- **Different attribution method:** §4 span (default, multitask-friendly) vs §4b strict 5min-cap (exclusive, lower-bound). Offer both when block is multitask.
- **Different BUCKET_INTERNAL_GAP:** 5min stricter (splits sub-spans more aggressively, reduces span totals), 15min looser. Default 10min.

## When NOT to use

- User asks for *future* time estimation - different task.
- User asks for git/PR activity log - use `git log`, not this skill.
- User asks for system uptime / login times - check `last`, `who`, not this.
