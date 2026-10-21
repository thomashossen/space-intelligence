# Fixtures — FICTITIOUS DATA

Everything in this folder is invented. It exists to run the pipeline end to end without network access and to test it.

- `feeds/*.xml|atom|json`: today's feeds (5–7 October 2026), shaped like the real formats (RSS 2.0, Atom, Google News RSS,
  GDELT DOC 2.0 JSON, YouTube channel Atom, arXiv API Atom, HAL search JSON). Hand-written, with the defects the pipeline
  must survive: tracking parameters, http/https and mobile URL variants, syndicated copies, malformed XML (Thales), an
  empty feed (CNES), a missing source (NASA), invalid and future dates, mojibake, an empty entry, an unknown language,
  an off-topic article, and a numeric contradiction (4 vs 6 missions).
- `feeds/history/*.xml`: 13 weeks of generated history (`python scripts/build_history_fixtures.py`, fixed seed).
- `history_manifest.json`: ground truth for the history (which items report the same story), used by the tests to
  measure deduplication and clustering quality.

Institution and company names are real actors, as in the Brief demo; nothing described happened. Publication names
(Kestrel Space Wire, Northline Defence Monitor, Halden Industry Bulletin, Brightwater Aviation Weekly, Orbital Rumour
Mill, Atlas Procurement Letter, Orient Ledger) are invented. Every host uses the reserved `.example` TLD.
