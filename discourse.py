"""Detect ESV discourse markers (Behold, Therefore, But God, ...) in a passage.

The ESV's "essentially literal" translation philosophy preserves discourse
markers from the underlying Hebrew/Greek that dynamic-equivalence
translations often smooth away — so these are worth surfacing explicitly.
See data/discourse_markers.json for the curated bank and categories.
"""

import json
import os
import re

_ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_ROOT, "data", "discourse_markers.json")) as _f:
    _MARKERS = json.load(_f)

# longest marker first, so a multi-word phrase (e.g. "but god") claims its
# span before the shorter marker it contains (e.g. "but") is considered
_MARKERS_BY_LENGTH = sorted(_MARKERS, key=lambda m: -len(m["marker"]))

_QUOTE_CHARS = "\"'‘’“”"


def _is_sentence_initial(text, start):
    prefix = text[:start].rstrip().rstrip(_QUOTE_CHARS).rstrip()
    return prefix == "" or prefix[-1] in ".;!?"


def find_markers(verses):
    """verses: list of {'number': int, 'text': str} (ESV). Returns hits sorted
    by verse then position, each with character offsets into that verse's text
    so the frontend can highlight the exact span without re-matching."""
    hits = []
    for v in verses:
        text = v["text"]
        claimed = []
        for marker_def in _MARKERS_BY_LENGTH:
            pattern = re.compile(r"\b" + re.escape(marker_def["marker"]) + r"\b", re.IGNORECASE)
            for match in pattern.finditer(text):
                start, end = match.start(), match.end()
                if any(start < c_end and end > c_start for c_start, c_end in claimed):
                    continue
                if marker_def["position"] == "sentence-initial" and not _is_sentence_initial(text, start):
                    continue
                claimed.append((start, end))
                hits.append({
                    "marker": text[start:end],
                    "category": marker_def["category"],
                    "verse": v["number"],
                    "start": start,
                    "end": end,
                })
    hits.sort(key=lambda h: (h["verse"], h["start"]))
    return hits
