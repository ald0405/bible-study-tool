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

REF_RE = re.compile(
    r"^\s*([1-3])?\s*([A-Za-z][A-Za-z\s\.]*?)\.?\s*(\d+)"
    r"(?:\s*:\s*(\d+)(?:\s*-\s*(\d+))?)?\s*$"
)

# How many verses of surrounding context to pull in when jumping to a
# cross-reference or quotation target, so you land in the passage rather
# than on one verse in isolation. Clamped to the chapter (no cross-chapter
# padding, same boundary the rest of this app assumes — see README).
CONTEXT_PAD = 4


def parse_reference(raw):
    # ESV's own cross-reference hrefs use an en dash for ranges (e.g. "Joel 2:28–32")
    normalized = (raw or "").replace("–", "-").replace("—", "-")
    m = REF_RE.match(normalized)
    if not m:
        return None
    prefix, book_word, chapter, vstart, vend = m.groups()
    key = (prefix or "") + re.sub(r"[\s\.]", "", book_word).lower()
    book_id = REVERSE_ALIASES.get(key)
    if not book_id:
        return None
    book = BOOKS_BY_ID[book_id]
    chapter = int(chapter)
    if chapter < 1 or chapter > book["chapters"]:
        return None
    verse_start = int(vstart) if vstart else None
    verse_end = int(vend) if vend else verse_start
    return {
        "book_id": book_id,
        "book_name": book["name"],
        "chapter": chapter,
        "verse_start": verse_start,
        "verse_end": verse_end,
    }


def esv_query_string(ref):
    q = f"{ref['book_name']} {ref['chapter']}"
    if ref["verse_start"]:
        q += f":{ref['verse_start']}"
        if ref["verse_end"] and ref["verse_end"] != ref["verse_start"]:
            q += f"-{ref['verse_end']}"
    return q


# ---------------------------------------------------------------------------
# ESV — live fetch only, in-memory cache, never persisted to disk
# ---------------------------------------------------------------------------

_esv_cache = {}  # key -> {"verses": [...], "citations": [...], "woc_spans": [...], "count": int}
_esv_cache_order = []
_esv_cache_total = 0


def _esv_cache_put(key, result):
    global _esv_cache_total
    count = len(result["verses"])
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
    key = (ref["book_id"], ref["chapter"], ref["verse_start"], ref["verse_end"])
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
    result = esv_html.parse(passages[0]) if passages else {"verses": [], "citations": [], "woc_spans": []}
    _esv_cache_put(key, result)
    return result


# ---------------------------------------------------------------------------
# BSB / NET / FBV — helloao.org, public domain, disk-cached
# ---------------------------------------------------------------------------

def fetch_helloao_chapter(translation_code, book_id, chapter):
    translation = TRANSLATIONS[translation_code]
    cache_path = os.path.join(CACHE_DIR, translation, f"{book_id}_{chapter}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    url = f"{HELLOAO_BASE}/{translation}/{book_id}/{chapter}.json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    verses = []
    for item in data["chapter"]["content"]:
        if item.get("type") == "verse":
            # content parts are either plain strings, poetry lines
            # ({"text": ..., "poem": N}), or non-text markers (footnote
            # callouts {"noteId": N}, {"lineBreak": ...}) — join the textual
            # parts with a space, then clean up doubled/pre-punctuation spaces.
            parts = []
            for part in item["content"]:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
            text = " ".join(parts)
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r"\s+([,.;:!?])", r"\1", text).strip()
            verses.append({"number": item["number"], "text": text})

    footnotes = [
        {"verse": fn["reference"]["verse"], "text": fn["text"]}
        for fn in data["chapter"].get("footnotes", [])
    ]

    result = {"verses": verses, "footnotes": footnotes}
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(result, f)
    return result


def slice_range(items, verse_start, verse_end, key="number"):
    if not verse_start:
        return items
    return [x for x in items if verse_start <= x[key] <= verse_end]


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
    esv_result = fetch_esv(ref)
    esv_verses = esv_result["verses"]

    # a padded/context request can ask for a verse_end past the chapter's
    # actual last verse (e.g. context around a verse near the end); the ESV
    # API just quietly stops at the real last verse, so clamp here too or
    # the displayed title would claim verses that were never fetched
    if ref["verse_end"] and esv_verses:
        ref["verse_end"] = min(ref["verse_end"], esv_verses[-1]["number"])

    translations = {"ESV": esv_verses}
    all_footnotes = []

    niv_result = niv.fetch_chapter(CONFIG, CACHE_DIR, ref["book_id"], ref["chapter"])
    translations["NIV"] = slice_range(niv_result["verses"], ref["verse_start"], ref["verse_end"])
    for fn in slice_range(niv_result["footnotes"], ref["verse_start"], ref["verse_end"], key="verse"):
        all_footnotes.append({"translation": "NIV", "verse": fn["verse"], "text": fn["text"]})
    fums_token = niv_result["fums_token"]

    for code in ("BSB", "NET", "FBV"):
        chapter_data = fetch_helloao_chapter(code, ref["book_id"], ref["chapter"])
        verses = slice_range(chapter_data["verses"], ref["verse_start"], ref["verse_end"])
        translations[code] = verses
        for fn in slice_range(chapter_data["footnotes"], ref["verse_start"], ref["verse_end"], key="verse"):
            all_footnotes.append({"translation": code, "verse": fn["verse"], "text": fn["text"]})

    base_verses = esv_verses

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
        cross_references.setdefault(str(citation["verse"]), []).append(entry)

    # Direct OT-in-NT (or NT-in-NT) quotations, expanded into per-verse
    # highlight spans — see esv_html.quotation_spans for how the block-indent
    # paragraph boundary is used to correctly span multi-verse quotations.
    ot_quotations = esv_html.quotation_spans(esv_verses, esv_result["citations"])
    woc_spans = esv_result["woc_spans"]

    glossary_hits = []
    for entry in GLOSSARY:
        matched_verses = [
            v["number"] for v in base_verses
            if any(trigger in v["text"].lower() for trigger in entry["triggers"])
        ]
        if matched_verses:
            glossary_hits.append({**entry, "verses": matched_verses})

    terms = termanalysis.analyze(base_verses)
    discourse_markers = discourse.find_markers(esv_verses)
    sentiment_result = sentiment.analyze(base_verses)

    return {
        "reference": {
            "book_id": ref["book_id"],
            "book_name": ref["book_name"],
            "chapter": ref["chapter"],
            "verse_start": ref["verse_start"],
            "verse_end": ref["verse_end"],
            "display": esv_query_string(ref),
        },
        "translations": translations,
        "footnotes": all_footnotes,
        "cross_references": cross_references,
        "ot_quotations": ot_quotations,
        "woc_spans": woc_spans,
        "glossary": glossary_hits,
        "terms": terms,
        "discourse_markers": discourse_markers,
        "sentiment": sentiment_result,
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
                    payload["reference"]["target_verse_start"] = target_verse_start
                    payload["reference"]["target_verse_end"] = target_verse_end
                self._send_json(payload)
            else:
                self._send_json({"error": "not found"}, status=404)
        except FileNotFoundError:
            self._send_json({"error": "not found"}, status=404)


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"Bible study tool running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
