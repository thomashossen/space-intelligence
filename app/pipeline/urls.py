"""URL canonicalisation.

``url`` keeps what the source gave us; ``canonical_url`` is the key used for
level-1 deduplication. Rules are deterministic and offline. Resolving redirects
needs the network: ``resolve_redirect`` is only called in live mode, and only for
known link shorteners (aggregator links such as Google News are flagged, not
resolved: they need JavaScript and their terms do not invite automated decoding).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src", "ref_url",
    "cmpid", "xtor", "igshid", "_ga", "_gl", "spm", "ocid", "oc", "smid",
    "at_medium", "at_campaign", "__twitter_impression", "s_cid", "icid", "wt.mc_id", "mkt_tok",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_", "at_")
SHORTENERS = {"t.co", "bit.ly", "ow.ly", "buff.ly", "tinyurl.com", "lnkd.in", "feedproxy.google.com", "dlvr.it"}
AGGREGATOR_HOSTS = ("news.google.", "news.yahoo.")
AMP_PARAMS = {"amp", "outputtype"}


def _clean_host(host: str, flags: list[str]) -> str:
    host = host.lower().rstrip(".")
    for prefix in ("www.", "m.", "mobile.", "amp."):
        if host.startswith(prefix) and host.count(".") >= 2:
            if prefix != "www.":
                flags.append(f"host_variant:{prefix.rstrip('.')}")
            host = host[len(prefix):]
            break
    return host


def canonicalize(url: str) -> tuple[str, list[str]]:
    """Return (canonical_url, flags). Never raises: a bad URL comes back flagged."""
    flags: list[str] = []
    raw = (url or "").strip()
    if not raw:
        return "", ["empty_url"]
    if raw.startswith("//"):
        raw = "https:" + raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        return raw, ["not_absolute"]
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw, ["unparseable"]
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return raw, [f"scheme:{scheme}"]
    if scheme == "http":
        flags.append("http_upgraded")
    host = _clean_host(parts.hostname or "", flags)
    if not host:
        return raw, ["no_host"]
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", unquote(parts.path or "/"))
    if re.search(r"/amp/?$", path):
        path = re.sub(r"/amp/?$", "", path) or "/"
        flags.append("amp_stripped")
    path = re.sub(r"/(index\.html?|index\.php)$", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    path = quote(path, safe="/:@!$&'()*+,;=-._~%")

    kept = []
    removed = 0
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        k = key.lower()
        if k in TRACKING_PARAMS or k.startswith(TRACKING_PREFIXES) or k in AMP_PARAMS:
            removed += 1
            continue
        kept.append((key, value))
    if removed:
        flags.append(f"tracking_removed:{removed}")
    query = urlencode(sorted(kept))
    if parts.fragment:
        flags.append("fragment_removed")
    if host in SHORTENERS:
        flags.append("shortener")
    if host.startswith(AGGREGATOR_HOSTS):
        flags.append("aggregator_redirect")
    return urlunsplit(("https", netloc, path, query, "")), flags


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    for prefix in ("www.", "m.", "mobile.", "amp."):
        if host.startswith(prefix) and host.count(".") >= 2:
            return host[len(prefix):]
    return host


def resolve_redirect(url: str, client, max_hops: int = 5) -> str | None:  # pragma: no cover - network
    """Live mode only: follow HTTP redirects of a known shortener with a HEAD request."""
    if domain_of(url) not in SHORTENERS:
        return None
    try:
        res = client.head(url, follow_redirects=True, timeout=8.0)
        if len(res.history) <= max_hops and str(res.url) != url:
            return str(res.url)
    except Exception:  # noqa: BLE001 - a failed resolution keeps the original URL
        return None
    return None
