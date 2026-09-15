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
  summaryContent: document.getElementById("summary-content"),
  summaryCount: document.getElementById("summary-count"),
  tooltip: document.getElementById("tooltip"),
  structureMinimap: document.getElementById("structure-minimap"),
  xrefMap: document.getElementById("xref-map"),
  xrefMapTitle: document.getElementById("xref-map-title"),
  xrefMapSummary: document.getElementById("xref-map-summary"),
  xrefMapBody: document.getElementById("xref-map-body"),
  xrefMapOpen: document.getElementById("xref-map-open"),
  xrefMapClose: document.getElementById("xref-map-close"),
};

// one colour per discourse-marker category, shared by the minimap, the
// in-grid section dividers, and the sidebar Sections list so all three
// read as the same system; a section with no opener marker (the passage's
// opening) gets the neutral colour
const CATEGORY_COLOR_VAR = {
  "Logical & Causal": "--cat-logical",
  "Contrastive & Adversative": "--cat-contrastive",
  "Temporal & Sequential": "--cat-temporal",
  "Focus & Attention": "--cat-focus",
};
function categoryColor(category) {
  return `var(${CATEGORY_COLOR_VAR[category] || "--cat-neutral"})`;
}
function categorySoftColor(category) {
  const base = CATEGORY_COLOR_VAR[category] || "--cat-neutral";
  return `var(${base}-bg)`;
}

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
let sections = []; // ordered [{start, end, opener}] — passage broken up at sentence-initial discourse markers
let sectionHeadings = []; // ordered [{start, end, title}] — real editorial section titles from NIV's publisher
let structureToggleOn = false;
let summaryPollTimer = null; // cleared on every render, so a poll left over from
                             // the previous passage can't write into the new one

// A verse is identified by "chapter:verse" (e.g. "2:10"), because verse numbers
// restart at every chapter boundary. `verseIds` is the passage's reading order
// as sent by the server; `versePosition` turns an id into its index, and all
// span/proportion arithmetic goes through it rather than through verse numbers.
let verseIds = [];
let versePosition = {};
let spansChapters = false;

// The passage's own shape, from the ESV's markup: which paragraph each verse
// belongs to (Crossway's paragraphing — the author's thought units, and a
// better answer to "where are the sections" than any marker heuristic), and
// where poetry lines break inside each verse.
let paragraphByVerse = {};
let esvBreaks = {};

function verseChapter(id) {
  return id.split(":")[0];
}

// Within a single chapter the chapter number is noise, so labels stay "v. 14"
// as they always were; across chapters they have to carry it: "1:20".
function verseLabel(id) {
  return spansChapters ? id : id.split(":")[1];
}

function formatRange(startId, endId) {
  return startId === endId
    ? `v. ${verseLabel(startId)}`
    : `vv. ${verseLabel(startId)}-${verseLabel(endId)}`;
}

function formatVerseList(ids) {
  return `vv. ${ids.map(verseLabel).join(", ")}`;
}

function inReadingOrder(ids) {
  return [...ids].sort((a, b) => versePosition[a] - versePosition[b]);
}

// Summaries written before multi-chapter support used bare verse numbers; a
// bare number belongs to the passage's opening chapter.
function toVerseId(value) {
  const raw = String(value);
  return raw.includes(":") ? raw : `${verseChapter(verseIds[0] || "1:1")}:${raw}`;
}

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

// Emits text[from, to) as escaped HTML with any poetry line breaks falling
// inside it. A break sits on the space separating two lines, so that space is
// consumed by the break itself. Emitting breaks during the range walk (rather
// than splitting the text first) is what lets a quotation or tone span run
// across a line break without the two fighting over the same characters.
function sliceWithBreaks(text, from, to, breaks) {
  if (!breaks || breaks.length === 0) return escapeHtml(text.slice(from, to));
  let out = "";
  let pos = from;
  breaks.forEach((b) => {
    if (b.offset < pos || b.offset >= to) return;
    out += escapeHtml(text.slice(pos, b.offset));
    out += "<br>";
    // the indented second limb of a couplet — in Hebrew poetry the indent is
    // the parallelism, not decoration
    if (b.indent > 0) out += `<span class="pline-indent"></span>`;
    pos = b.offset + 1;
  });
  return out + escapeHtml(text.slice(pos, to));
}

