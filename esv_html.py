"""Parse the ESV API's HTML passage format into plain per-verse text plus
phrase-level citation/quotation and words-of-Christ annotations.

Crossway's official ESV Study Bible cross-reference apparatus (requested via
include-crossrefs=true) attaches a <sup><a class="cf" href="..." title="...">
marker to the exact phrase it annotates. When that phrase is an actual
quotation of another passage, the title is prefixed "Cited from ..." — that's
the signal used to distinguish quotations from plain thematic references.

The HTML also carries the passage's own shape, which is worth keeping: <p>
marks Crossway's paragraphing (the author's thought units, and a better
answer to "where are the sections" than any marker heuristic), <p
class="block-indent"> marks poetry, and <br> marks a poetry line break. Each
verse therefore reports which paragraph it belongs to, whether that paragraph
is poetry, and the offsets of any line breaks inside its own text.

A passage can span several chapters, so a verse is identified here by a
(chapter, verse) pair rather than a bare number — verse numbers restart at
each chapter boundary. The ESV HTML marks a new chapter with
<b class="chapter-num">2:1</b>; plain verses inside it get
<b class="verse-num">5</b> and inherit the current chapter. A passage that
starts mid-chapter (e.g. "Ephesians 1:20-2:3") opens on a verse-num with no
chapter-num before it, so parse() must be told which chapter it starts in.
"""

import re
from html.parser import HTMLParser


def _classes(attrs):
    for name, value in attrs:
        if name == "class":
            return (value or "").split()
    return []


def _attr(attrs, name):
    for n, value in attrs:
        if n == name:
            return value
    return None


