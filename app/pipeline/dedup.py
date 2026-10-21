"""DEDUPLICATE: group copies of the same document into one origin.

Levels, tried in order for each item against earlier items:
  L1 url      same canonical URL (any time)
  L1 hash     same normalised text, ≥ min_content_chars (any time)
  L2 title    word Jaccard ≥ title_jaccard or trigram Jaccard ≥ title_trigram
  L3 content  TF-IDF cosine ≥ content_cosine (or embedding cosine when enabled)
  L4 time     L2 and L3 only apply within window_hours of each other

L2 never merges two titles that carry different numbers (a correction or a
contradiction: the event stage must see both), two titles that each name something
the other does not ("Leonardo completes …" / "ArianeGroup completes …"), or two
items with the same headline but clearly different full texts (independent
coverage reusing a headline).

Each group gets ``duplicate_group_id``: it IS the "origin" counted by the proof
line and by corroboration. The group head is the publisher's own copy when there
is one (non-aggregator first), then the earliest. Every merged item records the
level, the matched item and the similarity, so the decision can be audited.
"""

from __future__ import annotations

import re
from datetime import timedelta

from app.pipeline.dates import from_iso
from app.pipeline.similarity import TfIdf, content_hash, cosine, jaccard, tokens, trigrams

NUM = re.compile(r"\d+(?:[.,]\d+)?")
PROPER = re.compile(r"\b[A-Z][\w²-]+")


def _numbers(text: str) -> set[str]:
    """Every number, including inside model names (A350 / A320, Falcon 10X / 6X)."""
    return {n.replace(",", ".") for n in NUM.findall(text)}


def proper_noun_swap(a: str, b: str) -> bool:
    """True when each title names something the other does not ("Leonardo …" vs "ArianeGroup …"):
    a substitution means another story, not a reprint. A prefix added on one side only
    ("Replay: …") is not a substitution."""
    pa, pb = set(PROPER.findall(a)), set(PROPER.findall(b))
    return bool(pa - pb) and bool(pb - pa) and bool({w for w in pa - pb if w not in ("Replay", "Video", "Watch", "Breaking", "Update")})


def deduplicate(items: list, rules: dict, tfidf: TfIdf | None = None) -> dict[str, int]:
    """Assign duplicate groups in place. Returns counts per level."""
    window = timedelta(hours=rules["window_hours"])
    order = sorted(items, key=lambda it: (it.published_at, it.tier, it.id))
    times = [from_iso(it.published_at) for it in order]
    title_tok = [set(tokens(it.title)) for it in order]
    title_tri = [trigrams(it.title) for it in order]
    title_num = [_numbers(it.title) for it in order]
    if tfidf is None:
        tfidf = TfIdf({it.id: it.cleaned_content for it in order if len(it.cleaned_content) >= rules["min_content_chars"]})

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    members: dict[str, list[str]] = {}
    long_ids = {it.id for it in order if len(it.cleaned_content) >= rules["min_content_chars"]}

    def _texts_differ(item_id: str, group: list[str]) -> bool:
        longs = [g for g in group if g in long_ids]
        return bool(longs) and max(tfidf.cosine(item_id, g) for g in longs) < rules["same_title_min_content_cosine"]

    by_url: dict[str, str] = {}
    by_hash: dict[str, str] = {}
    counts = {"L1_url": 0, "L1_hash": 0, "L2_title": 0, "L3_content": 0, "L3_embedding": 0}
    match: dict[str, tuple[str, str, float]] = {}

    for i, it in enumerate(order):
        parent[it.id] = it.id
        found = None
        if it.canonical_url and it.canonical_url in by_url:
            found = (by_url[it.canonical_url], "L1_url", 1.0)
        long_text = len(it.cleaned_content) >= rules["min_content_chars"]
        h = content_hash(it.cleaned_content) if long_text else None
        if not found and h and h in by_hash:
            found = (by_hash[h], "L1_hash", 1.0)
        if not found:
            best = None
            j = i - 1
            while j >= 0 and times[i] - times[j] <= window:
                other = order[j]
                tj, ttri = jaccard(title_tok[i], title_tok[j]), jaccard(title_tri[i], title_tri[j])
                if (min(len(title_tok[i]), len(title_tok[j])) >= 4 and title_num[i] == title_num[j]
                        and (tj >= rules["title_jaccard"] or ttri >= rules["title_trigram"])
                        and not proper_noun_swap(it.title, other.title)
                        # Same headline, different full text = independent coverage reusing a headline.
                        # Checked against every long text already in the target group, so a short
                        # copy cannot bridge two independent write-ups.
                        and not (long_text and _texts_differ(it.id, members[find(other.id)]))):
                    cand = (other.id, "L2_title", round(max(tj, ttri), 3))
                elif it.embedding and other.embedding and cosine(it.embedding, other.embedding) >= rules["embedding_cosine"]:
                    cand = (other.id, "L3_embedding", round(cosine(it.embedding, other.embedding), 3))
                elif long_text and len(other.cleaned_content) >= rules["min_content_chars"]:
                    c = tfidf.cosine(it.id, other.id)
                    cand = (other.id, "L3_content", round(c, 3)) if c >= rules["content_cosine"] else None
                else:
                    cand = None
                if cand and (best is None or cand[2] > best[2]):
                    best = cand
                j -= 1
            found = best
        if found:
            root = find(found[0])
            parent[it.id] = root
            members[root].append(it.id)
            match[it.id] = found
            counts[found[1]] += 1
        else:
            members[it.id] = [it.id]
        if it.canonical_url:
            by_url.setdefault(it.canonical_url, it.id)
        if h:
            by_hash.setdefault(h, it.id)

    groups: dict[str, list] = {}
    for it in order:
        groups.setdefault(find(it.id), []).append(it)
    for members in groups.values():
        head = min(members, key=lambda m: (m.source_type == "aggregator", m.published_at, m.tier, m.id))
        gid = "dg-" + head.id[3:]
        for m in members:
            m.duplicate_group_id = gid
            m.is_origin_head = m is head
            if m.id in match:
                m.duplicate_of, m.dedup_level, m.dedup_similarity = match[m.id]
            else:
                m.duplicate_of, m.dedup_level, m.dedup_similarity = "", "", None
            if m is not head and not m.duplicate_of:
                # The earliest item lost the head role to the publisher's own copy.
                m.duplicate_of, m.dedup_level = head.id, match.get(head.id, ("", "regrouped", None))[1]
    counts["groups"] = len(groups)
    counts["duplicates"] = sum(len(m) - 1 for m in groups.values())
    return counts