// Renders `text` as HTML, wrapping the given non-overlapping [start,end)
// ranges (sorted by start) each in the markup for its `kind`, and breaking
// poetry lines at `breaks`.
function rangesToHtml(text, ranges, breaks) {
  let html = "";
  let pos = 0;
  ranges.forEach((r) => {
    if (r.start < pos) return; // defensively skip any residual overlap
    html += sliceWithBreaks(text, pos, r.start, breaks);
    const inner = sliceWithBreaks(text, r.start, r.end, breaks);
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
  html += sliceWithBreaks(text, pos, text.length, breaks);
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
function buildBaseRanges(verseId) {
  const quotations = (citationByVerse[verseId] || []).map((q) => ({ ...q, kind: "quotation" }));
  const discourse = (discourseByVerse[verseId] || []).map((d) => ({ ...d, kind: "discourse" }));
  let woc = (wocByVerse[verseId] || []).map((w) => ({ ...w, kind: "woc" }));
  woc = subtractRanges(woc, quotations);
  const foreground = [...quotations, ...discourse, ...woc];

  let sentiment = sentimentToggleOn ? (sentimentByVerse[verseId] || []) : [];
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
  esvVerses.forEach((v) => { textByVerse[v.id] = v.text; });

  const map = {};
  const words = [
    ...(currentData.sentiment.positive_words || []).map((w) => ({ ...w, kind: "sentiment-positive" })),
    ...(currentData.sentiment.negative_words || []).map((w) => ({ ...w, kind: "sentiment-negative" })),
  ];
  words.forEach((entry) => {
    const re = new RegExp(`\\b${escapeRegExp(entry.word)}\\b`, "gi");
    entry.verses.forEach((id) => {
      const text = textByVerse[id];
      if (text === undefined) return;
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(text))) {
        (map[id] ||= []).push({ start: m.index, end: m.index + m[0].length, kind: entry.kind });
        if (m.index === re.lastIndex) re.lastIndex++;
      }
    });
  });
  return map;
}

// Breaks the passage into ordered sections at each verse whose earliest
// sentence-initial discourse-marker hit opens a new clause — e.g. a
// sentence-initial "But" or "Therefore" marks a real turn in the argument,
// while the same word buried mid-sentence doesn't. The first section never
// gets a divider (there's nothing before it to divide from); an `opener` of
// null there just means the passage doesn't open on a marker.
function computeSections(esvVerses, hits) {
  if (!esvVerses || esvVerses.length === 0) return [];
  const openerByVerse = {};
  hits.forEach((h) => {
    if (!h.sentence_initial) return;
    const existing = openerByVerse[h.verse];
    if (!existing || h.start < existing.start) openerByVerse[h.verse] = h;
  });
  // esvVerses already arrives in reading order, so no sort is needed
  const result = [];
  let current = null;
  esvVerses.forEach((v, idx) => {
    const opener = openerByVerse[v.id];
    if (idx === 0) {
      current = { start: v.id, end: v.id, opener: opener || null };
    } else if (opener) {
      result.push(current);
      current = { start: v.id, end: v.id, opener };
    } else {
      current.end = v.id;
    }
  });
  if (current) result.push(current);
  return result;
}

function renderEsvCellText(el) {
  const text = el.dataset.original;
  const id = el.closest(".verse-cell").dataset.verse;
  el.innerHTML = rangesToHtml(text, buildBaseRanges(id), esvBreaks[id]);
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
    loadPassage(jumpBtn.dataset.ref, { context: true });
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

async function loadPassage(ref, { context } = {}) {
  showState("loading");
  hideTooltip();
  try {
    const url = `/api/passage?ref=${encodeURIComponent(ref)}${context ? "&context=1" : ""}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "Something went wrong");
    }
    currentData = data;
    // use the response's own display form (not the raw input) so a jump that
    // widened the range to show context reloads/back-navigates to that same
    // widened range, not back to the single verse in isolation
    currentRef = data.reference.display;
    location.hash = encodeURIComponent(currentRef);
    render(data);
    showState("view");
    if (data.reference.target_verse) {
      selectVerse(data.reference.target_verse);
      const cell = document.querySelector(`.verse-cell[data-verse="${data.reference.target_verse}"]`);
      if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  } catch (err) {
    els.error.textContent = err.message;
    showState("error");
  }
}

function render(data) {
  els.title.textContent = data.reference.display;
  stopSummaryPolling();

  verseIds = data.verse_ids || [];
  versePosition = {};
  verseIds.forEach((id, i) => { versePosition[id] = i; });
  spansChapters = new Set(verseIds.map(verseChapter)).size > 1;

  paragraphByVerse = {};
  esvBreaks = {};
  (data.translations.ESV || []).forEach((v) => {
    paragraphByVerse[v.id] = v.paragraph;
    esvBreaks[v.id] = v.breaks || [];
  });

  discourseByVerse = {};
  (data.discourse_markers || []).forEach((m) => {
    (discourseByVerse[m.verse] ||= []).push({ start: m.start, end: m.end, category: m.category });
  });
  sections = computeSections(data.translations.ESV, data.discourse_markers || []);
  sectionHeadings = data.section_headings || [];

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
  renderSummary(data);
  if (xrefMapIsOpen()) renderXrefMap();
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

  if (verseIds.length === 0) return;

  // One grid row per verse, in the passage's own reading order, plus a
  // full-width divider row wherever a new chapter starts and (with structure
  // view on) wherever a section begins. Walking the ordered verse list rather
  // than counting from the lowest to the highest verse number is what makes a
  // multi-chapter passage possible at all — the numbers restart at each
  // boundary — and it also copes with a verse missing from the middle.
  const sectionAt = {}; // verse id -> the section it opens
  if (structureToggleOn && sections.length > 1) {
    sections.forEach((section, i) => { if (i > 0) sectionAt[section.start] = section; });
  }

  const rowByVerse = {};
  const dividers = []; // {row, section}
  const chapterBreaks = []; // {row, chapter}
  const paragraphGaps = []; // {row} — a blank row where the author starts a new thought
  const lastInParagraph = new Set();
  let rowCursor = 2; // row 1 is the column headers
  verseIds.forEach((id, idx) => {
    const prev = idx > 0 ? verseIds[idx - 1] : null;
    const newChapter = prev && verseChapter(id) !== verseChapter(prev);
    const newParagraph = prev && paragraphByVerse[id] !== paragraphByVerse[prev];
    if (newParagraph && prev) lastInParagraph.add(prev);

    if (newChapter) {
      chapterBreaks.push({ row: rowCursor, chapter: verseChapter(id) });
      rowCursor++;
    }
    if (sectionAt[id]) {
      dividers.push({ row: rowCursor, section: sectionAt[id] });
      rowCursor++;
    }
    // a chapter divider or section divider already separates these rows, so
    // don't stack a blank one on top of it
    if (newParagraph && !newChapter && !sectionAt[id]) {
      paragraphGaps.push({ row: rowCursor });
      rowCursor++;
    }
    rowByVerse[id] = rowCursor++;
  });
  if (verseIds.length) lastInParagraph.add(verseIds[verseIds.length - 1]);
  grid.style.gridTemplateRows = `auto repeat(${rowCursor - 2}, auto)`;
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

  paragraphGaps.forEach(({ row }) => {
    const gap = document.createElement("div");
    gap.className = "paragraph-gap";
    gap.style.gridColumn = `1 / span ${visibleColumns.length}`;
    gap.style.gridRow = row;
    grid.appendChild(gap);
  });

  chapterBreaks.forEach(({ row, chapter }) => {
    const div = document.createElement("div");
    div.className = "chapter-divider";
    div.style.gridColumn = `1 / span ${visibleColumns.length}`;
    div.style.gridRow = row;
    div.textContent = `${data.reference.book_name} ${chapter}`;
    grid.appendChild(div);
  });

  dividers.forEach(({ row, section }) => {
    const div = document.createElement("div");
    div.className = "structure-divider";
    div.style.gridColumn = `1 / span ${visibleColumns.length}`;
    div.style.gridRow = row;
    div.style.setProperty("--divider-color", categoryColor(section.opener && section.opener.category));
    div.textContent = section.opener ? `${section.opener.marker} — ${section.opener.category}` : "New section";
    grid.appendChild(div);
  });

  visibleColumns.forEach((c, colIdx) => {
    data.translations[c].forEach((v) => {
      const cell = document.createElement("div");
      cell.className = "verse-cell";
      if (v.poetry) cell.classList.add("poetry");
      // the row-separating rule sits only on the last verse of a paragraph, so
      // a paragraph reads as one block rather than a stack of boxed rows
      if (lastInParagraph.has(v.id)) cell.classList.add("paragraph-end");
      cell.dataset.col = c;
      cell.dataset.verse = v.id;
      cell.style.gridColumn = colIdx + 1;
      cell.style.gridRow = rowByVerse[v.id];
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
        // no analysis overlays on the other columns, but they carry their own
        // poetry line breaks, so a psalm reads as verse in every column
        textSpan.innerHTML = sliceWithBreaks(v.text, 0, v.text.length, v.breaks);
      }

      const notes = data.footnotes.filter((f) => f.translation === c && f.verse === v.id);
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

      cell.addEventListener("click", () => selectVerse(v.id));
      grid.appendChild(cell);
    });
  });
}

function selectVerse(id) {
  document.querySelectorAll(".verse-cell.selected").forEach((el) => el.classList.remove("selected"));
  document.querySelectorAll(`.verse-cell[data-verse="${id}"]`).forEach((el) => el.classList.add("selected"));
  renderCrossrefs(id);
  // the sidebar scrolls internally (see .sidebar in style.css), so this
  // brings Cross References into view within the sidebar itself rather than
  // jumping the whole page and losing your place in the passage
  document.getElementById("crossref-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderCrossrefs(id) {
  const refs = currentData.cross_references[id];
  if (!refs || refs.length === 0) {
    els.crossrefList.innerHTML = `<p class="muted">No cross-references found for verse ${escapeHtml(verseLabel(id))}.</p>`;
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
      if (r.refs[0]) loadPassage(r.refs[0], { context: true });
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
    verses.textContent = formatVerseList(term.verses);
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
    verses.textContent = formatVerseList(entry.verses);
    row.appendChild(verses);

    row.addEventListener("click", () => {
      setActiveHighlight(row, new RegExp(`\\b(${escapeRegExp(entry.word)})\\b`, "gi"), entry.verses);
    });

    container.appendChild(row);
  });
}

function renderSections() {
  if (sections.length <= 1) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "No structural breaks detected in this passage.";
    els.discourseList.appendChild(note);
    return;
  }
  const heading = document.createElement("div");
  heading.className = "discourse-category";
  heading.textContent = "Sections";
  els.discourseList.appendChild(heading);

  sections.forEach((section) => {
    const label = formatRange(section.start, section.end);
    const swatch = `<span class="minimap-swatch" style="background:${categoryColor(section.opener && section.opener.category)}"></span>`;
    const btn = document.createElement("button");
    btn.className = "discourse-item";
    btn.innerHTML = section.opener
      ? `<span class="d-marker">${swatch}${escapeHtml(section.opener.marker.toLowerCase())} <span class="muted">(${escapeHtml(section.opener.category)})</span></span><span class="d-verse">${label}</span>`
      : `<span class="d-marker">${swatch}Opening</span><span class="d-verse">${label}</span>`;
    btn.addEventListener("click", () => {
      selectVerse(section.start);
      const cell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${section.start}"]`);
      if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    els.discourseList.appendChild(btn);
  });
}