class _EsvHtmlParser(HTMLParser):
    def __init__(self, start_chapter):
        super().__init__(convert_charrefs=True)
        self.current_chapter = start_chapter
        self.current_verse = None  # (chapter, verse) key, or None before the first verse
        self.verse_order = []
        self.texts = {}  # (chapter, verse) -> accumulated, already-normalized text

        # Passage shape. paragraph_index counts <p> tags that actually contain
        # a verse; pending_paragraph holds one that has opened but not yet
        # reached its first verse (Crossway emits some empty paragraphs).
        self.paragraph_index = -1
        self.pending_paragraph = None  # {"poetry": bool} once a <p> has opened
        self.verse_paragraph = {}  # (chapter, verse) -> paragraph index
        self.poetry_paragraphs = set()
        self.breaks = {}  # (chapter, verse) -> [{"offset", "indent"}, ...]
        # a <br> sets this; the <span class="line"> that follows supplies the
        # indent level, since the break's own tag carries no depth
        self.pending_break = None

        self.in_verse_num = False
        self.verse_num_buffer = ""

        self.skip_stack = []  # tag names currently suppressing text capture (sup, copyright <a>)
        self.span_stack = []  # "woc" | "other", mirrors nesting of <span> tags
        self.woc_open = []  # (verse, start_offset, pending_citations) for currently-open woc spans

        self.p_stack = []  # (kind, verse_at_open, len_at_open) for currently-open <p> tags
        self.pending_block_citations = []  # citations recorded inside the currently-open block-indent <p>

        # Crossway occasionally splits one multi-clause quotation across two
        # <p class="block-indent"> blocks with a one-word connector paragraph
        # ("and") in between. These hold a block-indent group's citations and
        # end position across such a gap until we're sure the quotation has
        # actually moved on (see handle_endtag's "p" branch).
        self.deferred_block_citations = []
        self.deferred_block_end = None

        self.citations = []
        self.woc_spans = []

    def _append(self, verse, s):
        if not s or verse is None:
            return
        s = s.replace("\xa0", " ")
        s = re.sub(r"\s+", " ", s)
        current = self.texts.get(verse, "")
        if current == "":
            s = s.lstrip(" ")
            if not s:
                return
        elif current.endswith(" ") and s.startswith(" "):
            s = s.lstrip(" ")
        self.texts[verse] = current + s

    def _set_verse(self, chapter, verse):
        key = (chapter, verse)
        self.current_verse = key
        if key not in self.texts:
            self.verse_order.append(key)
            self.texts[key] = ""
        if key not in self.verse_paragraph:
            if self.pending_paragraph is not None:
                self.paragraph_index += 1
                if self.pending_paragraph["poetry"]:
                    self.poetry_paragraphs.add(self.paragraph_index)
                self.pending_paragraph = None
            self.verse_paragraph[key] = max(self.paragraph_index, 0)

    def handle_starttag(self, tag, attrs):
        classes = _classes(attrs)

        if tag == "b" and ("verse-num" in classes or "chapter-num" in classes):
            self.in_verse_num = True
            self.verse_num_buffer = ""
            return

        if tag == "sup":
            self.skip_stack.append(tag)
            return

        if tag == "a" and "cf" in classes:
            href = (_attr(attrs, "href") or "").rstrip("/")
            refs = [r.strip() for r in href.split(";") if r.strip()]
            title = _attr(attrs, "title") or ""
            if self.current_verse is not None:
                citation = {
                    "verse": self.current_verse,
                    "offset": len(self.texts.get(self.current_verse, "")),
                    "refs": refs,
                    "title": title,
                    "is_quotation": "Cited from" in title,
                    "is_woc": bool(self.woc_open),
                    "block_end": None,
                    "woc_end": None,
                }
                self.citations.append(citation)
                if self.p_stack and self.p_stack[-1][0] == "block-indent":
                    self.pending_block_citations.append(citation)
                if self.woc_open:
                    self.woc_open[-1][2].append(citation)
            return

        if tag == "a" and "copyright" in classes:
            self.skip_stack.append(tag)
            return

        if tag == "span":
            if "line" in classes and self.pending_break is not None:
                verse, offset = self.pending_break
                self.breaks.setdefault(verse, []).append(
                    {"offset": offset, "indent": 1 if "indent" in classes else 0}
                )
                self.pending_break = None
            if "woc" in classes:
                self.span_stack.append("woc")
                offset = len(self.texts.get(self.current_verse, "")) if self.current_verse is not None else 0
                self.woc_open.append((self.current_verse, offset, []))
            else:
                self.span_stack.append("other")
            return

        if tag == "p":
            kind = "block-indent" if "block-indent" in classes else "other"
            # a paragraph only counts once a verse lands in it — see _set_verse
            self.pending_paragraph = {"poetry": kind == "block-indent"}
            verse_at_open = self.current_verse
            len_at_open = len(self.texts.get(verse_at_open, "")) if verse_at_open is not None else 0
            self.p_stack.append((kind, verse_at_open, len_at_open))
            return

        if tag == "br":
            # a poetry line break. The space is still appended exactly as before
            # so that every other annotation's character offsets stay valid;
            # the offset of that space is recorded so the frontend can render a
            # line break there instead. A break with no text before it is a
            # break *between* verses, which the reading grid's rows already
            # express, so it is not recorded.
            verse = self.current_verse
            if verse is not None:
                current = self.texts.get(verse, "")
                if current and not current.endswith(" "):
                    self.pending_break = (verse, len(current))
            self._append(verse, " ")
            return

    def handle_endtag(self, tag):
        if tag == "b" and self.in_verse_num:
            # the opening verse of a chapter is marked class="chapter-num" with
            # text "2:1" (chapter:verse) instead of plain "1" — so a colon here
            # is how a chapter boundary announces itself mid-passage. Verses
            # without one inherit whatever chapter is current.
            buffer = self.verse_num_buffer
            if ":" in buffer:
                chapter_text, verse_text = buffer.rsplit(":", 1)
                chapter_digits = re.sub(r"\D", "", chapter_text)
                if chapter_digits:
                    self.current_chapter = int(chapter_digits)
            else:
                verse_text = buffer
            digits = re.sub(r"\D", "", verse_text)
            if digits and self.current_chapter is not None:
                self._set_verse(self.current_chapter, int(digits))
            self.in_verse_num = False
            return

        if self.skip_stack and self.skip_stack[-1] == tag:
            self.skip_stack.pop()
            return

        if tag == "span" and self.span_stack:
            kind = self.span_stack.pop()
            if kind == "woc" and self.woc_open:
                verse, start, pending_citations = self.woc_open.pop()
                end = len(self.texts.get(verse, "")) if verse is not None else start
                for citation in pending_citations:
                    citation["woc_end"] = {"verse": verse, "offset": end}
                if verse is not None and end > start:
                    self.woc_spans.append({"verse": verse, "start": start, "end": end})
            return

        if tag == "p" and self.p_stack:
            kind, verse_at_open, len_at_open = self.p_stack.pop()
            end_verse = self.current_verse
            end_len = len(self.texts.get(end_verse, "")) if end_verse is not None else 0
            added_len = (end_len - len_at_open) if verse_at_open == end_verse else 999

            if kind == "block-indent":
                # fold this block's citations + end position into the deferred
                # group, which may already be carrying citations forward from
                # an earlier block-indent block separated only by a trivial
                # connector paragraph (see the "other" branch below)
                self.deferred_block_citations.extend(self.pending_block_citations)
                self.pending_block_citations = []
                self.deferred_block_end = {"verse": end_verse, "offset": end_len}
            elif self.deferred_block_citations and added_len > 15:
                # this paragraph has real content of its own, so the deferred
                # quotation has definitely ended — lock in its end position
                for citation in self.deferred_block_citations:
                    citation["block_end"] = self.deferred_block_end
                self.deferred_block_citations = []
                self.deferred_block_end = None
            return

    def handle_data(self, data):
        if self.in_verse_num:
            self.verse_num_buffer += data
            return
        if self.skip_stack:
            return
        self._append(self.current_verse, data)


