"""NIV via api.bible (American Bible Society's Digital Bible Platform).

Unlike the ESV API, api.bible's terms permit local caching as long as it's
checked for updates at least every 30 days — so this disk-caches with a
"fetched_at" staleness check, the same disk-cache pattern server.py already
uses for BSB/NET/FBV, just with that added expiry BSB/NET/FBV (public domain)
don't need.

api.bible also requires reporting each view via their Fair Use Management
System (FUMS): every response carries a meta.fumsToken, which the caller is
expected to report back (see static/app.js for the client-side tracker call).
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

API_BASE = "https://api.scripture.api.bible/v1"
CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_SKIP_PARA_STYLES = {"s1", "s2", "ms", "mr", "sr", "r", "d"}  # section headings etc.
_FOOTNOTE_TEXT_STYLES = {"ft", "fq", "fqa"}  # skip "fr" (ref locator) and "fv" (verse marker)


def _append(buf, s):
    if not s:
        return buf
    s = re.sub(r"\s+", " ", s)
    if buf == "":
        return s.lstrip(" ")
    if buf.endswith(" ") and s.startswith(" "):
        s = s.lstrip(" ")
    return buf + s


def _note_text(note_item):
    text = ""
    for child in note_item.get("items", []):
        if child.get("type") != "tag" or child.get("name") != "char":
            continue
        if child.get("attrs", {}).get("style") not in _FOOTNOTE_TEXT_STYLES:
            continue
        for grandchild in child.get("items", []):
            if grandchild.get("type") == "text":
                text = _append(text, grandchild["text"])
    return text.strip()


def _walk(items, texts, footnotes, verse_ref):
    for item in items:
        if item.get("type") == "text":
            v = verse_ref[0]
            if v is not None:
                texts[v] = _append(texts.get(v, ""), item["text"])
            continue

        if item.get("type") != "tag":
            continue

        name = item.get("name")
        if name == "verse":
            number = item.get("attrs", {}).get("number")
            if number and number.isdigit():
                verse_ref[0] = int(number)
            continue

        if name == "note":
            v = verse_ref[0]
            text = _note_text(item)
            if v is not None and text:
                footnotes.append({"verse": v, "text": text})
            continue

        if "items" in item:
            _walk(item["items"], texts, footnotes, verse_ref)


def _parse_chapter(data):
    texts = {}
    verse_order = []
    footnotes = []
    verse_ref = [None]  # mutable box so _walk can update "current verse"

    class _OrderTrackingDict(dict):
        def __setitem__(self, key, value):
            if key not in self:
                verse_order.append(key)
            super().__setitem__(key, value)

    tracked_texts = _OrderTrackingDict()

    for para in data["content"]:
        if para.get("type") == "tag" and para.get("attrs", {}).get("style") in _SKIP_PARA_STYLES:
            continue
        # a new top-level paragraph always implies at least a space, even
        # when it continues the previous verse (via attrs.vid) rather than
        # opening a new one — otherwise consecutive paragraphs glue together
        v = verse_ref[0]
        if v is not None and tracked_texts.get(v) and not tracked_texts[v].endswith(" "):
            tracked_texts[v] = tracked_texts[v] + " "
        _walk([para], tracked_texts, footnotes, verse_ref)

    verses = [{"number": n, "text": tracked_texts[n].strip()} for n in verse_order]
    return {"verses": verses, "footnotes": footnotes}


def _cache_path(cache_dir, book_id, chapter):
    return os.path.join(cache_dir, "NIV", f"{book_id}_{chapter}.json")


def fetch_chapter(config, cache_dir, book_id, chapter):
    """Returns {"verses": [...], "footnotes": [...], "fums_token": str|None}."""
    path = _cache_path(cache_dir, book_id, chapter)
    if os.path.exists(path):
        with open(path) as f:
            cached = json.load(f)
        age = time.time() - cached.get("fetched_at", 0)
        if age < CACHE_MAX_AGE_SECONDS:
            return {"verses": cached["verses"], "footnotes": cached["footnotes"], "fums_token": None}

    params = {"content-type": "json", "include-notes": "true", "fums-version": "3"}
    url = f"{API_BASE}/bibles/{config['api_bible_niv_id']}/chapters/{book_id}.{chapter}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"api-key": config["api_bible_key"]})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    parsed = _parse_chapter(raw["data"])
    fums_token = raw.get("meta", {}).get("fumsToken")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"fetched_at": time.time(), **parsed}, f)

    return {**parsed, "fums_token": fums_token}
