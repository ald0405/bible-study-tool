"""Who acts: the subject of each verb, its voice, and its mood.

Reading Ephesians 1:3-14, almost every verb is something God does and the
reader is the object of it — which is the passage's argument, carried by syntax
rather than by vocabulary. This module makes that countable: for each finite
verb it reports the subject, whether the clause is active or passive, and
whether it is an imperative.

Two limits are deliberate, because the alternatives were tested and are wrong.

**It analyses the English.** Greek participles become English finite verbs and
some Greek passives become English actives, so this describes the ESV's syntax
rather than Paul's. That is the right target for a reading aid — you read the
translation — and the ESV's "essentially literal" policy is the same property
data/discourse_markers.json already leans on. The panel says so plainly.

**It does not resolve third-person pronouns.** The obvious heuristic — "he"
takes the most recent named subject — was tried and is wrong often enough to
mislead: it resolved Eph 1:4 "he chose" to *Paul*, Acts 19:5 "they baptized" to
*John*, and Rom 9:15 "he says" to *Rebekah*. So a participant is assigned only
where grammar settles it (first and second person) or where the subject is a
divine name in the text; a bare "he" is reported as "he" and left to the
reader, who knows perfectly well who it is.

Verb *semantics* are not classified either. Separating "acting" verbs from
"receiving" ones would be the real prize — in Eph 1 the reader's verbs are all
be/have/obtain/acquire — but WordNet puts *lavish* (God giving) and *obtain*
(us receiving) in the same class, so any such split would be guesswork. Voice
and mood come straight from the parse and are reliable; the pattern shows up
by reading the list.
"""

import spacy

MODEL = "en_core_web_sm"

try:
    # the lemmatizer stays: the panel groups verbs by lemma, so "chose" and
    # "choose" are one row. Only entity recognition is dropped, unused here.
    _nlp = spacy.load(MODEL, disable=["ner"])
except OSError:  # model not installed — degrade like sentiment.py does
    _nlp = None

# Only names, never pronouns. "the Beloved" is included because Eph 1:6 uses it
# as a title for Christ; "Spirit" catches "the Holy Spirit" through its head noun.
#
# A match must also be *capitalised*, which is how the ESV itself separates the
# senses and is far more reliable than trying to read the modifiers: "the Holy
# Spirit" and "the Spirit" against "the evil spirit" (Acts 19:15), and "God"
# against "gods made with hands" (Acts 19:26). Without this the evil spirit that
# mauls the sons of Sceva gets filed under God, which is both wrong and comic.
DIVINE_NAMES = {
    "god", "christ", "jesus", "lord", "father", "spirit", "beloved",
    "almighty", "messiah", "saviour", "savior",
}


def _is_divine_name(token):
    return token.lower_ in DIVINE_NAMES and token.text[:1].isupper()

FIRST_SINGULAR = {"i", "me", "my", "myself", "mine"}
FIRST_PLURAL = {"we", "us", "our", "ourselves", "ours"}
SECOND = {"you", "your", "yours", "yourself", "yourselves", "thou", "thee", "thy", "ye"}

# what the frontend colours by
AUTHOR = "author"
READERS = "readers"
SHARED = "we"
GOD = "God"
THIRD_PERSON = "third-person"  # a bare he/they/it, deliberately unresolved
OTHER = "other"

THIRD_PERSON_PRONOUNS = {
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "it", "its", "itself",
    "they", "them", "their", "theirs", "themselves",
}


def available():
    return _nlp is not None


