"""Passage summaries — a short synthesis of the metadata the app already computes.

Everything else in this app is traceable: a tone score points back at the words
that drove it, a section break points at the discourse marker that opened it.
A summary can't work that way. "Why does Paul reach for Psalm 68 here?" and
"the author wants us to believe X so that Y" are interpretive claims, not
counts, so they're written by Claude rather than derived.

Two ways that happens, same prompt and same schema for both:

  1. If ANTHROPIC_API_KEY is set, generate() calls the API directly and the
     summary comes back in one request.
  2. Otherwise (the default — this project ships with no such key), the app
     writes a request bundle to data/summary_requests/ and a Claude Code
     session picks it up via the /summarise skill, writing the answer to
     data/summaries/. The frontend polls for that file to appear.

Summaries live in data/summaries/, deliberately *outside* data/cache/: the
cache is gitignored and wiped by `make clean`, and these are expensive enough
(and stable enough — the text they describe never changes) to be worth keeping
and committing.
"""

import json
import os
import re
import time

SUMMARY_DIRNAME = "summaries"
REQUEST_DIRNAME = "summary_requests"

# Kept in step with the length caps in .claude/skills/summarise/SKILL.md — the
# panel is meant to be scannable in a few seconds, so the shape does the work
# of keeping it punchy rather than relying on the instruction alone.
SCHEMA_HINT = {
    "sections": "3-5 items, the argument's real movements: {title, start, end, line} where start/end are verse ids",
    "themes": "3-4 items: {theme, note, verses[] (verse ids), triggers[]}",
    "references": "up to 5 items: {ref, verses[] (verse ids), kind: quotation|parallel, why}",
    "purpose": "2-3 strings, each 'X wants us to know/believe ... so that/because ...'",
}


def summary_dir(data_dir):
    return os.path.join(data_dir, SUMMARY_DIRNAME)


def request_dir(data_dir):
    return os.path.join(data_dir, REQUEST_DIRNAME)


def summary_key(ref):
    """"EPH_1" for a whole chapter, "EPH_1_3-14" for a range within one,
    "JON_3-4" for a chapter range, "EPH_1:15-2:10" across a boundary. Keyed on
    the full range rather than the chapter, since a summary of Eph 1:3-14 is a
    genuinely different piece of work from one of the whole chapter.

    The single-chapter forms are unchanged from before multi-chapter support,
    so summaries written back then still resolve."""
    book, first, last = ref["book_id"], ref["chapter"], ref["chapter_end"]
    if first != last:
        if ref.get("verse_start"):
            return f"{book}_{first}:{ref['verse_start']}-{last}:{ref['verse_end']}"
        return f"{book}_{first}-{last}"
    key = f"{book}_{first}"
    if ref.get("verse_start"):
        key += f"_{ref['verse_start']}-{ref['verse_end']}"
    return key


def _summary_path(data_dir, ref):
    return os.path.join(summary_dir(data_dir), f"{summary_key(ref)}.json")


def _request_path(data_dir, ref):
    return os.path.join(request_dir(data_dir), f"{summary_key(ref)}.json")


def load(data_dir, ref):
    """The stored summary for this exact reference, or None."""
    path = _summary_path(data_dir, ref)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        # a half-written or hand-mangled summary file shouldn't take the panel
        # down — treat it as absent so the Run button comes back
        return None


def is_pending(data_dir, ref):
    """True when a request has been written but no summary has landed yet."""
    return os.path.exists(_request_path(data_dir, ref))


def build_bundle(ref, payload):
    """Everything Claude needs to write the summary, and nothing else.

    Passage text is BSB, not ESV, on purpose: this bundle gets written to disk,
    and fetch_esv() in server.py deliberately never persists ESV text (the ESV
    API terms cap local storage at 500 verses). crossref_preview() reaches for
    BSB for the same reason. The *derived* ESV analysis — discourse markers,
    Crossway's citation apparatus — is fine to include: that's analysis output,
    not the text itself.
    """
    cross_references = []
    order = {vid: i for i, vid in enumerate(payload["verse_ids"])}
    for verse, entries in sorted(payload["cross_references"].items(), key=lambda kv: order.get(kv[0], 0)):
        for entry in entries:
            cross_references.append({
                "verse": verse,
                "refs": entry["refs"],
                "title": entry["title"],
                "kind": "quotation" if entry["is_quotation"] else "parallel",
            })

    # sentence-initial markers only: that's the same signal computeSections()
    # in app.js uses to break the passage up, so the bundle shows Claude the
    # same structural skeleton the reader sees in the minimap
    openers = [
        {"verse": m["verse"], "marker": m["marker"], "category": m["category"]}
        for m in payload["discourse_markers"] if m["sentence_initial"]
    ]

    sentiment_result = payload.get("sentiment")

    return {
        "reference": payload["reference"]["display"],
        "book": payload["reference"]["book_name"],
        "spans_chapters": payload["reference"]["chapter_end"] != payload["reference"]["chapter"],
        "instructions": (
            "Write a short, punchy study summary of this passage. Ground every claim in the "
            "verses and metadata below; do not introduce cross-references that are not listed. "
            "See .claude/skills/summarise/SKILL.md for the full brief and length caps."
        ),
        "schema": SCHEMA_HINT,
        "verses": payload["translations"]["BSB"],
        "verse_ids": payload["verse_ids"],
        "verses_translation": "BSB",
        "publisher_section_headings": payload["section_headings"],
        "repeated_terms": payload["terms"],
        "discourse_openers": openers,
        "cross_references": cross_references,
        "glossary_hits": [
            {"term": g["term"], "gloss": g["gloss"], "verses": g["verses"]}
            for g in payload["glossary"]
        ],
        "tone": sentiment_result["overall"]["label"] if sentiment_result else None,
    }


def write_request(data_dir, ref, payload):
    """Write the request bundle for a Claude Code session to pick up.
    Returns the path, relative to the project root, for the UI to name."""
    path = _request_path(data_dir, ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(build_bundle(ref, payload), f, indent=2)
    return os.path.relpath(path, os.path.dirname(data_dir))


def save(data_dir, ref, result):
    path = _summary_path(data_dir, ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(result, f, indent=2)


def clear_request(data_dir, ref):
    path = _request_path(data_dir, ref)
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# Optional direct-API path
# ---------------------------------------------------------------------------

def api_key():
    return os.environ.get("ANTHROPIC_API_KEY")


def generate(ref, payload):
    """Generate the summary in-process via the Anthropic API. Only called when
    api_key() is set; without it the app falls back to the file handoff. Same
    bundle and same schema as the /summarise skill, so a summary generated
    either way is interchangeable on disk.

    Raises if the SDK isn't installed or the call fails — the caller falls back
    to the request-file path rather than failing the request.
    """
    import anthropic  # imported lazily: an optional path, not a hard dependency

    bundle = build_bundle(ref, payload)
    skill_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude", "skills", "summarise", "SKILL.md")
    with open(skill_path) as f:
        brief = f.read()

    client = anthropic.Anthropic(api_key=api_key())
    message = client.messages.create(
        model="claude-opus-5",
        max_tokens=2000,
        system=brief,
        messages=[{
            "role": "user",
            "content": (
                "Write the summary for this request bundle. Reply with the summary JSON "
                "object only — no prose, no code fence.\n\n" + json.dumps(bundle, indent=2)
            ),
        }],
    )

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    # be forgiving about a stray code fence even though we asked for none
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    result = json.loads(text)
    result.setdefault("reference", bundle["reference"])
    result["generated_at"] = time.time()
    return result
