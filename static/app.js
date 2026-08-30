const COLUMNS = ["ESV", "NIV", "BSB", "NET", "FBV"];
const COLUMN_LABELS = { ESV: "ESV", NIV: "NIV", BSB: "Berean (BSB)", NET: "NET", FBV: "Free Bible Version" };
const TOGGLEABLE_COLUMNS = COLUMNS.filter((c) => c !== "ESV"); // ESV is pinned — everything else in the sidebar is computed from it

const els = {
  form: document.getElementById("search-form"),
  input: document.getElementById("search-input"),
  empty: document.getElementById("empty-state"),
  loading: document.getElementById("loading-state"),
  error: document.getElementById("error-state"),
  view: document.getElementById("passage-view"),
  title: document.getElementById("passage-title"),
  translationPicker: document.getElementById("translation-picker"),
  grid: document.getElementById("reading-grid"),
  termsChart: document.getElementById("terms-chart"),
  discourseList: document.getElementById("discourse-list"),
  crossrefList: document.getElementById("crossref-list"),
  glossaryList: document.getElementById("glossary-list"),
  sentimentContent: document.getElementById("sentiment-content"),
  tooltip: document.getElementById("tooltip"),
};

function loadActiveColumns() {
  try {
    const saved = JSON.parse(localStorage.getItem("activeColumns"));
    if (Array.isArray(saved) && saved.length) {
      const set = new Set(saved.filter((c) => COLUMNS.includes(c)));
      set.add("ESV");
      return set;
    }
  } catch (e) {
    // ignore malformed storage, fall through to default
  }
  return new Set(COLUMNS);
}

function saveActiveColumns() {
  localStorage.setItem("activeColumns", JSON.stringify([...activeColumns]));
}

let activeColumns = loadActiveColumns();
let currentData = null;
let currentRef = null; // the reference currently loaded, so our own hash writes don't self-trigger a reload
let discourseByVerse = {}; // verse number -> [{start, end, category}], ESV only
let citationByVerse = {}; // verse number -> [{start, end, refs, title}] — direct OT/NT quotations only
let wocByVerse = {}; // verse number -> [{start, end}] — words of Christ
let sentimentByVerse = {}; // verse number -> [{start, end, kind}] — only populated while the tone toggle is on
let sentimentToggleOn = false;

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Shows how many items a collapsed panel holds, e.g. "Top Terms (10)", so
// there's a reason to open it visible without opening it.
function setPanelCount(elementId, count) {
  const el = document.getElementById(elementId);
  if (el) el.textContent = count > 0 ? `(${count})` : "";
}

// Renders `text` as HTML, wrapping the given non-overlapping [start,end)
// ranges (sorted by start) each in the markup for its `kind`.
function rangesToHtml(text, ranges) {
  let html = "";
  let pos = 0;
  ranges.forEach((r) => {
    if (r.start < pos) return; // defensively skip any residual overlap
    html += escapeHtml(text.slice(pos, r.start));
    const inner = escapeHtml(text.slice(r.start, r.end));
    if (r.kind === "term") {
      html += `<mark>${inner}</mark>`;
    } else if (r.kind === "quotation") {
      const refsAttr = escapeHtml(r.refs.join("|"));
      html += `<span class="ot-quote" data-refs="${refsAttr}" data-title="${escapeHtml(r.title)}">${inner}</span>`;
    } else if (r.kind === "woc") {
      html += `<span class="woc-text" title="Words of Christ">${inner}</span>`;
    } else if (r.kind === "sentiment-positive") {
      html += `<span class="sentiment-positive" title="Positive tone">${inner}</span>`;
    } else if (r.kind === "sentiment-negative") {
      html += `<span class="sentiment-negative" title="Negative tone">${inner}</span>`;
    } else {
      html += `<span class="discourse-marker" title="${escapeHtml(r.category)}">${inner}</span>`;
    }
    pos = r.end;
  });
  html += escapeHtml(text.slice(pos));
  return html;
}

