"""Text normalisation: HTML cleaning, canonical URLs, language, SimHash."""

from __future__ import annotations

import hashlib
import html
import re
import warnings
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

_WS = re.compile(r"\s+")


def clean_html(text: str) -> str:
    """Plain text from HTML (or plain text), whitespace collapsed."""
    if not text:
        return ""
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ")
    return _WS.sub(" ", html.unescape(text)).strip()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    space = cut.rfind(" ")
    if space > max_chars * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


# ── Canonical URLs ───────────────────────────────────────────────────────────

_TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "at_", "_hs")
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "igshid", "mkt_tok",
    "ref", "ref_src", "ref_url", "cmpid", "smid", "src", "ito", "ocid", "cid", "share", "s_cid",
}


def canonical_url(url: str) -> str | None:
    """A normalised form for matching the same article across sources:
    https, lower-case host without www, no default port, fragment or
    tracking parameters, remaining parameters sorted, no trailing slash."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(query), ""))


# ── Language ─────────────────────────────────────────────────────────────────


def detect_lang(text: str, hint: str | None = None) -> str | None:
    """ISO 639-1 code: the feed's declared language if given, else detected
    from the text (needs a sentence or so to be reliable)."""
    if hint:
        code = hint.strip().lower().replace("_", "-").split("-")[0]
        if re.fullmatch(r"[a-z]{2,3}", code):
            return code
    if len(text) < 20:
        return None
    import py3langid

    code, _score = py3langid.classify(text)
    return str(code)


# ── SimHash ──────────────────────────────────────────────────────────────────

_MASK64 = (1 << 64) - 1
_WORD = re.compile(r"\w+", re.UNICODE)


def _features(text: str) -> list[str]:
    words = _WORD.findall(text.lower())
    return words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]


def simhash64(text: str) -> int:
    """64-bit SimHash of words and word pairs, as a signed integer (fits a
    Postgres BIGINT). Near-identical texts differ in few bits."""
    weights = [0] * 64
    for feature in _features(text):
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        for i in range(64):
            weights[i] += 1 if (h >> i) & 1 else -1
    value = sum(1 << i for i in range(64) if weights[i] > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & _MASK64).bit_count()


def bands(h: int) -> list[int]:
    """The four 16-bit bands used by the near-duplicate index (same
    arithmetic as the SQL index expressions)."""
    return [(h >> shift) & 0xFFFF for shift in (48, 32, 16, 0)]
