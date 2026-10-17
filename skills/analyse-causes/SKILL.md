---
name: analyse-causes
description: Fill a ticket's technical post-mortem - write the whole root cause + fix into one tracker field (no restating the initial symptom), then point the other analysis fields at it ("see dev notes"). Tracker, fields, language and headings come from the config. Invoke for a given ticket or the fix in progress. Triggers - "analyse des causes", "analyse technique", "root cause", "cause racine", "solution apportée", "/analyse-causes".
---

# Ticket technical post-mortem (one field + pointers)

The user puts everything in **one field** and points the other analysis fields to it, so nothing is said twice. Reproduce that workflow.

## Config

Read `~/.config/skills/analyse-causes/config.md` before anything else. It sets the tracker and its tools, the project and ticket key pattern, the field format, the write-up language and headings, the write-up field, the deflected fields with their pointer text, and the MR link fields. If it is missing, tell the user to copy `config.example.md` (next to this file) to `~/.config/skills/analyse-causes/config.md` and fill it in, and stop.

Target: the ticket passed as argument, otherwise the fix in progress in the session (diff / branch / MR).

## What to produce

**One complete write-up**, in the configured language, in markdown, with two sections under the configured headings:

1. Root cause - the technical WHY. **Do NOT restate the initial situation** (symptom / ticket description). Go back to the real origin: design choice, `constant` / `file:line`, wrong assumption. Never the visible symptom.
2. Fix - what changed and why it removes the root cause. Factual: chosen mechanism, files/constants touched, resulting behaviour. **No MR link here**: it goes in the configured MR link fields.

Rules: concise, short bullets, exact terms (`constants`, `functions`, `file:line`), checked against the real code. No em-dash (U+2014) - hyphen, comma, or rephrase.

## Procedure

1. Identify the ticket: argument = key matching the configured pattern; otherwise the fix in progress (diff / branch / MR of the session).
2. Read the ticket through the configured tool if needed, but **do not copy** its description.
3. Trace the root cause in the code and list the fix's changes (git diff / MR).
4. Write the complete write-up.
5. Show it, then write the fields in the configured format:
   - write-up field <- the complete write-up
   - each deflected field <- a one-paragraph document with the configured pointer text
   If no ticket is targeted (a fix in session without a key), only show the write-up and offer to fill the fields.
