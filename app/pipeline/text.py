"""Text cleaning and language detection (stdlib only).

raw_content is never modified: cleaning produces ``cleaned_content`` and a
``summary`` next to it. Steps: HTML → drop navigation, scripts, forms and
boilerplate blocks → keep text blocks → unescape → repair mojibake → Unicode
NFC → strip control characters → normalise whitespace → drop boilerplate lines.
Lines repeated across many items of one source (signatures, footers) are removed
by ``repeated_lines`` at batch level.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser

SKIP_TAGS = {"script", "style", "nav", "footer", "aside", "header", "form", "noscript", "iframe",
             "svg", "button", "select", "template", "object", "canvas"}
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "track", "param"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "tr", "table", "pre", "figcaption", "dd", "dt", "main"}
BOILERPLATE_ATTR = re.compile(
    r"(^|[\s_-])(nav|menu|cookie|consent|banner|share|social|related|newsletter|subscribe|advert|ads?|"
    r"promo|footer|header|sidebar|comments?|breadcrumbs?|pagination|popup|modal)([\s_-]|$)", re.I)
BOILERPLATE_LINE = re.compile(
    r"^(subscribe( now| to our newsletter)?|sign up for|accept (all )?cookies|we use cookies|share (this|on)|"
    r"read more|follow us|all rights reserved|©|copyright|advertisement|related articles?|click here|"
    r"abonnez-vous|lire aussi|partager|tous droits réservés|publicité|en savoir plus|"
    r"the post .* appeared first on)", re.I)

STOPWORDS = {
    "en": "the and of to in for is on that with as by at from this are be was has have it an will its which their said".split(),
    "fr": "le la les des du de et en un une pour que qui dans sur au aux par est sont avec ce cette ses il elle a été".split(),
    "de": "der die das und den dem des ein eine ist mit für auf von im zu nicht sich auch werden wird".split(),
    "es": "el la los las de del y en un una para que con por es se su al como más".split(),
    "it": "il lo la gli le di del della e in un una per che con è sono al nel dei".split(),
}
_SW = {lang: set(words) for lang, words in STOPWORDS.items()}
MOJIBAKE = re.compile(r"[ÃÂâ][\x80-\xbfŒœŠšŸŽžƒˆ˜–—‘-„†-•…‰‹›€™]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​﻿]")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            if tag == "br" and not self.skip:
                self.parts.append("\n")
            return
        attr_text = " ".join(v or "" for k, v in attrs if k in ("class", "id", "role", "aria-label"))
        if self.skip or tag in SKIP_TAGS or (attr_text and BOILERPLATE_ATTR.search(attr_text)):
            self.skip.append(tag)
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self.skip:
                while self.skip and self.skip.pop() != tag:
                    pass
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def fix_mojibake(text: str) -> tuple[str, bool]:
    """Repair UTF-8 decoded as Latin-1/CP1252 (``DÃ©fense`` → ``Défense``)."""
    if not MOJIBAKE.search(text):
        return text, False
    for codec in ("cp1252", "latin-1"):
        try:
            repaired = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if len(MOJIBAKE.findall(repaired)) < len(MOJIBAKE.findall(text)):
            return repaired, True
    # Mixed text: repair run by run.
    def _fix(m: re.Match) -> str:
        chunk = m.group(0)
        for codec in ("cp1252", "latin-1"):
            try:
                return chunk.encode(codec).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        return chunk
    repaired = re.sub(r"(?:[ÃÂâ][\x80-\xbfŒ-™]{1,2})+", _fix, text)
    return repaired, repaired != text


def html_to_text(raw: str) -> str:
    if "<" not in raw:
        return html.unescape(raw)
    parser = _Extractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to tag stripping
        return html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "".join(parser.parts)


def clean_text(raw: str | None) -> tuple[str, list[str]]:
    """Return (cleaned text, flags). Paragraphs separated by blank lines."""
    flags: list[str] = []
    if not raw or not raw.strip():
        return "", ["empty_content"]
    text = html_to_text(raw)
    text, fixed = fix_mojibake(text)
    if fixed:
        flags.append("encoding_repaired")
    text = unicodedata.normalize("NFC", text)
    text = CONTROL.sub("", text).replace("\xa0", " ")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if not line:
            continue
        if BOILERPLATE_LINE.match(line) and len(line) < 160:
            flags.append("boilerplate_removed")
            continue
        lines.append(line)
    cleaned = "\n\n".join(lines)
    if not cleaned:
        flags.append("empty_content")
    return cleaned, sorted(set(flags))


def clean_inline(raw: str | None) -> str:
    """Titles and short fields: one line, no HTML."""
    text, _ = clean_text(raw or "")
    return re.sub(r"\s+", " ", text).strip()


def repeated_lines(texts: list[str], min_items: int = 3, min_share: float = 0.5) -> set[str]:
    """Paragraphs present in most items of one source batch (footers, disclaimers)."""
    if len(texts) < min_items:
        return set()
    counts = Counter(p for t in texts for p in set(t.split("\n\n")) if p)
    limit = max(min_items, int(min_share * len(texts)))
    return {p for p, n in counts.items() if n >= limit and len(p) < 300}


def strip_lines(text: str, lines: set[str]) -> str:
    if not lines:
        return text
    return "\n\n".join(p for p in text.split("\n\n") if p not in lines)


SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý0-9«\"'])")


def sentences(text: str) -> list[str]:
    out = []
    for para in text.split("\n\n"):
        out += [s.strip() for s in SENTENCE.split(para) if s.strip()]
    return out


def make_summary(feed_summary: str, cleaned: str, limit: int = 400) -> tuple[str, str]:
    """Return (summary, origin). Prefer the feed's own summary, else the lead sentences."""
    base = feed_summary.strip()
    origin = "feed"
    if len(base) < 40:
        base = " ".join(sentences(cleaned)[:2])
        origin = "extracted" if base else "none"
    if len(base) > limit:
        cut = base[:limit]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        base = cut[: stop + 1] if stop > 120 else cut.rsplit(" ", 1)[0] + "…"
    return base, origin


def detect_language(text: str, min_tokens: int = 6) -> tuple[str, float]:
    """Stop-word vote over en/fr/de/es/it. Returns ("und", 0.0) when unsure."""
    tokens = re.findall(r"[a-zàâäçéèêëîïôöùûüÿñœæß]+", text.lower())
    if len(tokens) < min_tokens:
        return "und", 0.0
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _SW.items()}
    best = max(scores, key=lambda k: (scores[k], k == "en"))
    hits = scores[best]
    total = sum(scores.values())
    if hits < 2 or hits / max(1, len(tokens)) < 0.06:
        return "und", round(hits / max(1, len(tokens)), 2)
    return best, round(hits / total, 2) if total else 0.0


def normalize_language(code: str | None) -> str:
    """Feed / API language labels → ISO 639-1 ("English" → en, "fr-FR" → fr)."""
    if not code:
        return ""
    c = code.strip().lower()
    names = {"english": "en", "french": "fr", "français": "fr", "german": "de", "spanish": "es",
             "italian": "it", "polish": "pl", "dutch": "nl", "portuguese": "pt"}
    if c in names:
        return names[c]
    return re.split(r"[-_]", c)[0][:3]