// Removes any overlap with `subtract` from `base` ranges, splitting a base
// range into up to two pieces if `subtract` falls in its middle. Used so
// words-of-Christ coloring yields to quotation styling where they overlap
// (extremely common — most OT quotations in the Gospels are spoken by Jesus)
// instead of one silently dropping the other.
function subtractRanges(base, subtract) {
  let result = base;
  subtract.forEach((sub) => {
    result = result.flatMap((r) => {
      if (sub.end <= r.start || sub.start >= r.end) return [r];
      const pieces = [];
      if (sub.start > r.start) pieces.push({ ...r, end: Math.min(sub.start, r.end) });
      if (sub.end < r.end) pieces.push({ ...r, start: Math.max(sub.end, r.start) });
      return pieces;
    });
  });
  return result.filter((r) => r.end > r.start);
}

// The always-on highlight layer for one ESV verse: quotations + discourse
// markers + words-of-Christ coloring (with quotations carved out of it),
// plus tone highlighting when that toggle is on. Tone is a background wash
// (not a text-color/underline change like the others), so rather than a
// full nesting renderer, it simply yields wherever a foreground range
// already claims the same text — same pattern as the woc/quotation carve-out.
function buildBaseRanges(verseNum) {
  const quotations = (citationByVerse[verseNum] || []).map((q) => ({ ...q, kind: "quotation" }));
  const discourse = (discourseByVerse[verseNum] || []).map((d) => ({ ...d, kind: "discourse" }));
  let woc = (wocByVerse[verseNum] || []).map((w) => ({ ...w, kind: "woc" }));
  woc = subtractRanges(woc, quotations);
  const foreground = [...quotations, ...discourse, ...woc];

  let sentiment = sentimentToggleOn ? (sentimentByVerse[verseNum] || []) : [];
  sentiment = subtractRanges(sentiment, foreground);

  return [...foreground, ...sentiment].sort((a, b) => a.start - b.start);
}

// Builds the verse->ranges map for every occurrence of the top
// positive/negative words in currentData.sentiment, directly from the
// fetched ESV verse text (not the DOM — this runs before renderGrid on a
// fresh passage load, so the cells don't exist yet).
function buildSentimentByVerse(esvVerses) {
  if (!currentData || !currentData.sentiment || !esvVerses) return {};
  const textByVerse = {};
  esvVerses.forEach((v) => { textByVerse[v.number] = v.text; });

  const map = {};
  const words = [
    ...(currentData.sentiment.positive_words || []).map((w) => ({ ...w, kind: "sentiment-positive" })),
    ...(currentData.sentiment.negative_words || []).map((w) => ({ ...w, kind: "sentiment-negative" })),
  ];
  words.forEach((entry) => {
    const re = new RegExp(`\\b${escapeRegExp(entry.word)}\\b`, "gi");
    entry.verses.forEach((num) => {
      const text = textByVerse[num];
      if (text === undefined) return;
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(text))) {
        (map[num] ||= []).push({ start: m.index, end: m.index + m[0].length, kind: entry.kind });
        if (m.index === re.lastIndex) re.lastIndex++;
      }
    });
  });
  return map;
}

function renderEsvCellText(el) {
  const text = el.dataset.original;
  const verseNum = Number(el.closest(".verse-cell").dataset.verse);
  const ranges = buildBaseRanges(verseNum);
  el.innerHTML = ranges.length ? rangesToHtml(text, ranges) : escapeHtml(text);
}

function showState(state) {
  els.empty.classList.add("hidden");
  els.loading.classList.add("hidden");
  els.error.classList.add("hidden");
  els.view.classList.add("hidden");
  if (state === "empty") els.empty.classList.remove("hidden");
  if (state === "loading") els.loading.classList.remove("hidden");
  if (state === "error") els.error.classList.remove("hidden");
  if (state === "view") els.view.classList.remove("hidden");
}

function hideTooltip() {
  els.tooltip.classList.add("hidden");
}

