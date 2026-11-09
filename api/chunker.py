"""Structural, PDF-aware chunking for module ingestion.

Chunks are built over the whole document as one flow (text continues across
page breaks), split at section headings and numbered room/area keys so a
room stays in one piece, and each chunk carries a `section` breadcrumb
("Chapter 2 > Level 3 > Room #9A: Living Room") for the extraction prompts.

Original notes:

Segments each page on paragraph boundaries (not a raw character count) and
tags every chunk with structural hints pulled from the PDF's actual layout —
a filled/bordered box or an italic run for read-aloud text, a cluster of
bold stat labels bounded by short lines for a stat block — so the pipeline
doesn't have to infer those from wording alone. A structurally distinct
paragraph (boxed text or a stat block) is always isolated into its own
chunk rather than merged with surrounding prose. Every chunk also carries
2-3 sentences of trailing overlap from the previous chunk so cross-paragraph
references (a trigger pointing at something mentioned earlier) survive
the split.
"""
import re

ITALIC_FLAG = 2
BOLD_FLAG = 16
MIN_CHUNK_WORDS = 150
MAX_CHUNK_WORDS = 400
OVERLAP_SENTENCES = 3
MAX_HEADING_WORDS = 14
SECTION_FLUSH_MIN_WORDS = 75   # don't emit tiny chunks at every heading
ROOM_KEY = re.compile(
    r"^\s*(?:(?:Room|Area|Encounter|Location|Chamber|Cave|Level|Site)\s*#?\s*\d+[A-Za-z]?\b"
    r"|#\d+[A-Za-z]?\b|\d{1,3}[A-Za-z]?\.\s+[A-Z])",
    re.IGNORECASE,
)
STAT_LABEL_PATTERN = re.compile(
    r"\b(AC|HP|hp|HD|MV|THAC0|STR|DEX|CON|INT|WIS|CHA|Damage|Dmg|Attack|Quality|Defense|Tough)\b"
)


def _boxed_rects(page):
    """Bounding boxes of filled rectangles that aren't the page background —
    the usual visual signature of boxed/read-aloud text in module layouts."""
    rects = []
    page_area = page.rect.width * page.rect.height
    for drawing in page.get_drawings():
        if not drawing.get("fill"):
            continue
        rect = drawing["rect"]
        area = rect.width * rect.height
        if 0 < area < 0.85 * page_area:
            rects.append(rect)
    return rects


def _rect_contains(rect, bbox) -> bool:
    x0, y0, x1, y1 = bbox
    return rect.x0 - 1 <= x0 and rect.y0 - 1 <= y0 and rect.x1 + 1 >= x1 and rect.y1 + 1 >= y1


def _paragraphs_with_spans(page):
    """Yield (text, spans) per paragraph block from the page's structured text."""
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        spans = [span for line in lines for span in line.get("spans", [])]
        text = "\n".join("".join(span["text"] for span in line.get("spans", [])) for line in lines)
        if text.strip():
            yield text, spans


def _classify_paragraph(text: str, spans: list, boxed_rects: list) -> set:
    hints = set()
    if not spans:
        return hints

    if any(_rect_contains(rect, s["bbox"]) for s in spans for rect in boxed_rects):
        hints.add("boxed")

    italic_spans = [s for s in spans if s.get("flags", 0) & ITALIC_FLAG]
    if len(italic_spans) >= max(1, len(spans) - 1):
        hints.add("italic_block")

    bold_spans = [s for s in spans if s.get("flags", 0) & BOLD_FLAG]
    line_count = text.count("\n") + 1
    stat_label_hits = len(STAT_LABEL_PATTERN.findall(text))
    if bold_spans and stat_label_hits >= 3 and line_count <= 12:
        hints.add("stat_block")

    return hints


def _sentence_tail(text: str, count: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(s for s in sentences[-count:] if s)


def _body_font_size(doc) -> float:
    """Most common font size by character count - the body text size."""
    counts = {}
    for page in doc:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []) if block.get("type") == 0 else []:
                for span in line.get("spans", []):
                    key = round(span["size"], 1)
                    counts[key] = counts.get(key, 0) + len(span["text"])
    return max(counts, key=counts.get) if counts else 10.0


