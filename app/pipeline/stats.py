"""Activity statistics over EVENTS (never raw articles): weekly series per subject.

Subjects: companies, organisations, technologies/materials, programmes, countries
and taxonomy labels. Week 12 is the current week ([as_of − 7 d, as_of)), weeks
0-11 are the 12-week baseline. Used by novelty (frequency change) and signals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, pstdev

from app.pipeline.dates import from_iso
from app.pipeline.models import EventRec


def subjects_of(ev: EventRec, entity_types: dict[str, str]) -> list[tuple[str, str]]:
    out = [(entity_types.get(e, "entity"), e) for e in ev.entity_ids]
    out += [("country", c) for c in ev.countries]
    out += [("topic", t) for t in ev.sectors + ev.topics]
    return out


def week_index(dt: datetime, as_of: datetime, weeks: int) -> int | None:
    delta = as_of - dt
    if delta < timedelta(0):
        return weeks - 1
    k = int(delta.total_seconds() // (7 * 86400))
    return weeks - 1 - k if k < weeks else None


@dataclass
class SeriesStats:
    series: list[int]
    baseline_mean: float
    baseline_std: float
    current: int
    z: float
    growth: float | None
    rolling_4w: float
    acceleration: float
    p_high: float          # Poisson P(X ≥ current | baseline mean)
    p_low: float           # Poisson P(X ≤ current | baseline mean)


def poisson_cdf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def series_stats(series: list[int]) -> SeriesStats:
    base, current = series[:-1], series[-1]
    m = mean(base) if base else 0.0
    sd = pstdev(base) if len(base) > 1 else 0.0
    floor = max(sd, math.sqrt(m), 0.5)       # Poisson floor: sparse series cannot produce huge z
    z = (current - m) / floor
    growth = (current - m) / m if m > 0 else None
    rolling = mean(series[-4:])
    accel = (mean(series[-2:]) - mean(series[-4:-2])) if len(series) >= 4 else 0.0
    p_high = 1.0 - poisson_cdf(current - 1, m) if current > 0 else 1.0
    p_low = poisson_cdf(current, m)
    return SeriesStats(series, round(m, 3), round(sd, 3), current, round(z, 2),
                       round(growth, 3) if growth is not None else None, round(rolling, 2),
                       round(accel, 2), round(p_high, 4), round(p_low, 4))


class Activity:
    def __init__(self, events: list[EventRec], as_of: datetime, weeks: int, entity_types: dict[str, str]):
        self.as_of, self.weeks = as_of, weeks
        self.counts: dict[tuple[str, str], list[int]] = {}
        self.current_events: dict[tuple[str, str], list[str]] = {}
        self.events_by_subject: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self.first_seen: dict[str, str] = {}
        self.history_weeks = 0
        seen_weeks = set()
        for ev in sorted(events, key=lambda e: e.first_seen):
            for subj in subjects_of(ev, entity_types):
                self.first_seen.setdefault(subj[1], ev.first_seen)
            w = week_index(from_iso(ev.first_seen), as_of, weeks)
            if w is None:
                continue
            seen_weeks.add(w)
            for subj in set(subjects_of(ev, entity_types)):
                self.counts.setdefault(subj, [0] * weeks)[w] += 1
                self.events_by_subject.setdefault(subj, []).append((w, ev.event_id))
                if w == weeks - 1:
                    self.current_events.setdefault(subj, []).append(ev.event_id)
        # Weeks of the baseline that contain at least one event: a proxy for collected history.
        self.history_weeks = len([w for w in seen_weeks if w < weeks - 1])

    def stats(self, subject: tuple[str, str]) -> SeriesStats:
        return series_stats(self.counts.get(subject, [0] * self.weeks))