function showTooltip(anchorEl, html) {
  els.tooltip.innerHTML = html;
  els.tooltip.classList.remove("hidden");
  const rect = anchorEl.getBoundingClientRect();
  const top = rect.bottom + window.scrollY + 6;
  let left = rect.left + window.scrollX;
  const maxLeft = window.scrollX + document.documentElement.clientWidth - 340;
  if (left > maxLeft) left = maxLeft;
  els.tooltip.style.top = `${top}px`;
  els.tooltip.style.left = `${left}px`;
}

document.addEventListener("click", (e) => {
  if (!els.tooltip.contains(e.target) && !e.target.closest(".note-marker") && !e.target.closest(".ot-quote")) {
    hideTooltip();
  }
});

// Clicking a highlighted OT/NT quotation shows what it's quoting and lets
// you jump straight there. Delegated (rather than a listener per span)
// because these spans are generated as HTML strings, not built as DOM nodes.
document.addEventListener("click", (e) => {
  const jumpBtn = e.target.closest(".tooltip-jump");
  if (jumpBtn) {
    e.stopPropagation();
    hideTooltip();
    loadPassage(jumpBtn.dataset.ref);
    return;
  }
  const quoteEl = e.target.closest(".ot-quote");
  if (quoteEl) {
    e.stopPropagation();
    const refs = quoteEl.dataset.refs.split("|");
    const primary = refs[0];
    showTooltip(
      quoteEl,
      `<strong>${escapeHtml(quoteEl.dataset.title)}</strong><br>` +
        `<button class="tooltip-jump" data-ref="${escapeHtml(primary)}">Jump to ${escapeHtml(primary)} →</button>`
    );
  }
});

async function loadPassage(ref) {
  showState("loading");
  hideTooltip();
  try {
    const resp = await fetch(`/api/passage?ref=${encodeURIComponent(ref)}`);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "Something went wrong");
    }
    currentData = data;
    currentRef = ref; // set before touching location.hash so the hashchange handler below sees it's already loaded
    location.hash = encodeURIComponent(ref);
    render(data);
    showState("view");
  } catch (err) {
    els.error.textContent = err.message;
    showState("error");
  }
}

function render(data) {
  els.title.textContent = data.reference.display;

  discourseByVerse = {};
  (data.discourse_markers || []).forEach((m) => {
    (discourseByVerse[m.verse] ||= []).push({ start: m.start, end: m.end, category: m.category });
  });

  citationByVerse = {};
  (data.ot_quotations || []).forEach((q) => {
    (citationByVerse[q.verse] ||= []).push({ start: q.start, end: q.end, refs: q.refs, title: q.title });
  });

  wocByVerse = {};
  (data.woc_spans || []).forEach((w) => {
    (wocByVerse[w.verse] ||= []).push({ start: w.start, end: w.end });
  });

  // rebuild for the new passage if the toggle was already on when we searched
  sentimentByVerse = sentimentToggleOn ? buildSentimentByVerse(data.translations.ESV) : {};

  renderTranslationPicker();
  renderGrid(data);
  renderTerms(data);
  renderDiscourseMarkers(data);
  renderSentiment(data);
  renderGlossary(data);
  els.crossrefList.innerHTML = '<p class="muted">Click a verse to see related passages.</p>';
  const crossrefTotal = Object.values(data.cross_references || {}).reduce((sum, refs) => sum + refs.length, 0);
  setPanelCount("crossref-count", crossrefTotal);

  // api.bible's Fair Use Management System requires reporting each view of
  // their content back to them — see niv.py for why this exists.
  if (data.fums_token && window.fums) {
    window.fums("trackView", data.fums_token);
  }
}