function renderDiscourseMarkers(data) {
  els.discourseList.innerHTML = "";
  const hits = data.discourse_markers || [];
  if (hits.length === 0) {
    els.discourseList.innerHTML = '<p class="muted">No discourse markers found in this passage.</p>';
    setPanelCount("discourse-count", 0);
    renderMinimap();
    return;
  }

  const toggleRow = document.createElement("label");
  toggleRow.className = "toggle-row";
  const toggleInput = document.createElement("input");
  toggleInput.type = "checkbox";
  toggleInput.checked = structureToggleOn;
  toggleInput.addEventListener("change", () => {
    structureToggleOn = toggleInput.checked;
    renderGrid(currentData);
    renderDiscourseMarkers(currentData);
  });
  toggleRow.appendChild(toggleInput);
  toggleRow.append("Show passage structure");
  els.discourseList.appendChild(toggleRow);

  if (structureToggleOn) renderSections();

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
      const verses = inReadingOrder(group.verses);
      const btn = document.createElement("button");
      btn.className = "discourse-item";
      btn.innerHTML = `<span class="d-marker">${escapeHtml(group.marker)}</span><span class="d-verse">${formatVerseList(verses)}</span>`;
      btn.addEventListener("click", () => {
        selectVerse(verses[0]);
        const cell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${verses[0]}"]`);
        if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      els.discourseList.appendChild(btn);
    });
  });

  renderMinimap();
}

