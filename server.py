"""Local Bible study tool server.

Serves the static frontend and a small JSON API that, for one passage at a
time, merges:
  - ESV text, fetched live from api.esv.org (never written to disk — see
    fetch_esv() for why), including Crossway's own phrase-level cross-
    reference/citation apparatus and words-of-Christ markup (see esv_html.py)
  - NIV text, fetched from api.bible and disk-cached with a 30-day staleness
    check (see niv.py)
  - BSB / NET / FBV text, fetched from bible.helloao.org and disk-cached
    (public domain, no storage restriction)
  - a curated key-terms glossary
  - NLTK-based repeated word/phrase analysis
  - an on-demand, Claude-written summary of all of the above (see summary.py)

Run with `make run` (after `make setup`).
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

import discourse
import esv_html
import niv
import sentiment
import summary
import termanalysis
from booknames import REVERSE_ALIASES

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
STATIC_DIR = os.path.join(ROOT, "static")
PORT = 8765

HELLOAO_BASE = "https://bible.helloao.org/api"
TRANSLATIONS = {"BSB": "BSB", "NET": "eng_net", "FBV": "eng_fbv"}

ESV_CACHE_MAX_VERSES = 400  # stays well under the 500-verse API storage cap


with open(os.path.join(DATA_DIR, "books.json")) as f:
    BOOKS = json.load(f)
BOOKS_BY_ID = {b["id"]: b for b in BOOKS}

with open(os.path.join(DATA_DIR, "glossary.json")) as f:
    GLOSSARY = json.load(f)

load_dotenv(os.path.join(ROOT, ".env"))

REQUIRED_ENV_VARS = ["ESV_API_TOKEN", "ESV_API_BASE", "API_BIBLE_KEY", "API_BIBLE_NIV_ID"]
missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
if missing:
    print(
        f"[!] Missing from .env: {', '.join(missing)}\n"
        "    Copy .env.example to .env and fill in your API keys:\n"
        "      ESV_API_TOKEN / ESV_API_BASE  from https://api.esv.org/account/\n"
        "      API_BIBLE_KEY / API_BIBLE_NIV_ID  from https://scripture.api.bible/"
    )
    sys.exit(1)

CONFIG = {
    "esv_api_token": os.environ["ESV_API_TOKEN"],
    "esv_api_base": os.environ["ESV_API_BASE"],
    "api_bible_key": os.environ["API_BIBLE_KEY"],
    "api_bible_niv_id": os.environ["API_BIBLE_NIV_ID"],
}


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------

# Book, a first number, then optionally ":verse", then optionally a range end
# which may itself carry its own "chapter:". That covers every supported form:
#   Ephesians 1        Eph 1:3-14      John 3:16
#   Jonah 3-4          Eph 1:15-2:10   Jude 3-5
REF_RE = re.compile(
    r"^\s*([1-3])?\s*([A-Za-z][A-Za-z\s\.]*?)\.?\s*(\d+)"      # book + first number
    r"(?:\s*:\s*(\d+))?"                                          # optional :verse
    r"(?:\s*-\s*(?:(\d+)\s*:\s*)?(\d+))?"                        # optional - [chapter:]verse
    r"\s*$"
)

# A passage may span at most this many chapters. Beyond it a cold request means
# four API round trips per chapter, and a single chapter can be large on its own
# (Psalm 119 is 176 verses).
MAX_CHAPTERS = 5

# How many verses of surrounding context to pull in when jumping to a
# cross-reference or quotation target, so you land in the passage rather
# than on one verse in isolation. Still clamped within the target's own
# chapter: padding across a boundary would need the previous chapter's last
# verse number, i.e. an extra fetch purely to decide how far back to pad.
CONTEXT_PAD = 4


def parse_reference(raw):
    """Returns a ref spanning (chapter, verse_start) to (chapter_end, verse_end),
    where either verse bound may be None meaning "the whole of that chapter".

    The ambiguity worth knowing about is a bare dash with no colon anywhere:
    "Jonah 3-4" means chapters, not verses. The exception is the five
    single-chapter books (Obadiah, Philemon, 2-3 John, Jude), where "Jude 3-5"
    can only mean verses — there is no chapter 5 to range to.
    """
    # ESV's own cross-reference hrefs use an en dash for ranges (e.g. "Joel 2:28–32")
    normalized = (raw or "").replace("–", "-").replace("—", "-")
    m = REF_RE.match(normalized)
    if not m:
        return None
    prefix, book_word, first, vstart, end_chapter, end_number = m.groups()
    key = (prefix or "") + re.sub(r"[\s\.]", "", book_word).lower()
    book_id = REVERSE_ALIASES.get(key)
    if not book_id:
        return None
    book = BOOKS_BY_ID[book_id]

    first = int(first)
    vstart = int(vstart) if vstart else None
    end_chapter = int(end_chapter) if end_chapter else None
    end_number = int(end_number) if end_number else None

    if vstart is None and end_number is not None and end_chapter is None:
        # "Jonah 3-4" — no colon anywhere, so the numbers are chapters, unless
        # the book only has one chapter and they can only be verses
        if book["chapters"] == 1:
            chapter, chapter_end = 1, 1
            verse_start, verse_end = first, end_number
        else:
            chapter, chapter_end = first, end_number
            verse_start = verse_end = None
    else:
        chapter = first
        verse_start = vstart
        chapter_end = end_chapter if end_chapter is not None else first
        verse_end = end_number if end_number is not None else vstart

    for c in (chapter, chapter_end):
        if c < 1 or c > book["chapters"]:
            return None
    if chapter_end < chapter:
        return None
    if chapter_end == chapter and verse_start and verse_end and verse_end < verse_start:
        return None

    return {
        "book_id": book_id,
        "book_name": book["name"],
        "chapter": chapter,
        "chapter_end": chapter_end,
        "verse_start": verse_start,
        "verse_end": verse_end,
    }


def esv_query_string(ref):
    """The canonical display form, and the query sent to the ESV API (which
    accepts every form below, including cross-chapter ranges)."""
    q = f"{ref['book_name']} {ref['chapter']}"
    if ref["verse_start"]:
        q += f":{ref['verse_start']}"
    if ref["chapter_end"] != ref["chapter"]:
        tail = str(ref["chapter_end"])
        if ref["verse_end"]:
            tail += f":{ref['verse_end']}"
        return f"{q}-{tail}"
    if ref["verse_end"] and ref["verse_end"] != ref["verse_start"]:
        q += f"-{ref['verse_end']}"
    return q


def verse_id(chapter, verse):
    """The identity of a verse within a passage. Verse numbers restart at each
    chapter boundary, so a bare number cannot identify a verse in a
    multi-chapter passage; this string is used as the key in every per-verse
    map the API returns, and as the frontend's data-verse attribute."""
    return f"{chapter}:{verse}"


