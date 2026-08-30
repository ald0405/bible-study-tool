"""One-time setup: download NLTK data and the book list.

Run via `make setup` (which runs `uv sync` first). Safe to re-run — skips
anything already present.
"""

import json
import os
import sys

import nltk
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
NLTK_DIR = os.path.join(ROOT, "nltk_data")
BOOKS_URL = "https://bible.helloao.org/api/BSB/books.json"


def setup_nltk():
    os.makedirs(NLTK_DIR, exist_ok=True)
    for package in ["stopwords", "punkt", "punkt_tab", "wordnet", "omw-1.4", "vader_lexicon"]:
        try:
            nltk.data.find(package, paths=[NLTK_DIR])
            print(f"[nltk] {package} already present")
        except LookupError:
            print(f"[nltk] downloading {package}...")
            nltk.download(package, download_dir=NLTK_DIR, quiet=True)


def setup_books():
    dest = os.path.join(DATA_DIR, "books.json")
    if os.path.exists(dest):
        print("[books] already present")
        return
    print("[books] fetching book list from helloao...")
    resp = requests.get(BOOKS_URL, timeout=30)
    resp.raise_for_status()
    raw = resp.json()
    books = [
        {
            "id": b["id"],
            "name": b["commonName"],
            "chapters": b["numberOfChapters"],
            "order": b["order"],
        }
        for b in raw["books"]
    ]
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(dest, "w") as f:
        json.dump(books, f, indent=2)
    print(f"[books] wrote {len(books)} books")


def main():
    setup_nltk()
    setup_books()
    os.makedirs(os.path.join(DATA_DIR, "cache"), exist_ok=True)

    env_path = os.path.join(ROOT, ".env")
    if not os.path.exists(env_path):
        print(
            "\n[!] .env not found. Copy .env.example to .env and fill in your API keys:\n"
            "    ESV_API_TOKEN from https://api.esv.org/account/\n"
            "    API_BIBLE_KEY from https://scripture.api.bible/"
        )
        sys.exit(1)

    print("\nSetup complete. Run `make run` to start the server.")


if __name__ == "__main__":
    main()