function renderTranslationPicker() {
  els.translationPicker.innerHTML = "";
  const label = document.createElement("span");
  label.className = "picker-label";
  label.textContent = "Translations:";
  els.translationPicker.appendChild(label);

  TOGGLEABLE_COLUMNS.forEach((code) => {
    const id = `col-toggle-${code}`;
    const wrap = document.createElement("label");
    wrap.className = "picker-item";
    wrap.htmlFor = id;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.id = id;
    checkbox.checked = activeColumns.has(code);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        activeColumns.add(code);
      } else {
        activeColumns.delete(code);
      }
      saveActiveColumns();
      renderGrid(currentData);
    });

    wrap.appendChild(checkbox);
    wrap.append(COLUMN_LABELS[code]);
    els.translationPicker.appendChild(wrap);
  });
}

function renderGrid(data) {
  const grid = els.grid;
  grid.innerHTML = "";

  const visibleColumns = COLUMNS.filter((c) => activeColumns.has(c));

  const allNumbers = new Set();
  visibleColumns.forEach((c) => data.translations[c].forEach((v) => allNumbers.add(v.number)));
  if (allNumbers.size === 0) return;
  const minV = Math.min(...allNumbers);
  const maxV = Math.max(...allNumbers);
  grid.style.gridTemplateRows = `auto repeat(${maxV - minV + 1}, auto)`;
  // set dynamically rather than in CSS — fewer active translations means
  // each gets more width instead of leaving the freed columns blank
  grid.style.gridTemplateColumns = `repeat(${visibleColumns.length}, minmax(180px, 1fr))`;

  const lastColIdx = visibleColumns.length - 1;

  visibleColumns.forEach((c, i) => {
    const h = document.createElement("div");
    h.className = "col-header";
    h.textContent = COLUMN_LABELS[c];
    h.style.gridColumn = i + 1;
    h.style.gridRow = 1;
    if (i === lastColIdx) h.style.borderRight = "none";
    grid.appendChild(h);
  });

  visibleColumns.forEach((c, colIdx) => {
    data.translations[c].forEach((v) => {
      const cell = document.createElement("div");
      cell.className = "verse-cell";
      cell.dataset.col = c;
      cell.dataset.verse = v.number;
      cell.style.gridColumn = colIdx + 1;
      cell.style.gridRow = v.number - minV + 2;
      if (colIdx === lastColIdx) cell.style.borderRight = "none";

      const badge = document.createElement("span");
      badge.className = "verse-badge";
      badge.textContent = v.number;
      cell.appendChild(badge);

      const textSpan = document.createElement("span");
      textSpan.className = "verse-text";
      textSpan.dataset.original = v.text;
      cell.appendChild(textSpan);
      if (c === "ESV") {
        renderEsvCellText(textSpan);
      } else {
        textSpan.textContent = v.text;
      }

      const notes = data.footnotes.filter((f) => f.translation === c && f.verse === v.number);
      notes.forEach((note, idx) => {
        const marker = document.createElement("sup");
        marker.className = "note-marker";
        marker.textContent = "†";
        marker.addEventListener("click", (e) => {
          e.stopPropagation();
          showTooltip(marker, `<strong>${escapeHtml(c)} note</strong><br>${escapeHtml(note.text)}`);
        });
        cell.appendChild(marker);
      });

      cell.addEventListener("click", () => selectVerse(v.number));
      grid.appendChild(cell);
    });
  });
}

