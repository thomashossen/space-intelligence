"""Ground truth for the fixture data: which items report the same story, which are copies.

History truth comes from data/fixtures/history_manifest.json (generated). Today's truth is
written by hand below, from the fixture files. Used to MEASURE deduplication and event
clustering quality (pairwise precision / recall), not to assert exact numbers.
"""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# story -> list of (key, role); key = guid, or URL for feeds without guids (GDELT, arXiv, HAL).
TODAY = {
    "isar": [("today-isar-esa", "origin"), ("today-isar-kestrel", "origin"), ("today-isar-rumour", "origin")],
    "a6": [("today-a6-arianespace", "origin"), ("today-a6-esa", "origin"), ("today-a6-kestrel", "origin"),
           ("today-a6-kestrel-newsletter", "copy"), ("today-a6-gn", "copy"),
           # ESA's own replay video: same publisher, same headline → not an independent origin.
           ("yt:video:fixture0001", "copy"),
           ("http://kestrel-spacewire.example/2026/10/07/ariane-6-galileo-launch?utm_source=gdelt&utm_medium=feed", "copy")],
    "vegac": [("today-vegac", "origin")],
    "mbda": [("today-mbda-minarm", "origin"), ("tag:mbda.example,2026:today-mbda-order", "origin"),
             ("today-mbda-northline", "origin"), ("today-mbda-gn", "copy")],
    "future": [("today-future", "origin")],
    "hyp": [("tag:mbda.example,2026:today-mbda-hyp", "origin"), ("today-hyp-ec", "origin"), ("today-hyp-northline", "origin")],
    "thales": [("today-thales-gm", "origin")],
    "airbus": [("today-airbus", "origin")],
    "nato": [("today-nato-cuas", "origin"), ("today-nato-northline", "origin")],
    "gallium": [("today-gallium-ec", "origin"), ("today-gallium-halden", "origin"),
                ("https://m.halden-bulletin.example/news/gallium-squeeze-radar-makers/#comments", "copy"),
                ("today-gallium-gn", "origin"),
                ("https://orient-ledger.example/business/2026/10/05/gallium-export-licensing-tightened", "origin")],
    "edip": [("today-edip", "origin")],
    "maia": [("today-maia-kestrel", "origin"), ("today-maia-halden", "origin"), ("today-maia-gn", "copy")],
    "arxiv-rl": [("http://arxiv.example/abs/2610.04321v1", "origin")],
    "arxiv-hgv": [("http://arxiv.example/abs/2610.05555v1", "origin")],
    "hal-hall": [("https://hal.example/hal-09990001", "origin")],
    "hal-rare": [("https://hal.example/hal-09990002", "origin")],
}


def truth() -> tuple[dict[str, str], dict[str, str]]:
    """Return (key -> story, key -> role)."""
    story, role = {}, {}
    for sid, members in TODAY.items():
        for key, r in members:
            story[key], role[key] = sid, r
    manifest = json.loads((ROOT / "data" / "fixtures" / "history_manifest.json").read_text(encoding="utf-8"))
    for s in manifest["stories"]:
        for guid, r in zip(s["items"], s["roles"]):
            story[guid], role[guid] = s["story"], ("copy" if r != "origin" else "origin")
    return story, role


def item_keys(con) -> dict[str, str]:
    """item id -> truth key (guid when present, else the URL as received)."""
    out = {}
    for row in con.execute("SELECT i.id, i.url, r.extra FROM items i JOIN raw_items r ON r.raw_id = i.raw_id"):
        guid = json.loads(row[2] or "{}").get("guid", "")
        out[row[0]] = guid or row[1]
    return out


def pairwise(pred: dict[str, str], gold: dict[str, str]) -> dict[str, float]:
    """Pairwise precision/recall/F1 of a clustering over the keys present in both."""
    keys = sorted(set(pred) & set(gold))
    tp = fp = fn = 0
    for a, b in combinations(keys, 2):
        same_pred, same_gold = pred[a] == pred[b], gold[a] == gold[b]
        tp += same_pred and same_gold
        fp += same_pred and not same_gold
        fn += same_gold and not same_pred
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3), "items": len(keys), "tp": tp, "fp": fp, "fn": fn}


def evaluate(con) -> dict[str, dict]:
    story, role = truth()
    keys = item_keys(con)
    rows = con.execute("SELECT id, duplicate_group_id, event_id FROM items").fetchall()
    # Events: every in-scope item with a known story.
    pred_ev = {keys[r[0]]: r[2] for r in rows if r[2] and keys[r[0]] in story}
    gold_ev = {k: story[k] for k in pred_ev}
    # Dedup: a copy must share its group with an origin of the same story; origins must not merge.
    pred_dup, gold_dup = {}, {}
    for r in rows:
        k = keys[r[0]]
        if k not in story:
            continue
        pred_dup[k] = r[1]
        gold_dup[k] = f"{story[k]}#copyset" if role[k] == "copy" else f"{story[k]}#{k}"
    # Copies belong with the first origin of their story: align the gold label with that origin.
    first_origin = {}
    for k in sorted(gold_dup):
        if role[k] == "origin":
            first_origin.setdefault(story[k], k)
    return {"events": pairwise(pred_ev, gold_ev), "dedup_copies": _copy_recall(pred_dup, story, role)}


def _copy_recall(pred_dup: dict[str, str], story: dict[str, str], role: dict[str, str]) -> dict[str, float]:
    """Copies merged into an origin of their own story (recall) and origins wrongly merged (false merges)."""
    copies = [k for k in pred_dup if role[k] == "copy"]
    ok = sum(1 for k in copies if any(pred_dup[o] == pred_dup[k] for o in pred_dup if o != k and story.get(o) == story[k] and role[o] == "origin"))
    origins = [k for k in pred_dup if role[k] == "origin"]
    groups: dict[str, list[str]] = {}
    for k in origins:
        groups.setdefault(pred_dup[k], []).append(k)
    false_merges = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
    return {"copies": len(copies), "copies_merged": ok, "copy_recall": round(ok / len(copies), 3) if copies else 1.0,
            "origins": len(origins), "origins_wrongly_merged": false_merges}
