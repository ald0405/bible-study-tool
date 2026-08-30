"""Passage-level tone/sentiment analysis using NLTK's VADER lexicon.

Lexicon-based rather than a transformer model deliberately: VADER scores each
word from an inspectable ~7,500-word dictionary (plus a few rule-based
adjustments for negation/intensifiers/punctuation), so a passage's overall
score is traceable back to the specific words driving it — see
positive_words/negative_words below. A BERT-style classifier would likely be
more contextually accurate but outputs one opaque number with no natural
per-word attribution, and needs heavy new dependencies this project
deliberately avoids.
"""

import os
from collections import defaultdict

os.environ.setdefault("NLTK_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nltk_data"))

import nltk  # noqa: E402

try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer

    _sia = SentimentIntensityAnalyzer()
except LookupError:
    _sia = None

SCORE_THRESHOLD = 1.0  # minimum |lexicon score| for a word to be worth tracing
TOP_N = 10


def _label(compound):
    if compound > 0.5:
        return "Strongly Positive"
    if compound > 0.05:
        return "Positive"
    if compound < -0.5:
        return "Strongly Negative"
    if compound < -0.05:
        return "Negative"
    return "Neutral or Mixed"


def _tokenize(text):
    try:
        return nltk.word_tokenize(text.lower())
    except LookupError:
        return text.lower().split()


def analyze(verses):
    """verses: list of {'number': int, 'text': str} (ESV). Returns None if the
    VADER lexicon isn't available (e.g. `make setup` wasn't run)."""
    if _sia is None or not verses:
        return None

    full_text = " ".join(v["text"] for v in verses)
    compound = _sia.polarity_scores(full_text)["compound"]

    word_verses = defaultdict(set)
    for v in verses:
        for token in _tokenize(v["text"]):
            if not token.isalpha():
                continue
            score = _sia.lexicon.get(token)
            if score is not None and abs(score) >= SCORE_THRESHOLD:
                word_verses[token].add(v["number"])

    positive, negative = [], []
    for word, verse_set in word_verses.items():
        score = _sia.lexicon[word]
        entry = {"word": word, "score": score, "verses": sorted(verse_set)}
        (positive if score > 0 else negative).append(entry)

    positive.sort(key=lambda e: -e["score"])
    negative.sort(key=lambda e: e["score"])

    return {
        "overall": {"compound": round(compound, 4), "label": _label(compound)},
        "positive_words": positive[:TOP_N],
        "negative_words": negative[:TOP_N],
    }
