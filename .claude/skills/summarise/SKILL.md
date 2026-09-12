---
name: summarise
description: Write passage summaries for any pending requests in data/summary_requests/. Use when the user runs /summarise, asks to fill in pending summaries, or the Bible study tool's Summary panel is waiting on a request.
---

# Writing a passage summary

The Bible study tool's Summary panel can't call Claude on its own (no API key),
so pressing **Run summary** writes a request bundle to disk instead and waits
for you to fill it in.

## What to do

1. List `data/summary_requests/`. If it's empty, say so and stop — nothing is
   waiting.
2. For each request file `<KEY>.json`:
   - Read it. It contains the passage text (BSB), the NIV publisher's section
     headings, repeated terms, sentence-initial discourse markers, the ESV's
     cross-reference apparatus with direct quotations flagged, glossary hits,
     and the overall tone label.
   - Write the summary to `data/summaries/<KEY>.json` in the schema below.
   - Delete the request file.
3. Report which passages you wrote. The browser polls every few seconds, so the
   panel fills itself in — the user doesn't need to reload.

Do all pending requests in one pass, not just the first.

## Schema

```json
{
  "reference": "Ephesians 1:1-14",
  "generated_at": 1788600000,
  "sections": [
    { "title": "Chosen before the foundation", "start": 3, "end": 6,
      "line": "Election framed as praise, not as a puzzle." }
  ],
  "themes": [
    { "theme": "In Christ", "note": "Every blessing is located in him, never alongside him.",
      "verses": [3, 4, 7, 11, 13], "triggers": ["in Christ", "in him", "in whom"] }
  ],
  "references": [
    { "ref": "Psalm 68:18", "verses": [8], "kind": "quotation",
      "why": "Recasts a victory procession so the spoils become gifts to the church." }
  ],
  "purpose": [
    "Paul wants the Ephesians to believe their standing was settled before they existed, so that nothing later can unsettle it."
  ]
}
```

`generated_at` is a Unix timestamp — use the current time.

## The brief

**Sections — 3 to 5.** The argument's real movements. Use the publisher's
headings and the discourse-marker breaks as *evidence*, but override them where
the argument plainly moves somewhere else; the panel exists precisely because
publisher headings aren't always the structure. `start`/`end` must be verse
numbers present in the bundle, and the sections should tile the passage in order
without gaps or overlaps. `line` ≤ 15 words.

**Themes — 3 to 4.** What actually recurs, not what a chapter on this topic
usually contains. The `repeated_terms` list is a strong hint but not the answer:
a theme can be carried by varied wording, and a frequent word can be incidental.
`triggers` are lowercase substrings that appear verbatim in the passage — they
drive inline highlighting, so a trigger that matches nothing is a dead link.
`note` ≤ 15 words.

**References — up to 5.** Only from `cross_references` in the bundle. Never
invent one. Prefer `kind: "quotation"` entries; include parallels only where the
connection is doing real work. `why` answers **what the citation does for this
author's argument** — not what the source passage means on its own. "Isaiah 53
is about the suffering servant" is a failure; "borrows Isaiah's servant language
so the church reads the cross as planned, not as defeat" is the job. ≤ 25 words.

**Purpose — 2 to 3.** The form is fixed: *"[Author] wants us to know/believe X
so that / because Y."* Both halves are required — a claim with no consequence
isn't a purpose statement. These are the punchiest lines in the panel, so make
them land. ≤ 30 words each.

## Rules

- Ground everything in the supplied verses and metadata. If the bundle doesn't
  support a claim, don't make it.
- Name the author as the text and tradition do (Paul, John, the Chronicler,
  the psalmist). Where authorship is genuinely disputed, say "the author".
- Describe what the text claims, in its own voice. This is a study tool: report
  the author's argument, don't adjudicate it or hedge it into mush.
- Write plainly. No sermon voice, no filler, no restating the verse you just
  cited. British English.
- Respect the length caps — the panel is a sidebar, not an essay.
