"""Offline French briefs without a language model: extractive summary + machine translation.

The summary is made of sentences taken from the articles themselves (nothing is
written that the sources do not say), ranked by word frequency and position.
English text is then translated into French by an Argos Translate model
(OpenNMT, MIT/CC0 licensed), run with CTranslate2 on the CPU: no account, no
API, no network once the model is installed (~100 MB, a few seconds per event).

    pip install ctranslate2 sentencepiece
    python -m app.pipeline translator install
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path

from app.config import DATA_DIR
from app.pipeline.text import STOPWORDS, detect_language, sentences

MODELS_DIR = DATA_DIR / "models"
INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
MODEL_NAME = "translate-en_fr"
_STOP = set().union(*STOPWORDS.values())
_WORD = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'’-]*")


class TranslatorMissing(RuntimeError):
    pass


# ---------------------------------------------------------------------- translation
def model_dir(root: Path = MODELS_DIR) -> Path:
    return root / MODEL_NAME


def _package_root(path: Path) -> Path | None:
    """The folder holding model/ (CTranslate2) and the SentencePiece file, wherever the zip put it."""
    for cand in [path, *sorted(p for p in path.rglob("*") if p.is_dir())]:
        if (cand / "model" / "model.bin").exists():
            return cand
    return None


def install(root: Path = MODELS_DIR, log=print) -> Path:  # pragma: no cover - network
    import httpx  # noqa: PLC0415

    log("reading the Argos package index…")
    index = httpx.get(INDEX_URL, timeout=30, follow_redirects=True).json()
    pkg = next(p for p in index if p.get("from_code") == "en" and p.get("to_code") == "fr")
    url = pkg["links"][0]
    log(f"downloading {url} (about 100 MB)…")
    res = httpx.get(url, timeout=600, follow_redirects=True)
    res.raise_for_status()
    target = model_dir(root)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    zipfile.ZipFile(io.BytesIO(res.content)).extractall(target)
    if _package_root(target) is None:
        raise TranslatorMissing(f"unexpected package layout in {url}")
    (target / "source.json").write_text(json.dumps(pkg, indent=1))
    log(f"installed in {target}")
    return target


def detokenize(pieces: list[str]) -> str:
    """Join SentencePiece pieces the way Argos does: "▁" marks the start of a word."""
    text = "".join(pieces).replace("\u2581", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?)»])", r"\1", text).replace("« ", "« ").replace("( ", "(")


class Translator:
    """English → French with CTranslate2 + SentencePiece (the Argos model files)."""

    def __init__(self, root: Path = MODELS_DIR, device: str = "cpu"):
        pkg = _package_root(model_dir(root)) if model_dir(root).exists() else None
        if pkg is None:
            raise TranslatorMissing("translation model not installed: run `python -m app.pipeline translator install`")
        try:
            import ctranslate2  # noqa: PLC0415
            import sentencepiece  # noqa: PLC0415
        except ImportError as exc:
            raise TranslatorMissing("missing packages: run `pip install ctranslate2 sentencepiece`") from exc
        spm = pkg / "sentencepiece.model"
        if not spm.exists():
            found = [p for p in pkg.glob("*.model")]
            if not found:
                raise TranslatorMissing(f"no SentencePiece model in {pkg}")
            spm = found[0]
        self.sp = sentencepiece.SentencePieceProcessor(model_file=str(spm))
        self.tr = ctranslate2.Translator(str(pkg / "model"), device=device, inter_threads=1, intra_threads=0)
        self.name = "argos-en_fr-" + json.loads((model_dir(root) / "source.json").read_text()).get("package_version", "?") \
            if (model_dir(root) / "source.json").exists() else "argos-en_fr"

    def translate(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        tokens = [self.sp.encode(t, out_type=str) for t in texts]
        res = self.tr.translate_batch(tokens, beam_size=4, max_batch_size=16, replace_unknowns=True,
                                      max_decoding_length=256)
        return [detokenize(r.hypotheses[0]) for r in res]


# ---------------------------------------------------------------------- extractive summary
_ABBR = re.compile(r"\b(?:[A-Z]\.){2,}|\b(?:Mr|Mrs|Ms|Dr|St|Gen|Lt|Col|Adm|Sgt|Inc|Corp|Ltd|Co|No|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|vs|M|Mme|MM|av|env|cf|etc)\.")
_DATELINE = re.compile(r"^[A-ZÀ-Ý][A-ZÀ-Ý .,'’-]{2,40}\s+[—–-]\s+")
_MARK = "\u2060"


def split_sentences(text: str) -> list[str]:
    """Sentences, without breaking on abbreviations (U.S., Gen., M.) and without datelines."""
    protected = _ABBR.sub(lambda m: m.group(0).replace(".", _MARK), text)
    out = []
    for s in sentences(protected):
        s = _DATELINE.sub("", s.replace(_MARK, ".")).strip()
        if s:
            out.append(s)
    return out


def _words(s: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(s) if w.lower() not in _STOP and len(w) > 2]


def _usable(s: str) -> bool:
    return 40 <= len(s) <= 400 and not re.search(r"(cookie|subscribe|newsletter|abonnez|©|all rights reserved|read more|lire aussi)", s, re.I)


def rank_sentences(texts: list[str], title: str = "") -> list[tuple[float, int, str]]:
    """(score, position, sentence) for every usable sentence. Score = mean frequency of its content words
    across all texts, with a bonus for words of the title and for the opening of the article."""
    sents = []
    for t in texts:
        for i, s in enumerate(split_sentences(t)):
            if _usable(s):
                sents.append((i, s))
    freq = Counter(w for _, s in sents for w in set(_words(s)))
    title_words = set(_words(title))
    out, seen = [], set()
    for pos, s in sents:
        ws = _words(s)
        if not ws:
            continue
        key = " ".join(ws[:8])
        if key in seen:
            continue
        seen.add(key)
        score = sum(freq[w] for w in ws) / (len(ws) ** 0.7) + 1.5 * len(title_words & set(ws)) + (2.0 if pos < 2 else 0.0)
        out.append((score, pos, s))
    return out


def extract(texts: list[str], title: str, n_summary: int = 3, n_facts: int = 3) -> tuple[list[str], list[str]]:
    """Summary sentences (in reading order of the main text) and fact sentences (with figures or names)."""
    ranked = rank_sentences(texts, title)
    best = sorted(ranked, key=lambda r: -r[0])
    summary = [s for _, _, s in best[:n_summary]]
    order = {s: i for i, s in enumerate(s for t in texts for s in split_sentences(t))}
    summary.sort(key=lambda s: order.get(s, 1e9))
    facts = [s for _, _, s in best[n_summary:] if re.search(r"\d|[A-Z][a-z]+ [A-Z]", s)][:n_facts]
    return summary, facts


class LocalWriter:
    """Builds a brief from the sources only: extracted sentences, translated when English."""

    name = "local"

    def __init__(self, translator: Translator | None = None):
        self.translator = translator or Translator()
        self.model = getattr(self.translator, "name", "argos-en_fr")

    def _fr(self, texts: list[str], lang: str) -> list[str]:
        if lang == "fr":
            return texts
        if lang not in ("en", "und"):
            return texts                     # no model for this language: kept as written
        return self.translator.translate(texts)

    def brief(self, ev, heads: list, pages: dict[str, str]) -> dict:
        lead = heads[0]
        texts = [pages.get(h["id"]) or h["cleaned_content"] or h["summary"] or "" for h in heads]
        texts = [t for t in texts if t.strip()]
        summary, facts = extract(texts, lead["title"])
        if not summary:
            summary = [lead["title"]]
        sample = " ".join(summary)
        lang = detect_language(sample)[0]
        if lang == "und":
            lang = lead["language"] or "en"
        title_lang = lead["language"] or detect_language(lead["title"])[0]
        title = self._fr([lead["title"]], title_lang)[0]
        out = self._fr(summary + facts, lang)
        return {"titre": title, "resume": " ".join(out[: len(summary)]), "points_cles": out[len(summary):],
                "enjeux": [], "contexte": "", "a_surveiller": [],
                "sources_limitees": sum(len(t) for t in texts) < 300, "method": "extractive",
                "translated": lang == "en"}