function selectVerse(num) {
  document.querySelectorAll(".verse-cell.selected").forEach((el) => el.classList.remove("selected"));
  document.querySelectorAll(`.verse-cell[data-verse="${num}"]`).forEach((el) => el.classList.add("selected"));
  renderCrossrefs(num);
  // the sidebar scrolls internally (see .sidebar in style.css), so this
  // brings Cross References into view within the sidebar itself rather than
  // jumping the whole page and losing your place in the passage
  document.getElementById("crossref-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderCrossrefs(num) {
  const refs = currentData.cross_references[String(num)];
  if (!refs || refs.length === 0) {
    els.crossrefList.innerHTML = `<p class="muted">No cross-references found for verse ${num}.</p>`;
    return;
  }
  els.crossrefList.innerHTML = "";
  refs.forEach((r) => {
    const btn = document.createElement("button");
    btn.className = "crossref-item";
    const badge = r.is_quotation ? '<span class="cr-quote-badge">Quotation</span>' : "";
    btn.innerHTML = `
      <div class="cr-head">
        <span class="cr-ref">${escapeHtml(r.refs.join("; "))} →</span>
        ${badge}
      </div>
      ${r.preview ? `<div class="cr-preview">“${escapeHtml(r.preview)}” <span class="cr-source">— BSB</span></div>` : ""}
    `;
    btn.addEventListener("click", () => {
      if (r.refs[0]) loadPassage(r.refs[0]);
    });
    els.crossrefList.appendChild(btn);
  });
}

function renderTerms(data) {
  els.termsChart.innerHTML = "";
  setPanelCount("terms-count", data.terms ? data.terms.length : 0);
  if (!data.terms || data.terms.length === 0) {
    els.termsChart.innerHTML = '<p class="muted">No repeated terms found in this passage.</p>';
    return;
  }
  const maxCount = Math.max(...data.terms.map((t) => t.count));
  data.terms.forEach((term) => {
    const row = document.createElement("div");
    row.className = "term-row";

    const label = document.createElement("div");
    label.className = "term-label";
    label.innerHTML = `<span class="term-name">${escapeHtml(term.term)}</span><span class="term-count">${term.count}×</span>`;
    row.appendChild(label);

    const track = document.createElement("div");
    track.className = "term-bar-track";
    const fill = document.createElement("div");
    fill.className = "term-bar-fill";
    fill.style.width = `${Math.max(6, (term.count / maxCount) * 100)}%`;
    track.appendChild(fill);
    row.appendChild(track);

    const verses = document.createElement("div");
    verses.className = "term-verses";
    verses.textContent = `vv. ${term.verses.join(", ")}`;
    row.appendChild(verses);

    row.addEventListener("click", () => {
      setActiveHighlight(row, new RegExp(`\\b(${escapeRegExp(term.term)})\\b`, "gi"), term.verses);
    });

    els.termsChart.appendChild(row);
  });
}

function renderSentiment(data) {
  const container = els.sentimentContent;
  container.innerHTML = "";
  const s = data.sentiment;
  if (!s) {
    container.innerHTML = '<p class="muted">Sentiment analysis unavailable — run <code>make setup</code> to install the NLTK VADER lexicon.</p>';
    setPanelCount("sentiment-count", 0);
    return;
  }
  setPanelCount("sentiment-count", (s.positive_words || []).length + (s.negative_words || []).length);

  const compound = s.overall.compound;
  const posPct = Math.max(0, compound) * 100;
  const negPct = Math.max(0, -compound) * 100;
  const sign = compound > 0 ? "+" : "";

  const gauge = document.createElement("div");
  gauge.className = "gauge";
  gauge.innerHTML = `
    <div class="gauge-track">
      <div class="gauge-half neg"><div class="gauge-fill negative" style="width:${negPct}%"></div></div>
      <div class="gauge-center-tick"></div>
      <div class="gauge-half pos"><div class="gauge-fill positive" style="width:${posPct}%"></div></div>
    </div>
    <div class="gauge-label"><strong>${escapeHtml(s.overall.label)}</strong><span class="g-score">(${sign}${compound.toFixed(2)})</span></div>
  `;
  container.appendChild(gauge);

  const toggleRow = document.createElement("label");
  toggleRow.className = "toggle-row";
  const toggleInput = document.createElement("input");
  toggleInput.type = "checkbox";
  toggleInput.checked = sentimentToggleOn;
  toggleInput.addEventListener("change", () => {
    sentimentToggleOn = toggleInput.checked;
    sentimentByVerse = sentimentToggleOn ? buildSentimentByVerse(currentData.translations.ESV) : {};
    clearTermHighlight(); // re-renders every ESV cell from buildBaseRanges, which now reflects the new toggle state
  });
  toggleRow.appendChild(toggleInput);
  toggleRow.append("Highlight tone throughout passage");
  container.appendChild(toggleRow);

  renderSentimentWordList(container, "Positive language", s.positive_words, "positive");
  renderSentimentWordList(container, "Negative language", s.negative_words, "negative");
}

function renderSentimentWordList(container, label, words, polarity) {
  if (!words || words.length === 0) return;

  const heading = document.createElement("div");
  heading.className = `sentiment-column-label ${polarity}`;
  heading.textContent = label;
  container.appendChild(heading);

  const maxScore = Math.max(...words.map((w) => Math.abs(w.score)));

  words.forEach((entry) => {
    const row = document.createElement("div");
    row.className = `term-row ${polarity}`;

    const sign = entry.score > 0 ? "+" : "";
    const labelDiv = document.createElement("div");
    labelDiv.className = "term-label";
    labelDiv.innerHTML = `<span class="term-name">${escapeHtml(entry.word)}</span><span class="term-count">${sign}${entry.score.toFixed(1)}</span>`;
    row.appendChild(labelDiv);

    const track = document.createElement("div");
    track.className = "term-bar-track";
    const fill = document.createElement("div");
    fill.className = "term-bar-fill";
    fill.style.width = `${Math.max(6, (Math.abs(entry.score) / maxScore) * 100)}%`;
    track.appendChild(fill);
    row.appendChild(track);

    const verses = document.createElement("div");
    verses.className = "term-verses";
    verses.textContent = `vv. ${entry.verses.join(", ")}`;
    row.appendChild(verses);

    row.addEventListener("click", () => {
      setActiveHighlight(row, new RegExp(`\\b(${escapeRegExp(entry.word)})\\b`, "gi"), entry.verses);
    });

    container.appendChild(row);
  });
}

function renderDiscourseMarkers(data) {
  els.discourseList.innerHTML = "";
  const hits = data.discourse_markers || [];
  if (hits.length === 0) {
    els.discourseList.innerHTML = '<p class="muted">No discourse markers found in this passage.</p>';
    setPanelCount("discourse-count", 0);
    return;
  }

  // group case-insensitively by (category, word) — "But" (v.4) and "but"
  // (v.6) are the same marker and should list once with both verses, not
  // as two separate rows
  const groups = new Map(); // `${category} ${lowercased word}` -> {category, marker, verses: Set}
  hits.forEach((h) => {
    const key = `${h.category} ${h.marker.toLowerCase()}`;
    if (!groups.has(key)) {
      groups.set(key, { category: h.category, marker: h.marker.toLowerCase(), verses: new Set() });
    }
    groups.get(key).verses.add(h.verse);
  });
  setPanelCount("discourse-count", groups.size);

  const byCategory = {};
  groups.forEach((g) => {
    (byCategory[g.category] ||= []).push(g);
  });

  Object.keys(byCategory).forEach((category) => {
    const heading = document.createElement("div");
    heading.className = "discourse-category";
    heading.textContent = category;
    els.discourseList.appendChild(heading);

    byCategory[category].forEach((group) => {
      const verses = [...group.verses].sort((a, b) => a - b);
      const btn = document.createElement("button");
      btn.className = "discourse-item";
      btn.innerHTML = `<span class="d-marker">${escapeHtml(group.marker)}</span><span class="d-verse">vv. ${verses.join(", ")}</span>`;
      btn.addEventListener("click", () => {
        selectVerse(verses[0]);
        const cell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${verses[0]}"]`);
        if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      els.discourseList.appendChild(btn);
    });
  });
}

function clearTermHighlight() {
  document.querySelectorAll('.verse-cell[data-col="ESV"] .verse-text').forEach((el) => {
    renderEsvCellText(el);
  });
}

// Highlights every match of `regex` in the given ESV verses (on top of any
// discourse-marker highlighting already there) and scrolls to the first one.
// Shared by Top Terms and Key Terms (glossary) clicks.
function applyHighlightRegex(regex, verseNumbers) {
  verseNumbers.forEach((num) => {
    const el = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${num}"] .verse-text`);
    if (!el) return;
    const text = el.dataset.original;
    const baseRanges = buildBaseRanges(num);
    let m;
    regex.lastIndex = 0;
    const termRanges = [];
    while ((m = regex.exec(text))) {
      termRanges.push({ start: m.index, end: m.index + m[0].length, kind: "term" });
      if (m.index === regex.lastIndex) regex.lastIndex++;
    }
    // a deliberate click-to-highlight should win over the passive tone wash
    // wherever they'd overlap, same carve-out pattern buildBaseRanges uses
    const isSentiment = (r) => r.kind === "sentiment-positive" || r.kind === "sentiment-negative";
    const other = baseRanges.filter((r) => !isSentiment(r));
    const sentiment = subtractRanges(baseRanges.filter(isSentiment), termRanges);
    const ranges = [...other, ...sentiment, ...termRanges].sort((a, b) => a.start - b.start);
    el.innerHTML = rangesToHtml(text, ranges);
  });
  const firstCell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${verseNumbers[0]}"]`);
  if (firstCell) firstCell.scrollIntoView({ behavior: "smooth", block: "center" });
}

// Toggles `rowEl` as the single active highlight row across both the Top
// Terms and Key Terms panels, so only one set of yellow marks is ever shown
// at once (on top of the always-on discourse-marker highlighting).
function setActiveHighlight(rowEl, regex, verseNumbers) {
  const wasActive = rowEl.classList.contains("active");
  document.querySelectorAll(".term-row.active, .glossary-entry.active").forEach((r) => r.classList.remove("active"));
  clearTermHighlight();
  if (!wasActive) {
    rowEl.classList.add("active");
    applyHighlightRegex(regex, verseNumbers);
  }
}

function renderGlossary(data) {
  els.glossaryList.innerHTML = "";
  setPanelCount("glossary-count", data.glossary ? data.glossary.length : 0);
  if (!data.glossary || data.glossary.length === 0) {
    els.glossaryList.innerHTML = '<p class="muted">No curated key terms matched this passage.</p>';
    return;
  }
  data.glossary.forEach((g) => {
    const entry = document.createElement("div");
    entry.className = "glossary-entry";
    entry.innerHTML = `<div class="g-term">${escapeHtml(g.term)} <span class="g-lang">(${escapeHtml(g.language)}: ${escapeHtml(g.transliteration)})</span></div><div class="g-gloss">${escapeHtml(g.gloss)}</div><div class="g-verses">vv. ${g.verses.join(", ")}</div>`;

    entry.addEventListener("click", () => {
      const pattern = g.triggers.map(escapeRegExp).join("|");
      setActiveHighlight(entry, new RegExp(`\\b(${pattern})\\b`, "gi"), g.verses);
    });

    els.glossaryList.appendChild(entry);
  });
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  const ref = els.input.value.trim();
  if (ref) loadPassage(ref);
});

document.querySelectorAll(".example").forEach((btn) => {
  btn.addEventListener("click", () => {
    els.input.value = btn.dataset.ref;
    loadPassage(btn.dataset.ref);
  });
});

window.addEventListener("load", () => {
  if (location.hash) {
    const ref = decodeURIComponent(location.hash.slice(1));
    els.input.value = ref;
    loadPassage(ref);
  } else {
    showState("empty");
  }
});

// Browser back/forward changes location.hash without re-running our own
// load logic — without this, the address bar updates but the passage on
// screen doesn't. Guarded by currentRef so our own loadPassage()-driven hash
// writes (which already loaded the content) don't trigger a redundant fetch.
window.addEventListener("hashchange", () => {
  const ref = decodeURIComponent(location.hash.slice(1));
  if (!ref || ref === currentRef) return;
  els.input.value = ref;
  loadPassage(ref);
});
