"""Deterministic, versioned segmentation of description text.

**A pure function of the exact retrieved section text.** No randomness, no
clock, no model, no configuration read at call time -- the same string in always
produces the same segments out, on any machine, in any process. That is what
makes a `SOURCE_RANGE` locator reproducible: a reviewer re-fetches the same text
version, slices the same offsets, and gets the same passage back.

**The version is stored with every passage it produced.** Changing this
algorithm changes where boundaries fall, which changes chunk IDs and offsets --
so it is a visible version bump recorded in `evidence_passages.segmenter_version`
rather than silent drift that quietly invalidates stored citations.

**Offsets index the pre-canonicalization text.** `SectionText.text` is what the
provider returned, byte for byte, and `text_sha256` is over that. Canonicalizing
first and hashing after would make every stored offset point somewhere else.

**Boundary-aware, not word-count.** `embeddings.chunk_text` splits on word count
with no notion of structure, which is fine for an embedding and useless for a
citation: a passage that starts mid-sentence cannot be quoted. This splits on
paragraph breaks first and sentence boundaries second, and only falls back to a
hard character limit when a single sentence exceeds it -- which happens in patent
text more often than it should.
"""

import re
from dataclasses import dataclass

SEGMENTER_VERSION = "desc-seg-1"
"""Stored with every `SOURCE_RANGE` passage this module produces.

Bump it in the same commit that changes any constant or rule below. A stored
passage records which version produced it, so a reviewer can tell a citation made
under different rules from one made under these.
"""

MIN_SEGMENT_CHARS = 200
"""Below this, a segment is merged forward into the next one.

A 40-character fragment is not a citable unit of disclosure -- it is a heading, a
figure caption, or a stray line -- and supplying it as evidence would waste one
of the five passage slots the verifier gets.
"""

MAX_SEGMENT_CHARS = 2000
"""Above this, a segment is split at the last sentence boundary that fits.

The ceiling exists because the verifier receives at most five passages and has a
finite context; a 20,000-character run-on paragraph would crowd out everything
else. Splitting at a sentence boundary keeps every segment quotable.
"""

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[(])")


@dataclass(frozen=True, slots=True)
class Segment:
    """One span of description text, with the offsets that locate it.

    `text` is always exactly ``section_text[start:end]``. The invariant is
    asserted by `tests/test_evidence_passages.py`, because a locator whose
    offsets do not reproduce its own text is a fabricated citation with a
    plausible-looking address.
    """

    start: int
    end: int
    text: str

    @property
    def length(self) -> int:
        return self.end - self.start


def _split_long(text: str, offset: int) -> list[Segment]:
    """Split an over-long span at sentence boundaries, never mid-sentence.

    Falls back to a hard cut only when a single sentence is itself longer than
    the ceiling -- rare, and preferable to emitting a segment nothing can hold.
    """
    segments: list[Segment] = []
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        if len(remaining) <= MAX_SEGMENT_CHARS:
            segments.append(
                Segment(offset + cursor, offset + cursor + len(remaining), remaining)
            )
            break

        window = remaining[:MAX_SEGMENT_CHARS]
        boundaries = list(_SENTENCE_END.finditer(window))
        cut = boundaries[-1].start() if boundaries else MAX_SEGMENT_CHARS
        chunk = remaining[:cut].rstrip()
        if not chunk:  # pragma: no cover - only if the window is all whitespace
            cut = MAX_SEGMENT_CHARS
            chunk = remaining[:cut]
        segments.append(Segment(offset + cursor, offset + cursor + len(chunk), chunk))
        cursor += cut
        # Skip the whitespace the boundary consumed, keeping offsets exact.
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    return segments


def segment_description(section_text: str) -> list[Segment]:
    """Split description text into citable segments.

    Paragraph breaks first, because a patent description is written in numbered
    paragraphs and those are the units a reader cites. Then the size rules: merge
    what is too small to cite, split what is too large to supply.

    Returns an empty list for empty or whitespace-only text -- **not** a single
    empty segment. A publication whose description is blank has no producible
    passage, and saying so is what `eligibility.py` needs to hear.
    """
    if not section_text or not section_text.strip():
        return []

    # Paragraph spans, with offsets into the original string.
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(section_text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(section_text):
        spans.append((cursor, len(section_text)))

    # Trim each span to its non-whitespace extent, so an offset never points at
    # padding the reader cannot see.
    trimmed: list[Segment] = []
    for start, end in spans:
        raw = section_text[start:end]
        lead = len(raw) - len(raw.lstrip())
        tail = len(raw) - len(raw.rstrip())
        real_start, real_end = start + lead, end - tail
        if real_end <= real_start:
            continue
        trimmed.append(Segment(real_start, real_end, section_text[real_start:real_end]))

    # Merge forward anything too small to be worth citing.
    merged: list[Segment] = []
    for segment in trimmed:
        if merged and merged[-1].length < MIN_SEGMENT_CHARS:
            previous = merged.pop()
            merged.append(
                Segment(
                    previous.start,
                    segment.end,
                    section_text[previous.start : segment.end],
                )
            )
        else:
            merged.append(segment)

    # Split anything too large to supply.
    final: list[Segment] = []
    for segment in merged:
        if segment.length <= MAX_SEGMENT_CHARS:
            final.append(segment)
        else:
            final.extend(_split_long(segment.text, segment.start))
    return final