def _heading_info(text: str, spans: list, body_size: float):
    """Return ("heading", size) / ("room", None) / None for a paragraph."""
    first_line = text.strip().split("\n")[0].strip()
    words = len(first_line.split())
    if not spans or words == 0 or words > MAX_HEADING_WORDS:
        return None
    size = max(s["size"] for s in spans)
    all_bold = all(s.get("flags", 0) & BOLD_FLAG for s in spans if s["text"].strip())
    short = text.count("\n") <= 1 and not first_line.endswith((".", ",", ";"))
    if size >= body_size * 1.15 and short:
        return ("heading", size) if not ROOM_KEY.match(first_line) else ("room", size)
    if ROOM_KEY.match(first_line) and (all_bold or size >= body_size * 1.05 or words <= 8):
        return ("room", None)
    return None


def _running_headers(doc) -> set:
    """Short paragraphs that repeat on many pages (page numbers, running
    titles) - noise that would otherwise be read as headings."""
    counts = {}
    for page in doc:
        seen = set()
        for text, _ in _paragraphs_with_spans(page):
            norm = re.sub(r"\d+", "#", " ".join(text.split()).lower())
            if len(norm.split()) <= 6 and norm not in seen:
                seen.add(norm)
                counts[norm] = counts.get(norm, 0) + 1
    threshold = max(4, len(doc) // 5)
    return {k for k, v in counts.items() if v >= threshold}


def chunk_document(doc):
    """Yield {"text", "structural_hints", "section", "page"} chunks across the
    whole document, with 2-3 sentences of overlap from the previous chunk."""
    body_size = _body_font_size(doc)
    noise = _running_headers(doc)
    heading_sizes = []   # distinct heading sizes seen, largest first -> levels
    stack = []           # [(level, title)]
    room = None
    chunks = []
    buf, buf_hints, buf_words, buf_section, buf_page = [], set(), 0, "", 0

    def section_label():
        parts = [t for _, t in stack][-3:] + ([room] if room else [])
        return " > ".join(parts)

    def flush():
        nonlocal buf, buf_hints, buf_words, buf_section, buf_page
        if buf:
            chunks.append({"text": "\n\n".join(buf).strip(), "structural_hints": sorted(buf_hints),
                           "section": buf_section, "page": buf_page})
        buf, buf_hints, buf_words = [], set(), 0

    for page in doc:
        boxed_rects = _boxed_rects(page)
        for text, spans in _paragraphs_with_spans(page):
            norm = re.sub(r"\d+", "#", " ".join(text.split()).lower())
            if norm in noise or re.fullmatch(r"\W*\d+\W*", text.strip()):
                continue
            kind = _heading_info(text, spans, body_size)
            title = " ".join(text.split())
            if kind:
                if buf_words >= SECTION_FLUSH_MIN_WORDS:
                    flush()
                if kind[0] == "heading":
                    size = kind[1]
                    if not any(abs(size - h) < 0.6 for h in heading_sizes):
                        heading_sizes.append(size)
                        heading_sizes.sort(reverse=True)
                    level = next(i for i, h in enumerate(heading_sizes) if abs(size - h) < 0.6)
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, title))
                    room = None
                else:
                    room = title
            hints = _classify_paragraph(text, spans, boxed_rects)
            words = len(text.split())

            if "boxed" in hints or "stat_block" in hints:
                flush()
                chunks.append({"text": text.strip(), "structural_hints": sorted(hints),
                               "section": section_label(), "page": page.number + 1})
                continue

            if not buf:
                buf_section, buf_page = section_label(), page.number + 1
            if buf_words + words > MAX_CHUNK_WORDS and buf_words >= MIN_CHUNK_WORDS:
                flush()
                buf_section, buf_page = section_label(), page.number + 1
            buf.append(text)
            buf_hints |= hints
            buf_words += words
    flush()

    previous_tail = ""
    for chunk in chunks:
        raw = chunk["text"]
        if previous_tail:
            chunk = {**chunk, "text": f"{previous_tail}\n\n{raw}"}
        previous_tail = _sentence_tail(raw, OVERLAP_SENTENCES)
        yield chunk