// Proportional, colour-coded overview bar above the reading grid — shows
// the whole passage's shape at a glance (which sections it breaks into,
// by category colour) without reading every divider. Hover a segment for
// detail (native title tooltip, kept deliberately plain rather than the
// custom tooltip component so it doesn't need its own dismiss wiring);
// click to jump, same as the Sections list and the in-grid dividers.
// One proportional segment shared by both minimap rows: verse range on
// top, a coloured bar, a label underneath. `range` is {start, end, color,
// softColor, title, label}; clicking jumps to `range.start` same as the
// Sections list and the in-grid dividers.
function buildMinimapSegment(range) {
  // proportional by how many verses the range actually covers, measured as a
  // distance in the passage's reading order — subtracting verse numbers would
  // be meaningless across a chapter boundary
  const span = versePosition[range.end] - versePosition[range.start] + 1;
  const segment = document.createElement("div");
  segment.className = "minimap-segment";
  segment.style.flexGrow = span; // proportional by verse count, not pixels
  segment.style.setProperty("--minimap-color", range.color);
  segment.style.setProperty("--minimap-bg", range.softColor);
  segment.title = range.title;
  segment.addEventListener("click", () => {
    selectVerse(range.start);
    const cell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${range.start}"]`);
    if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  const rangeEl = document.createElement("div");
  rangeEl.className = "minimap-range";
  rangeEl.textContent = formatRange(range.start, range.end);
  segment.appendChild(rangeEl);

  const bar = document.createElement("div");
  bar.className = "minimap-bar";
  segment.appendChild(bar);

  const label = document.createElement("div");
  label.className = "minimap-marker";
  label.textContent = range.label;
  segment.appendChild(label);

  return segment;
}

function renderMinimap() {
  const el = els.structureMinimap;
  el.innerHTML = "";
  const hasHeadings = sectionHeadings.length > 0;
  const hasSections = sections.length > 1;
  if (!structureToggleOn || (!hasHeadings && !hasSections)) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");

  if (hasHeadings) {
    const rowLabel = document.createElement("div");
    rowLabel.className = "minimap-row-label";
    rowLabel.textContent = "Section headings (NIV)";
    el.appendChild(rowLabel);

    const track = document.createElement("div");
    track.className = "minimap-track";
    sectionHeadings.forEach((heading) => {
      const rangeLabel = formatRange(heading.start, heading.end);
      track.appendChild(buildMinimapSegment({
        start: heading.start,
        end: heading.end,
        color: "var(--accent)",
        softColor: "var(--accent-soft)",
        title: `${rangeLabel} — ${heading.title}`,
        label: heading.title,
      }));
    });
    el.appendChild(track);
  }

  if (hasSections) {
    const rowLabel = document.createElement("div");
    rowLabel.className = "minimap-row-label";
    rowLabel.textContent = hasHeadings ? "Discourse turns (ESV)" : "Passage structure (ESV)";
    if (hasHeadings) rowLabel.style.marginTop = "0.6rem";
    el.appendChild(rowLabel);

    const track = document.createElement("div");
    track.className = "minimap-track";
    const categoriesSeen = new Map(); // category -> color, in first-seen order (for the legend)

    sections.forEach((section) => {
      const category = section.opener && section.opener.category;
      const color = categoryColor(category);
      const rangeLabel = formatRange(section.start, section.end);
      track.appendChild(buildMinimapSegment({
        start: section.start,
        end: section.end,
        color,
        softColor: categorySoftColor(category),
        title: section.opener
          ? `${rangeLabel} — ${section.opener.marker} (${section.opener.category})`
          : `${rangeLabel} — opening`,
        label: section.opener ? section.opener.marker : "Opening",
      }));
      if (section.opener && !categoriesSeen.has(section.opener.category)) {
        categoriesSeen.set(section.opener.category, color);
      }
    });
    el.appendChild(track);

    const legend = document.createElement("div");
    legend.className = "minimap-legend";
    categoriesSeen.forEach((color, category) => {
      const item = document.createElement("span");
      item.className = "minimap-legend-item";
      item.innerHTML = `<span class="minimap-swatch" style="background:${color}"></span>${escapeHtml(category)}`;
      legend.appendChild(item);
    });
    el.appendChild(legend);
  }
}

function clearTermHighlight() {
  document.querySelectorAll('.verse-cell[data-col="ESV"] .verse-text').forEach((el) => {
    renderEsvCellText(el);
  });
}

// Highlights every match of `regex` in the given ESV verses (on top of any
// discourse-marker highlighting already there) and scrolls to the first one.
// Shared by Top Terms and Key Terms (glossary) clicks.
function applyHighlightRegex(regex, verseIdList) {
  verseIdList.forEach((id) => {
    const el = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${id}"] .verse-text`);
    if (!el) return;
    const text = el.dataset.original;
    const baseRanges = buildBaseRanges(id);
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
    el.innerHTML = rangesToHtml(text, ranges, esvBreaks[id]);
  });
  const firstCell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${verseIdList[0]}"]`);
  if (firstCell) firstCell.scrollIntoView({ behavior: "smooth", block: "center" });
}

// Toggles `rowEl` as the single active highlight row across both the Top
// Terms and Key Terms panels, so only one set of yellow marks is ever shown
// at once (on top of the always-on discourse-marker highlighting).
function setActiveHighlight(rowEl, regex, verseIdList) {
  const wasActive = rowEl.classList.contains("active");
  document.querySelectorAll(".term-row.active, .glossary-entry.active").forEach((r) => r.classList.remove("active"));
  clearTermHighlight();
  if (!wasActive) {
    rowEl.classList.add("active");
    applyHighlightRegex(regex, verseIdList);
  }
}

// --- Summary panel ----------------------------------------------------------
// The one panel that isn't computed locally: it's written by Claude from the
// metadata every other panel already shows. With no ANTHROPIC_API_KEY set, the
// server answers Run by writing data/summary_requests/<KEY>.json and a Claude
// Code session fills it in with /summarise — so after pressing Run we poll for
// the answer to land rather than blocking on a response.

const SUMMARY_POLL_MS = 3000;
const SUMMARY_POLL_GIVE_UP_MS = 10 * 60 * 1000;

function stopSummaryPolling() {
  if (summaryPollTimer) {
    clearInterval(summaryPollTimer);
    summaryPollTimer = null;
  }
}

function setSummaryCount(text) {
  els.summaryCount.textContent = text;
}

async function fetchSummaryStatus(ref) {
  const resp = await fetch(`/api/summary?ref=${encodeURIComponent(ref)}`);
  return resp.json();
}

function renderSummary(data) {
  stopSummaryPolling();
  const ref = data.reference.display;
  els.summaryContent.innerHTML = '<p class="muted">Checking…</p>';
  setSummaryCount("");

  fetchSummaryStatus(ref)
    .then((result) => applySummaryStatus(ref, result))
    .catch(() => {
      els.summaryContent.innerHTML = '<p class="muted">Could not reach the summary service.</p>';
    });
}

function applySummaryStatus(ref, result) {
  if (ref !== currentRef) return; // a different passage loaded while we were waiting
  if (result.status === "ready") {
    stopSummaryPolling();
    renderSummaryContent(result.summary);
  } else if (result.status === "pending") {
    renderSummaryPending(ref, result.request_file);
  } else {
    renderSummaryRunPrompt(ref);
  }
}

function renderSummaryRunPrompt(ref) {
  setSummaryCount("not run");
  els.summaryContent.innerHTML = "";

  const btn = document.createElement("button");
  btn.className = "summary-run";
  btn.textContent = "Run summary";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Requesting…";
    try {
      const resp = await fetch(`/api/summary?ref=${encodeURIComponent(ref)}`, { method: "POST" });
      const result = await resp.json();
      if (!resp.ok) throw new Error(result.error || "Could not request a summary");
      applySummaryStatus(ref, result);
    } catch (err) {
      els.summaryContent.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
    }
  });
  els.summaryContent.appendChild(btn);

  const note = document.createElement("p");
  note.className = "summary-note";
  note.innerHTML =
    'Costs nothing until you press it. Without an <code>ANTHROPIC_API_KEY</code> this writes a request ' +
    'to disk for you to answer with <code>/summarise</code> in Claude Code.';
  els.summaryContent.appendChild(note);
}