def _participant(token):
    """(participant, label) for a subject token.

    `participant` is the colour class; `label` is the bucket the panel groups
    by. A bare third-person pronoun gets its own bucket named after itself —
    "he", "they" — rather than being folded into OTHER. That distinction is the
    whole point: in Ephesians 1 a single "he" carries nine verbs while the
    reader's verbs are all be/have/obtain, and burying "he" among unrelated
    subjects like "purpose" would hide exactly the pattern worth seeing. The
    reader supplies the referent; this module refuses to guess it."""
    word = token.lower_
    if word in FIRST_SINGULAR:
        return AUTHOR, "I"
    if word in FIRST_PLURAL:
        return SHARED, "we"
    if word in SECOND:
        return READERS, "you"
    if _is_divine_name(token):
        return GOD, token.text
    # A name like "the Lord Jesus Christ" hangs its other parts off a head noun,
    # so check those — but only the parts of the name itself. Searching the whole
    # subtree instead sweeps up prepositional phrases and makes "the will of God",
    # "the name of the Lord" and even "Paul, an apostle of Christ Jesus" come out
    # as God, when in each the subject is the thing belonging to him, not him.
    for part in token.children:
        if part.dep_ in ("compound", "flat", "appos") and _is_divine_name(part):
            return GOD, part.text
    if word in THIRD_PERSON_PRONOUNS:
        return THIRD_PERSON, word
    return OTHER, token.text


def _subject_of(verb):
    """The subject token of a finite verb, following a relative pronoun up to
    the noun its clause modifies — that step is structural (spaCy gives us the
    head directly), unlike pronoun coreference, so it is safe."""
    subjects = [c for c in verb.children if c.dep_ in ("nsubj", "nsubjpass")]
    if not subjects:
        return None, False
    subject = subjects[0]
    passive = subject.dep_ == "nsubjpass" or any(c.dep_ == "auxpass" for c in verb.children)
    if subject.lower_ in ("who", "which", "that") and verb.dep_ == "relcl":
        return verb.head, passive
    return subject, passive


def _is_imperative(verb):
    """A base-form verb heading its own clause with no subject and no auxiliary.
    Checked against Ephesians 1 (none) and Ephesians 5 (thirteen)."""
    if verb.pos_ != "VERB" or verb.tag_ != "VB":
        return False
    if verb.dep_ not in ("ROOT", "conj", "advcl", "ccomp"):
        return False
    return not any(c.dep_ in ("nsubj", "nsubjpass", "aux", "auxpass") for c in verb.children)


def analyze(verses):
    """verses: list of {'id': str, 'text': str} (ESV), in reading order.

    Returns {"spans": [...], "verbs": [...]} or None if the spaCy model is
    missing. Spans carry character offsets into the verse's own text, the same
    contract esv_html and discourse.find_markers use, so the frontend's
    highlighting needs no new machinery.
    """
    if _nlp is None or not verses:
        return None

    spans = []
    verbs = []
    for verse in verses:
        doc = _nlp(verse["text"])
        for token in doc:
            if token.pos_ not in ("VERB", "AUX"):
                continue
            if token.dep_ in ("aux", "auxpass"):
                continue  # folded into the verb it serves, so "were sealed" is one verb

            imperative = _is_imperative(token)
            subject, passive = _subject_of(token)
            if subject is None and not imperative:
                continue  # a participle or bare infinitive with nothing to attribute

            if subject is not None:
                participant, label = _participant(subject)
            else:
                participant, label = READERS, "you"  # the addressee of an imperative
            entry = {
                "verse": verse["id"],
                "lemma": token.lemma_.lower(),
                "surface": token.text,
                "subject": subject.text if subject is not None else None,
                "participant": participant,
                "label": label,
                "voice": "passive" if passive else "active",
                # an imperative has no subject in the text; its subject is whoever
                # is being addressed, which in a letter is the readers
                "mood": "imperative" if imperative else "indicative",
            }
            verbs.append(entry)

            # the verb itself, and its subject where one is written
            spans.append({
                "verse": verse["id"], "start": token.idx, "end": token.idx + len(token.text),
                "kind": "verb", "participant": participant, "label": label,
                "voice": entry["voice"], "mood": entry["mood"],
            })
            if subject is not None and subject.doc is doc:
                spans.append({
                    "verse": verse["id"], "start": subject.idx,
                    "end": subject.idx + len(subject.text),
                    "kind": "subject", "participant": participant, "label": label,
                    "voice": entry["voice"], "mood": entry["mood"],
                })

    spans.sort(key=lambda s: (s["start"], s["end"]))
    return {"spans": spans, "verbs": verbs}
