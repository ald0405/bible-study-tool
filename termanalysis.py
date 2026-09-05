"""Repeated word/phrase/theme detection over a passage, using NLTK.

Falls back to a small built-in stopword list if NLTK's corpus data isn't
available, so the server doesn't hard-crash if `make setup` wasn't run.
"""

import os
from collections import Counter, defaultdict

os.environ.setdefault("NLTK_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nltk_data"))

import nltk  # noqa: E402

_FALLBACK_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "for", "nor", "yet",
    "of", "to", "in", "on", "at", "by", "with", "from", "as", "is", "was", "were",
    "are", "be", "been", "being", "he", "she", "it", "they", "them", "his", "her",
    "its", "their", "you", "your", "i", "we", "our", "us", "this", "that", "these",
    "those", "not", "no", "will", "shall", "may", "have", "has", "had", "do", "did",
    "does", "who", "whom", "which", "what", "when", "where", "how", "there", "here",
    "into", "unto", "upon", "also", "even", "just", "one", "all", "would", "shall",
    "him", "himself", "herself", "themselves", "am", "let",
}

try:
    from nltk.corpus import stopwords as _nltk_stopwords

    STOPWORDS = set(_nltk_stopwords.words("english")) | _FALLBACK_STOPWORDS
except LookupError:
    STOPWORDS = _FALLBACK_STOPWORDS

# "in him", "through him", "by whom", etc. are a real pattern worth catching —
# epistles lean on this kind of repeated pronoun reference to point back at
# Christ without naming him every time — but "him"/"whom" alone are ordinary
# stopwords, and the phrase filter below normally drops any two-word phrase
# starting or ending on a stopword. So this pairing gets a narrow exception
# instead of loosening the stopword filter generally (which would just fill
# Top Terms with noise like "of the" or "and to").
_REFERENTIAL_PRONOUNS = {"him", "whom"}
_REFERENCE_PREPOSITIONS = {"in", "through", "by", "with", "of", "unto", "to"}

try:
    from nltk.stem import WordNetLemmatizer

    _lemmatizer = WordNetLemmatizer()
    _lemmatizer.lemmatize("test")  # force wordnet load, raises LookupError if missing
except LookupError:
    _lemmatizer = None


def _tokenize(text):
    try:
        return nltk.word_tokenize(text.lower())
    except LookupError:
        return text.lower().split()


def _lemma(word):
    if _lemmatizer is None:
        return word
    return _lemmatizer.lemmatize(word)


def analyze(verses, top_n=10):
    """verses: list of {'number': int, 'text': str}. Returns top_n terms/phrases
    with counts and the verse numbers each occurs in."""
    term_verses = defaultdict(set)
    term_surface = defaultdict(Counter)
    phrase_counts = Counter()
    phrase_verses = defaultdict(set)

    for v in verses:
        tokens = _tokenize(v["text"])
        alpha = [t for t in tokens if t.isalpha()]

        for t in alpha:
            if len(t) < 3 or t in STOPWORDS:
                continue
            lemma = _lemma(t)
            term_surface[lemma][t] += 1
            term_verses[lemma].add(v["number"])

        for n in (2, 3):
            for i in range(len(alpha) - n + 1):
                gram = alpha[i:i + n]
                is_referential_pair = (
                    n == 2 and gram[0] in _REFERENCE_PREPOSITIONS and gram[1] in _REFERENTIAL_PRONOUNS
                )
                if not is_referential_pair and (gram[0] in STOPWORDS or gram[-1] in STOPWORDS):
                    continue
                phrase = " ".join(gram)
                phrase_counts[phrase] += 1
                phrase_verses[phrase].add(v["number"])

    items = []
    for lemma, verse_set in term_verses.items():
        count = sum(term_surface[lemma].values())
        if count < 2:
            continue
        surface = term_surface[lemma].most_common(1)[0][0]
        items.append({"term": surface, "count": count, "verses": sorted(verse_set), "type": "word"})

    for phrase, count in phrase_counts.items():
        if count < 2:
            continue
        items.append({"term": phrase, "count": count, "verses": sorted(phrase_verses[phrase]), "type": "phrase"})

    items.sort(key=lambda x: (-x["count"], -len(x["term"].split()), x["term"]))

    # de-duplicate phrases that are just a repeated single word already counted
    seen_terms = set()
    deduped = []
    for item in items:
        if item["term"] in seen_terms:
            continue
        seen_terms.add(item["term"])
        deduped.append(item)

    return deduped[:top_n]