function renderSummaryPending(ref, requestFile) {
  setSummaryCount("waiting");
  const fileNote = requestFile
    ? `<p class="summary-note">Request written to <code>${escapeHtml(requestFile)}</code>.</p>`
    : "";
  els.summaryContent.innerHTML =
    '<p class="summary-waiting">Waiting for Claude…</p>' +
    fileNote +
    '<p class="summary-note">Run <code>/summarise</code> in Claude Code. This panel fills itself in — no need to reload.</p>';

  if (summaryPollTimer) return;
  const startedAt = Date.now();
  summaryPollTimer = setInterval(async () => {
    if (ref !== currentRef) {
      stopSummaryPolling();
      return;
    }
    if (Date.now() - startedAt > SUMMARY_POLL_GIVE_UP_MS) {
      stopSummaryPolling();
      setSummaryCount("waiting");
      els.summaryContent.innerHTML =
        '<p class="summary-note">Still waiting. Run <code>/summarise</code> in Claude Code, then search this passage again.</p>';
      return;
    }
    try {
      const result = await fetchSummaryStatus(ref);
      if (result.status === "ready") applySummaryStatus(ref, result);
    } catch {
      // a failed poll is not worth surfacing — the next one is 3s away
    }
  }, SUMMARY_POLL_MS);
}

