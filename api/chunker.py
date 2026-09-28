"""Structural, PDF-aware chunking for module ingestion.

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


def chunk_page(page):
    """Yield {"text", "structural_hints"} chunks for a single fitz Page."""
    boxed_rects = _boxed_rects(page)
    chunks = []
    buffer_text, buffer_hints, buffer_words = [], set(), 0

    def flush():
        nonlocal buffer_text, buffer_hints, buffer_words
        if buffer_text:
            chunks.append({"text": "\n\n".join(buffer_text).strip(), "structural_hints": sorted(buffer_hints)})
        buffer_text, buffer_hints, buffer_words = [], set(), 0

    for text, spans in _paragraphs_with_spans(page):
        hints = _classify_paragraph(text, spans, boxed_rects)
        word_count = len(text.split())

        if "boxed" in hints or "stat_block" in hints:
            flush()
            chunks.append({"text": text.strip(), "structural_hints": sorted(hints)})
            continue

        if buffer_words + word_count > MAX_CHUNK_WORDS and buffer_words >= MIN_CHUNK_WORDS:
            flush()

        buffer_text.append(text)
        buffer_hints |= hints
        buffer_words += word_count

    flush()
    return chunks


def chunk_document(doc):
    """Yield chunks across every page of a fitz Document, each carrying 2-3
    sentences of overlap from the previous chunk."""
    previous_tail = ""
    for page in doc:
        for chunk in chunk_page(page):
            text = f"{previous_tail}\n\n{chunk['text']}" if previous_tail else chunk["text"]
            previous_tail = _sentence_tail(chunk["text"], OVERLAP_SENTENCES)
            yield {"text": text, "structural_hints": chunk["structural_hints"]}