# ---------------------------------------------------------------------------
# ESV — live fetch only, in-memory cache, never persisted to disk
# ---------------------------------------------------------------------------

_esv_cache = {}  # key -> {"verses": [...], "citations": [...], "woc_spans": [...], "count": int}
_esv_cache_order = []
_esv_cache_total = 0


def _esv_cache_put(key, result):
    global _esv_cache_total
    count = len(result["verses"])
    if count > ESV_CACHE_MAX_VERSES:
        # A multi-chapter passage can exceed the cap on its own. Serving it live
        # is fine; retaining it is what the ESV terms limit, and without this
        # the eviction loop below would drain the cache and then store it anyway.
        return
    while _esv_cache_order and _esv_cache_total + count > ESV_CACHE_MAX_VERSES:
        oldest = _esv_cache_order.pop(0)
        _esv_cache_total -= _esv_cache.pop(oldest)["count"]
    _esv_cache[key] = {**result, "count": count}
    _esv_cache_order.append(key)
    _esv_cache_total += count


def fetch_esv(ref):
    """Fetch ESV text live from api.esv.org, including Crossway's own
    phrase-level cross-reference/citation apparatus and words-of-Christ
    markup (see esv_html.py for how that's parsed out of the HTML).

    Deliberately never written to disk: the ESV API terms cap local storage
    at 500 verses / half a book. We keep an in-memory-only cache, evicted to
    stay comfortably under that cap, and it disappears when the process exits.
    """
    key = (ref["book_id"], ref["chapter"], ref["chapter_end"], ref["verse_start"], ref["verse_end"])
    if key in _esv_cache:
        cached = _esv_cache[key]
        return {"verses": cached["verses"], "citations": cached["citations"], "woc_spans": cached["woc_spans"]}

    params = {
        "q": esv_query_string(ref),
        "include-crossrefs": "true",
        "include-footnotes": "false",
        "include-headings": "false",
        "include-passage-references": "false",
        "include-audio-link": "false",
        "include-verse-numbers": "true",
        "include-first-verse-numbers": "true",
    }
    url = f"{CONFIG['esv_api_base']}/passage/html/?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Token {CONFIG['esv_api_token']}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    passages = data.get("passages", [])
    # the parser needs the opening chapter: a passage starting mid-chapter gets
    # a plain verse-num for its first verse, with no chapter-num to infer from
    result = (
        esv_html.parse(passages[0], ref["chapter"]) if passages
        else {"verses": [], "citations": [], "woc_spans": []}
    )
    _esv_cache_put(key, result)
    return result