function summaryBlock(title) {
  const block = document.createElement("div");
  block.className = "summary-block";
  const heading = document.createElement("div");
  heading.className = "discourse-category";
  heading.textContent = title;
  block.appendChild(heading);
  return block;
}

function jumpToVerse(id) {
  selectVerse(id);
  const cell = document.querySelector(`.verse-cell[data-col="ESV"][data-verse="${id}"]`);
  if (cell) cell.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderSummaryContent(summary) {
  stopSummaryPolling();
  setSummaryCount("ready");
  els.summaryContent.innerHTML = "";

  // Purpose first: these are the point of the panel, so they shouldn't be
  // three scrolls down past the section list.
  if ((summary.purpose || []).length) {
    const block = summaryBlock("Why this was written");
    summary.purpose.forEach((entry) => {
      // summaries written before purpose statements carried their verses are
      // plain strings; they still render, just without the citation
      const statement = typeof entry === "string" ? entry : entry.statement;
      const verses = (typeof entry === "string" ? [] : entry.verses || []).map(toVerseId);

      const p = document.createElement("p");
      p.className = "summary-purpose";
      p.textContent = statement;

      // These are the most interpretive lines in the panel, so they carry the
      // verses they rest on and each one jumps to its verse — the claim is
      // checkable against the text rather than taken on trust.
      if (verses.length) {
        const refs = document.createElement("span");
        refs.className = "summary-purpose-refs";
        refs.append(verses.length === 1 ? "v. " : "vv. ");
        verses.forEach((id, i) => {
          if (i > 0) refs.append(", ");
          const btn = document.createElement("button");
          btn.className = "summary-purpose-ref";
          btn.textContent = verseLabel(id);
          btn.title = `Go to ${id}`;
          btn.addEventListener("click", () => jumpToVerse(id));
          refs.appendChild(btn);
        });
        p.appendChild(refs);
      }
      block.appendChild(p);
    });
    els.summaryContent.appendChild(block);
  }

  if ((summary.sections || []).length) {
    const block = summaryBlock("Sections");
    summary.sections.forEach((s) => {
      const start = toVerseId(s.start);
      const end = toVerseId(s.end);
      const btn = document.createElement("button");
      btn.className = "summary-section";
      btn.innerHTML =
        `<span class="s-head"><span class="s-title">${escapeHtml(s.title)}</span><span class="d-verse">${formatRange(start, end)}</span></span>` +
        `<span class="s-line">${escapeHtml(s.line || "")}</span>`;
      btn.addEventListener("click", () => jumpToVerse(start));
      block.appendChild(btn);
    });
    els.summaryContent.appendChild(block);
  }

  if ((summary.themes || []).length) {
    const block = summaryBlock("Recurring themes");
    summary.themes.forEach((t) => {
      const verses = (t.verses || []).length ? formatVerseList(t.verses.map(toVerseId)) : "";
      const entry = document.createElement("div");
      entry.className = "summary-theme";
      entry.innerHTML =
        `<div class="s-head"><span class="s-title">${escapeHtml(t.theme)}</span><span class="d-verse">${verses}</span></div>` +
        `<div class="s-line">${escapeHtml(t.note || "")}</div>`;
      // same click-to-highlight contract as the glossary entries, driven by
      // the theme's triggers rather than a curated term's
      const triggers = (t.triggers || []).filter(Boolean);
      if (triggers.length) {
        entry.classList.add("clickable");
        entry.addEventListener("click", () => {
          const pattern = triggers.map(escapeRegExp).join("|");
          setActiveHighlight(entry, new RegExp(`\\b(${pattern})\\b`, "gi"), (t.verses || []).map(toVerseId));
        });
      }
      block.appendChild(entry);
    });
    els.summaryContent.appendChild(block);
  }

  if ((summary.references || []).length) {
    const block = summaryBlock("Scripture it reaches for");
    summary.references.forEach((r) => {
      // same badge as the Cross References panel, so a quotation reads as the
      // same kind of thing in both places
      const badge = r.kind === "quotation"
        ? '<span class="cr-quote-badge">Quotation</span>'
        : '<span class="cr-quote-badge cr-badge-parallel">Parallel</span>';
      const verses = (r.verses || []).length ? `at ${formatVerseList(r.verses.map(toVerseId))}` : "";
      const entry = document.createElement("div");
      entry.className = "summary-reference";
      entry.innerHTML =
        `<div class="s-head"><span class="s-title">${escapeHtml(r.ref)}${badge}</span><span class="d-verse">${verses}</span></div>` +
        `<div class="s-line">${escapeHtml(r.why || "")}</div>`;
      if ((r.verses || []).length) {
        entry.classList.add("clickable");
        entry.addEventListener("click", () => jumpToVerse(toVerseId(r.verses[0])));
      }
      block.appendChild(entry);
    });
    els.summaryContent.appendChild(block);
  }

  const footer = document.createElement("p");
  footer.className = "summary-note summary-footer";
  footer.textContent = "Written by Claude from the analysis on this page — interpretation, not data.";
  els.summaryContent.appendChild(footer);
}

// --- Cross-reference map ----------------------------------------------------
// The Cross References panel answers "what does this verse connect to", one
// verse at a time. It cannot answer "what does this passage draw on" — that
// Ephesians 1 reaches for Colossians 21 times, or that Romans 9-11 leans on
// Isaiah 20 times with most of its direct quotations among them. This is that
// view: one strip per cited book, positioned across the passage, so weight and
// placement read at once.

function xrefMapIsOpen() {
  return els.xrefMap.classList.contains("open");
}

function openXrefMap() {
  renderXrefMap();
  els.xrefMap.classList.add("open");
  els.xrefMap.setAttribute("aria-hidden", "false");
  els.xrefMapClose.focus();
}

function closeXrefMap() {
  els.xrefMap.classList.remove("open");
  els.xrefMap.setAttribute("aria-hidden", "true");
}

// Where a verse sits along the passage, 0-1. Used for the left offset of every
// mark, so the strips line up with each other and with the reading order.
function passageFraction(verseIdValue) {
  const span = Math.max(1, verseIds.length - 1);
  return (versePosition[verseIdValue] || 0) / span;
}

function buildXrefStrip(group) {
  const strip = document.createElement("div");
  strip.className = "xref-strip";
  // quotations last so they paint over parallels at the same position
  [...group.links]
    .sort((a, b) => Number(a.is_quotation) - Number(b.is_quotation))
    .forEach((link) => {
      const mark = document.createElement("span");
      mark.className = link.is_quotation ? "xref-mark xref-mark-quote" : "xref-mark";
      mark.style.left = `${passageFraction(link.verse) * 100}%`;
      mark.title = `${verseLabel(link.verse)} → ${link.ref}${link.is_quotation ? " (quotation)" : ""}`;
      mark.addEventListener("click", (e) => {
        e.stopPropagation();
        jumpToVerse(link.verse);
      });
      strip.appendChild(mark);
    });
  return strip;
}

function buildXrefLinkList(group) {
  const list = document.createElement("div");
  list.className = "xref-links";
  [...group.links]
    .sort((a, b) => versePosition[a.verse] - versePosition[b.verse])
    .forEach((link) => {
      const row = document.createElement("button");
      row.className = "xref-link";
      const badge = link.is_quotation ? '<span class="cr-quote-badge">Quotation</span>' : "";
      row.innerHTML =
        `<span class="xref-link-from">${escapeHtml(verseLabel(link.verse))}</span>` +
        `<span class="xref-link-to">${escapeHtml(link.ref)}</span>${badge}`;
      row.title = link.title;
      row.addEventListener("click", () => {
        closeXrefMap();
        loadPassage(link.ref, { context: true });
      });
      list.appendChild(row);
    });
  return list;
}

function buildXrefRuler() {
  const ruler = document.createElement("div");
  ruler.className = "xref-ruler";
  const addTick = (id, label) => {
    const tick = document.createElement("span");
    tick.className = "xref-tick";
    tick.style.left = `${passageFraction(id) * 100}%`;
    tick.textContent = label;
    ruler.appendChild(tick);
  };

  if (spansChapters) {
    let seen = null;
    verseIds.forEach((id) => {
      const chapter = verseChapter(id);
      if (chapter === seen) return;
      seen = chapter;
      addTick(id, id);
    });
  } else {
    // one chapter, so chapter starts would give a single useless tick at the
    // left edge — space verse marks across instead
    const step = Math.max(1, Math.ceil(verseIds.length / 5));
    for (let i = 0; i < verseIds.length; i += step) {
      addTick(verseIds[i], `v${verseIds[i].split(":")[1]}`);
    }
  }
  return ruler;
}

function buildXrefGroupRow(group, showQuotations) {
  const row = document.createElement("div");
  row.className = "xref-row";

  const head = document.createElement("button");
  head.className = "xref-row-head";
  head.setAttribute("aria-expanded", "false");
  const quoted = showQuotations && group.quotations > 0
    ? `<span class="xref-quote-count">${group.quotations} quoted</span>` : "";
  head.innerHTML =
    `<span class="xref-book">${escapeHtml(group.book_name)}</span>` +
    `${quoted}<span class="xref-count">${group.count}</span>`;
  row.appendChild(head);
  row.appendChild(buildXrefStrip(group));

  const links = buildXrefLinkList(group);
  links.classList.add("hidden");
  row.appendChild(links);

  head.addEventListener("click", () => {
    const collapsed = links.classList.toggle("hidden");
    head.setAttribute("aria-expanded", String(!collapsed));
    row.classList.toggle("expanded", !collapsed);
  });
  return row;
}

const XREF_VISIBLE_PER_GROUP = 8;

function buildXrefSection(title, hint, groups, showQuotations) {
  if (groups.length === 0) return null;
  const section = document.createElement("section");
  section.className = "xref-section";

  const total = groups.reduce((n, g) => n + g.count, 0);
  const heading = document.createElement("div");
  heading.className = "xref-section-head";
  heading.innerHTML =
    `<span class="xref-section-title">${escapeHtml(title)}</span>` +
    `<span class="xref-section-count">${groups.length} book${groups.length === 1 ? "" : "s"} · ${total} ref${total === 1 ? "" : "s"}</span>`;
  section.appendChild(heading);

  if (hint) {
    const note = document.createElement("p");
    note.className = "xref-section-hint";
    note.textContent = hint;
    section.appendChild(note);
  }

  groups.slice(0, XREF_VISIBLE_PER_GROUP)
    .forEach((g) => section.appendChild(buildXrefGroupRow(g, showQuotations)));

  // the tail is mostly books cited once; keep it available but out of the way
  const rest = groups.slice(XREF_VISIBLE_PER_GROUP);
  if (rest.length) {
    const more = document.createElement("div");
    more.className = "xref-more hidden";
    rest.forEach((g) => more.appendChild(buildXrefGroupRow(g, showQuotations)));
    const toggle = document.createElement("button");
    toggle.className = "xref-more-toggle";
    toggle.textContent = `Show ${rest.length} more`;
    toggle.addEventListener("click", () => {
      const collapsed = more.classList.toggle("hidden");
      toggle.textContent = collapsed ? `Show ${rest.length} more` : "Show fewer";
    });
    section.appendChild(more);
    section.appendChild(toggle);
  }
  return section;
}

function renderXrefMap() {
  const sources = (currentData && currentData.cross_reference_sources) || [];
  els.xrefMapTitle.textContent = currentData ? currentData.reference.display : "";
  els.xrefMapBody.innerHTML = "";

  if (sources.length === 0) {
    els.xrefMapSummary.textContent = "";
    els.xrefMapBody.innerHTML = '<p class="muted">No cross-references in this passage.</p>';
    return;
  }

  const total = sources.reduce((n, g) => n + g.count, 0);
  const quotations = sources.reduce((n, g) => n + g.quotations, 0);
  els.xrefMapSummary.textContent =
    `${total} references · ${quotations} direct quotation${quotations === 1 ? "" : "s"}`;

  els.xrefMapBody.appendChild(buildXrefRuler());

  // Split rather than rank together. An Old Testament reference inside a New
  // Testament passage is usually the author reaching for scripture; a New
  // Testament one is Crossway pointing at a similar passage elsewhere; and a
  // reference back into this same book is the letter talking about itself.
  // Ranking all three in one list buries the first under the other two — in
  // Ephesians 1, forty-four of the top references are Ephesians citing itself.
  const internal = sources.filter((g) => g.internal);
  const external = sources.filter((g) => !g.internal);
  const ownTestament = currentData.reference.book_id
    && (currentData.cross_reference_sources.find((g) => g.internal) || {}).testament;

  [
    buildXrefSection("Old Testament", ownTestament === "NT"
      ? "Scripture this passage reaches for." : null,
      external.filter((g) => g.testament === "OT"), true),
    buildXrefSection("New Testament", ownTestament === "OT"
      ? "Where this passage is picked up later." : null,
      external.filter((g) => g.testament === "NT"), true),
    buildXrefSection(`Within ${currentData.reference.book_name}`,
      "The book referring back to itself.", internal, false),
  ].forEach((section) => { if (section) els.xrefMapBody.appendChild(section); });
}

els.xrefMapOpen.addEventListener("click", openXrefMap);
els.xrefMapClose.addEventListener("click", closeXrefMap);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && xrefMapIsOpen()) closeXrefMap();
});

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
    entry.innerHTML = `<div class="g-term">${escapeHtml(g.term)} <span class="g-lang">(${escapeHtml(g.language)}: ${escapeHtml(g.transliteration)})</span></div><div class="g-gloss">${escapeHtml(g.gloss)}</div><div class="g-verses">${formatVerseList(g.verses)}</div>`;

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
