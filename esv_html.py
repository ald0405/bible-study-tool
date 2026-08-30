"""Parse the ESV API's HTML passage format into plain per-verse text plus
phrase-level citation/quotation and words-of-Christ annotations.

Crossway's official ESV Study Bible cross-reference apparatus (requested via
include-crossrefs=true) attaches a <sup><a class="cf" href="..." title="...">
marker to the exact phrase it annotates. When that phrase is an actual
quotation of another passage, the title is prefixed "Cited from ..." — that's
the signal used to distinguish quotations from plain thematic references.
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
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current_verse = None
        self.verse_order = []
        self.texts = {}  # verse -> accumulated, already-normalized text

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

    def _set_verse(self, number):
        self.current_verse = number
        if number not in self.texts:
            self.verse_order.append(number)
            self.texts[number] = ""

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
            if "woc" in classes:
                self.span_stack.append("woc")
                offset = len(self.texts.get(self.current_verse, "")) if self.current_verse is not None else 0
                self.woc_open.append((self.current_verse, offset, []))
            else:
                self.span_stack.append("other")
            return

        if tag == "p":
            kind = "block-indent" if "block-indent" in classes else "other"
            verse_at_open = self.current_verse
            len_at_open = len(self.texts.get(verse_at_open, "")) if verse_at_open is not None else 0
            self.p_stack.append((kind, verse_at_open, len_at_open))
            return

        if tag == "br":
            self._append(self.current_verse, " ")
            return

    def handle_endtag(self, tag):
        if tag == "b" and self.in_verse_num:
            # the opening verse of a chapter is marked class="chapter-num"
            # with text "1:1" (chapter:verse) instead of plain "1" — take
            # whatever follows the last colon before stripping non-digits
            verse_text = self.verse_num_buffer.rsplit(":", 1)[-1]
            digits = re.sub(r"\D", "", verse_text)
            if digits:
                self._set_verse(int(digits))
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


def parse(html):
    parser = _EsvHtmlParser()
    parser.feed(html)
    parser.close()

    if parser.deferred_block_citations:
        for citation in parser.deferred_block_citations:
            citation["block_end"] = parser.deferred_block_end

    verses = []
    for number in parser.verse_order:
        text = parser.texts.get(number, "")
        text = re.sub(r"\(\s*\)\s*$", "", text).rstrip()
        verses.append({"number": number, "text": text})

    citations = sorted(parser.citations, key=lambda c: (c["verse"], c["offset"]))
    woc_spans = sorted(parser.woc_spans, key=lambda w: (w["verse"], w["start"]))
    return {"verses": verses, "citations": citations, "woc_spans": woc_spans}


def quotation_spans(verses, citations):
    """Expand is_quotation citations into per-verse {verse, start, end, refs,
    title} ranges for inline highlighting. The end boundary comes from,
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
    be, however far away."""
    verse_order = [v["number"] for v in verses]
    verse_index = {n: i for i, n in enumerate(verse_order)}
    verse_len = {v["number"]: len(v["text"]) for v in verses}

    quotations = [c for c in citations if c["is_quotation"]]
    spans = []
    for q in quotations:
        start_verse, start_offset = q["verse"], q["offset"]
        if q["block_end"]:
            end_verse, end_offset = q["block_end"]["verse"], q["block_end"]["offset"]
        elif q["woc_end"]:
            end_verse, end_offset = q["woc_end"]["verse"], q["woc_end"]["offset"]
        else:
            end_verse = start_verse
            end_offset = verse_len.get(start_verse, start_offset)

        if start_verse not in verse_index or end_verse not in verse_index:
            continue
        start_idx, end_idx = verse_index[start_verse], verse_index[end_verse]
        if end_idx < start_idx:
            continue
        for idx in range(start_idx, end_idx + 1):
            v = verse_order[idx]
            v_start = start_offset if v == start_verse else 0
            v_end = end_offset if v == end_verse else verse_len.get(v, 0)
            if v_end > v_start:
                spans.append({"verse": v, "start": v_start, "end": v_end, "refs": q["refs"], "title": q["title"]})
    return spans