# ---------------------------------------------------------------------------
# BSB / NET / FBV — helloao.org, public domain, disk-cached
# ---------------------------------------------------------------------------

# Bumped when the cached shape changes, so old files are refetched rather than
# silently serving a payload missing newer fields (poetry breaks, at v2).
HELLOAO_CACHE_FORMAT = 2


def fetch_helloao_chapter(translation_code, book_id, chapter):
    translation = TRANSLATIONS[translation_code]
    cache_path = os.path.join(CACHE_DIR, translation, f"{book_id}_{chapter}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get("format") == HELLOAO_CACHE_FORMAT:
            return cached

    url = f"{HELLOAO_BASE}/{translation}/{book_id}/{chapter}.json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    verses = []
    for item in data["chapter"]["content"]:
        if item.get("type") == "verse":
            # content parts are either plain strings, poetry lines
            # ({"text": ..., "poem": N}), or non-text markers (footnote
            # callouts {"noteId": N}, {"lineBreak": ...}). Join the textual
            # parts with a space, cleaning up doubled/pre-punctuation spaces,
            # and record the offset of each separator that begins a poetry
            # line so the frontend can break there — the same contract
            # esv_html.parse uses, so the columns line up as poetry together
            # rather than one column of verse beside four of prose.
            pieces = []  # (text, poem level or None) — "poem": 1 is the base
            for part in item["content"]:
                if isinstance(part, str):
                    pieces.append((part, None))
                elif isinstance(part, dict) and "text" in part:
                    pieces.append((part["text"], part.get("poem")))

            text = ""
            breaks = []
            for raw, poem_level in pieces:
                piece = re.sub(r"\s+", " ", raw).strip()
                piece = re.sub(r"\s+([,.;:!?])", r"\1", piece)
                if not piece:
                    continue
                if text:
                    if poem_level is not None:
                        breaks.append({"offset": len(text), "indent": max(0, poem_level - 1)})
                        text += " "
                    elif piece[0] not in ",.;:!?":
                        text += " "
                text += piece

            textual = [p for p in pieces if p[0].strip()]
            verses.append({
                "number": item["number"],
                "text": text,
                "breaks": breaks,
                # wholly poetic, rather than a prose line introducing a quotation
                "poetry": bool(textual) and all(level is not None for _, level in textual),
            })

    footnotes = [
        {"verse": fn["reference"]["verse"], "text": fn["text"]}
        for fn in data["chapter"].get("footnotes", [])
    ]

    result = {"format": HELLOAO_CACHE_FORMAT, "verses": verses, "footnotes": footnotes}
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(result, f)
    return result


def slice_chapter(items, ref, chapter, key="number"):
    """Clip one chapter's worth of items to the requested passage. Only the
    first and last chapter of a span are clipped — everything in between is
    taken whole."""
    lower = ref["verse_start"] if chapter == ref["chapter"] else None
    upper = ref["verse_end"] if chapter == ref["chapter_end"] else None
    if lower:
        items = [x for x in items if x[key] >= lower]
    if upper:
        items = [x for x in items if x[key] <= upper]
    return items


def _parse_verse_org_id(verse_id):
    """"EPH.2.10" -> (2, 10). api.bible's Section objects give chapter/verse
    this way rather than as separate fields. The tuple sorts in reading order,
    which is what the overlap checks above rely on."""
    _, chapter, verse = verse_id.split(".")
    return int(chapter), int(verse)


def relevant_section_headings(sections, passage_start, passage_end):
    """Editorial section headings (from niv.fetch_sections) overlapping the
    requested passage, clipped to it. passage_start/passage_end are
    (chapter, verse) tuples.

    This used to additionally clip each heading to a single chapter, because
    that was the largest passage the app could hold — a heading spanning a
    boundary (e.g. Eph 4:17-5:20) could only ever be shown in part. Now that a
    passage can span chapters, such a heading is shown whole, which is rather
    the point."""
    result = []
    for section in sections:
        first = _parse_verse_org_id(section["first_verse"])
        last = _parse_verse_org_id(section["last_verse"])
        if last < passage_start or first > passage_end:
            continue
        display_start = max(first, passage_start)
        display_end = min(last, passage_end)
        result.append({
            "title": section["title"],
            "start": verse_id(*display_start),
            "end": verse_id(*display_end),
        })
    return result


# books.json carries canonical order rather than a testament field; Matthew is 40
OT_NT_BOUNDARY_ORDER = 40


def glossary_for_book(book_id):
    """Hebrew glossary entries for Old Testament books, Greek for New.

    The glossary matches English trigger words ("god" -> Elohim, "peace" ->
    Shalom), which says nothing about the language a book was written in —
    unfiltered it claimed Shalom, Ruach, Elohim and Kabod all appear in
    Ephesians, a Greek epistle. A study tool stating that confidently costs
    more trust than the panel earns."""
    is_new_testament = BOOKS_BY_ID[book_id]["order"] >= OT_NT_BOUNDARY_ORDER
    language = "Greek" if is_new_testament else "Hebrew"
    return [entry for entry in GLOSSARY if entry["language"] == language]


def crossref_preview(target_display, max_len=110):
    """Short BSB preview snippet for a cross-reference target like 'Philippians
    2:9-11', so you can see what the connection actually is without navigating
    away. BSB (not ESV) deliberately, since it's free to fetch/cache and this
    is just an at-a-glance preview, not the passage you're studying."""
    parsed = parse_reference(target_display)
    if not parsed:
        return None
    try:
        chapter_data = fetch_helloao_chapter("BSB", parsed["book_id"], parsed["chapter"])
    except Exception:  # noqa: BLE001 — a preview is a nice-to-have, never fatal
        return None
    verse_start = parsed["verse_start"] or 1
    verse = next((v for v in chapter_data["verses"] if v["number"] == verse_start), None)
    if not verse:
        return None
    text = verse["text"]
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


# ---------------------------------------------------------------------------
# Request orchestration
# ---------------------------------------------------------------------------

def build_passage_response(ref):
    chapters = range(ref["chapter"], ref["chapter_end"] + 1)

    esv_result = fetch_esv(ref)
    esv_verses = esv_result["verses"]

    # a padded/context request can ask for a verse_end past the chapter's
    # actual last verse (e.g. context around a verse near the end); the ESV
    # API just quietly stops at the real last verse, so clamp here too or
    # the displayed title would claim verses that were never fetched
    if ref["verse_end"] and esv_verses and esv_verses[-1]["chapter"] == ref["chapter_end"]:
        ref["verse_end"] = min(ref["verse_end"], esv_verses[-1]["number"])

    # Every verse carries its own id from here on, so the analysis modules and
    # the frontend never re-derive the format or do arithmetic on verse numbers.
    for v in esv_verses:
        v["id"] = verse_id(v["chapter"], v["number"])

    translations = {"ESV": esv_verses}
    all_footnotes = []
    fums_token = None

    # The other four translations are fetched a chapter at a time — their disk
    # caches are keyed per chapter and stay that way — then concatenated, with
    # each verse tagged by the chapter it came from.
    niv_verses = []
    for chapter in chapters:
        niv_result = niv.fetch_chapter(CONFIG, CACHE_DIR, ref["book_id"], chapter)
        niv_verses += [
            {**v, "chapter": chapter, "id": verse_id(chapter, v["number"])}
            for v in slice_chapter(niv_result["verses"], ref, chapter)
        ]
        for fn in slice_chapter(niv_result["footnotes"], ref, chapter, key="verse"):
            all_footnotes.append({
                "translation": "NIV", "verse": verse_id(chapter, fn["verse"]), "text": fn["text"],
            })
        fums_token = fums_token or niv_result["fums_token"]
    translations["NIV"] = niv_verses

    for code in ("BSB", "NET", "FBV"):
        verses = []
        for chapter in chapters:
            chapter_data = fetch_helloao_chapter(code, ref["book_id"], chapter)
            verses += [
                {**v, "chapter": chapter, "id": verse_id(chapter, v["number"])}
                for v in slice_chapter(chapter_data["verses"], ref, chapter)
            ]
            for fn in slice_chapter(chapter_data["footnotes"], ref, chapter, key="verse"):
                all_footnotes.append({
                    "translation": code, "verse": verse_id(chapter, fn["verse"]), "text": fn["text"],
                })
        translations[code] = verses

    base_verses = esv_verses

    # The canonical reading order for this passage, as verse ids. Everything
    # positional downstream (grid rows, minimap proportions, section spans) is
    # index arithmetic over this list rather than arithmetic on verse numbers,
    # which restart at every chapter boundary.
    verse_ids = [v["id"] for v in esv_verses]

    # Crossway's own phrase-level cross-reference/citation apparatus,
    # grouped per verse for the sidebar panel.
    cross_references = {}
    for citation in esv_result["citations"]:
        entry = {
            "refs": citation["refs"],
            "title": citation["title"],
            "is_quotation": citation["is_quotation"],
            "preview": crossref_preview(citation["refs"][0]) if citation["refs"] else None,
        }
        key = verse_id(citation["chapter"], citation["verse"])
        cross_references.setdefault(key, []).append(entry)

    # Direct OT-in-NT (or NT-in-NT) quotations, expanded into per-verse
    # highlight spans — see esv_html.quotation_spans for how the block-indent
    # paragraph boundary is used to correctly span multi-verse quotations.
    ot_quotations = [
        {**q, "verse": verse_id(q["chapter"], q["verse"])}
        for q in esv_html.quotation_spans(esv_verses, esv_result["citations"])
    ]
    woc_spans = [
        {**w, "verse": verse_id(w["chapter"], w["verse"])}
        for w in esv_result["woc_spans"]
    ]

    glossary_hits = []
    for entry in glossary_for_book(ref["book_id"]):
        matched_verses = [
            v["id"] for v in base_verses
            if any(trigger in v["text"].lower() for trigger in entry["triggers"])
        ]
        if matched_verses:
            glossary_hits.append({**entry, "verses": matched_verses})

    terms = termanalysis.analyze(base_verses)
    discourse_markers = discourse.find_markers(esv_verses)
    sentiment_result = sentiment.analyze(base_verses)

    # editorial section headings (e.g. "Made Alive in Christ") from NIV's
    # publisher — always fetched regardless of which translation columns are
    # ticked on, since this is analysis data, not display text
    section_headings = []
    if esv_verses:
        passage_start = (esv_verses[0]["chapter"], esv_verses[0]["number"])
        passage_end = (esv_verses[-1]["chapter"], esv_verses[-1]["number"])
        all_sections = niv.fetch_sections(CONFIG, CACHE_DIR, ref["book_id"])
        section_headings = relevant_section_headings(all_sections, passage_start, passage_end)

    return {
        "reference": {
            "book_id": ref["book_id"],
            "book_name": ref["book_name"],
            "chapter": ref["chapter"],
            "chapter_end": ref["chapter_end"],
            "verse_start": ref["verse_start"],
            "verse_end": ref["verse_end"],
            "display": esv_query_string(ref),
        },
        "verse_ids": verse_ids,
        "translations": translations,
        "footnotes": all_footnotes,
        "cross_references": cross_references,
        "ot_quotations": ot_quotations,
        "woc_spans": woc_spans,
        "glossary": glossary_hits,
        "terms": terms,
        "discourse_markers": discourse_markers,
        "sentiment": sentiment_result,
        "section_headings": section_headings,
        "fums_token": fums_token,
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep stdout clean; never logs response bodies (no ESV text in logs)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path == "/" or parsed.path == "/index.html":
                self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html")
            elif parsed.path == "/app.js":
                self._send_file(os.path.join(STATIC_DIR, "app.js"), "application/javascript")
            elif parsed.path == "/style.css":
                self._send_file(os.path.join(STATIC_DIR, "style.css"), "text/css")
            elif parsed.path == "/api/books":
                self._send_json(BOOKS)
            elif parsed.path == "/api/glossary":
                self._send_json(GLOSSARY)
            elif parsed.path == "/api/passage":
                raw = query.get("ref", [""])[0]
                ref = parse_reference(raw)
                if not ref:
                    self._send_json({"error": f"Could not understand reference '{raw}'"}, status=400)
                    return
                if not self._within_chapter_cap(ref):
                    return
                target_verse_start, target_verse_end = ref["verse_start"], ref["verse_end"]
                if query.get("context", ["0"])[0] == "1" and ref["verse_start"]:
                    ref["verse_start"] = max(1, ref["verse_start"] - CONTEXT_PAD)
                    ref["verse_end"] = ref["verse_end"] + CONTEXT_PAD
                try:
                    payload = build_passage_response(ref)
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": str(exc)}, status=502)
                    return
                if ref["verse_start"] != target_verse_start or ref["verse_end"] != target_verse_end:
                    # the verse the user actually asked for, before context
                    # padding widened the range around it
                    payload["reference"]["target_verse"] = verse_id(ref["chapter"], target_verse_start)
                self._send_json(payload)
            elif parsed.path == "/api/summary":
                ref = self._summary_ref(query)
                if not ref:
                    return
                stored = summary.load(DATA_DIR, ref)
                if stored:
                    self._send_json({"status": "ready", "summary": stored})
                elif summary.is_pending(DATA_DIR, ref):
                    self._send_json({"status": "pending"})
                else:
                    self._send_json({"status": "none"})
            else:
                self._send_json({"error": "not found"}, status=404)
        except FileNotFoundError:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        """Only /api/summary, which is a POST rather than a flag on GET because
        it writes a request file — a state change, not a lookup."""
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path != "/api/summary":
            self._send_json({"error": "not found"}, status=404)
            return

        ref = self._summary_ref(query)
        if not ref:
            return

        stored = summary.load(DATA_DIR, ref)
        if stored:  # already written — nothing to run
            self._send_json({"status": "ready", "summary": stored})
            return

        try:
            payload = build_passage_response(ref)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=502)
            return

        # With an API key set this is one round trip; without one (the default)
        # we fall back to the file handoff and a Claude Code session finishes
        # the job via /summarise. A failed API call falls back the same way
        # rather than dead-ending the panel.
        if summary.api_key():
            try:
                result = summary.generate(ref, payload)
                summary.save(DATA_DIR, ref, result)
                summary.clear_request(DATA_DIR, ref)
                self._send_json({"status": "ready", "summary": result})
                return
            except Exception as exc:  # noqa: BLE001
                print(f"[!] Summary API call failed ({exc}); falling back to a request file.")

        request_file = summary.write_request(DATA_DIR, ref, payload)
        self._send_json({"status": "pending", "request_file": request_file})

    def _within_chapter_cap(self, ref):
        """Sends the 400 itself and returns False if the span is too wide."""
        span = ref["chapter_end"] - ref["chapter"] + 1
        if span > MAX_CHAPTERS:
            self._send_json({
                "error": f"That's {span} chapters — this tool reads at most {MAX_CHAPTERS} at a time.",
            }, status=400)
            return False
        return True

    def _summary_ref(self, query):
        """Shared by both summary verbs. Sends the 400 itself and returns None
        if the reference doesn't parse, so callers just bail on a falsy result."""
        raw = query.get("ref", [""])[0]
        ref = parse_reference(raw)
        if not ref:
            self._send_json({"error": f"Could not understand reference '{raw}'"}, status=400)
            return None
        if not self._within_chapter_cap(ref):
            return None
        return ref


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(summary.summary_dir(DATA_DIR), exist_ok=True)
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"Bible study tool running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