def _split(key):
    """Internal (chapter, verse) tuple -> the public pair of ints."""
    return {"chapter": key[0], "verse": key[1]}


def _public_citation(citation):
    out = {**citation, **_split(citation["verse"])}
    for boundary in ("block_end", "woc_end"):
        edge = citation[boundary]
        out[boundary] = {**_split(edge["verse"]), "offset": edge["offset"]} if edge else None
    return out


def parse(html, start_chapter):
    """start_chapter is the chapter the passage opens in. Required because a
    passage beginning mid-chapter gets a plain verse-num for its first verse,
    with no chapter-num to announce the chapter (see the module docstring)."""
    parser = _EsvHtmlParser(start_chapter)
    parser.feed(html)
    parser.close()

    if parser.deferred_block_citations:
        for citation in parser.deferred_block_citations:
            citation["block_end"] = parser.deferred_block_end

    verses = []
    for key in parser.verse_order:
        text = parser.texts.get(key, "")
        text = re.sub(r"\(\s*\)\s*$", "", text).rstrip()
        paragraph = parser.verse_paragraph.get(key, 0)
        verses.append({
            "chapter": key[0],
            "number": key[1],
            "text": text,
            "paragraph": paragraph,
            "poetry": paragraph in parser.poetry_paragraphs,
            # the rstrip above can drop a trailing break's space; a break at
            # the very end of a verse is a break *between* verses, which the
            # reading grid's rows already express
            "breaks": [b for b in parser.breaks.get(key, []) if b["offset"] < len(text)],
        })

    citations = [
        _public_citation(c)
        for c in sorted(parser.citations, key=lambda c: (c["verse"], c["offset"]))
    ]
    woc_spans = [
        {**_split(w["verse"]), "start": w["start"], "end": w["end"]}
        for w in sorted(parser.woc_spans, key=lambda w: (w["verse"], w["start"]))
    ]
    return {"verses": verses, "citations": citations, "woc_spans": woc_spans}


def quotation_spans(verses, citations):
    """Expand is_quotation citations into per-verse {chapter, verse, start, end,
    refs, title} ranges for inline highlighting. The end boundary comes from,
    in priority order:
      1. block_end — the end of the containing <p class="block-indent">
         paragraph, i.e. how Crossway typesets a poetry-formatted OT
         quotation, regardless of how many verses it spans.
      2. woc_end — the end of the enclosing <span class="woc"> (words of
         Christ), which correctly bounds short quotations embedded inline in
         narrative prose (e.g. Matt 4:7's "Cited from Deut. 6:16") without
         over-running into unrelated later verses.
      3. End of the citation's own verse, as a conservative fallback.
    Deliberately does NOT fall back to "the next quotation's start" — that
    over-extends short inline quotes across any plain narrative text (and
    verses) sitting between them and whatever the next quotation happens to
    be, however far away.

    Positions are resolved by index into the passage's own verse order, so a
    quotation that runs across a chapter boundary spans it correctly without
    any verse-number arithmetic."""
    verse_order = [(v["chapter"], v["number"]) for v in verses]
    verse_index = {key: i for i, key in enumerate(verse_order)}
    verse_len = {key: len(v["text"]) for key, v in zip(verse_order, verses)}

    quotations = [c for c in citations if c["is_quotation"]]
    spans = []
    for q in quotations:
        start_key, start_offset = (q["chapter"], q["verse"]), q["offset"]
        if q["block_end"]:
            end_key = (q["block_end"]["chapter"], q["block_end"]["verse"])
            end_offset = q["block_end"]["offset"]
        elif q["woc_end"]:
            end_key = (q["woc_end"]["chapter"], q["woc_end"]["verse"])
            end_offset = q["woc_end"]["offset"]
        else:
            end_key = start_key
            end_offset = verse_len.get(start_key, start_offset)

        if start_key not in verse_index or end_key not in verse_index:
            continue
        start_idx, end_idx = verse_index[start_key], verse_index[end_key]
        if end_idx < start_idx:
            continue
        for idx in range(start_idx, end_idx + 1):
            key = verse_order[idx]
            length = verse_len.get(key, 0)
            v_start = start_offset if key == start_key else 0
            # clamp: offsets are recorded while parsing, but parse() then strips
            # each verse's trailing whitespace, so a block_end captured at the
            # close of a <p> can land one character past the final length
            v_end = min(end_offset if key == end_key else length, length)
            if v_end > v_start:
                spans.append({
                    "chapter": key[0], "verse": key[1],
                    "start": v_start, "end": v_end,
                    "refs": q["refs"], "title": q["title"],
                })
    return spans
