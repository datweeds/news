from news.ids import uuid7
from news.pipeline.text import bands, canonical_url, clean_html, detect_lang, hamming, simhash64, truncate

BASE = (
    "Households in rural Scotland will be able to claim up to nine thousand pounds towards a heat pump "
    "under a scheme announced on Sunday by the net zero secretary, who said the grants would open in "
    "January and that installers in the Highlands and Islands would be given training support."
)


def test_canonical_url_strips_tracking_and_normalises() -> None:
    assert canonical_url("http://www.Example.com/a/b/?utm_source=x&id=2&fbclid=y#top") == "https://example.com/a/b?id=2"
    assert canonical_url("https://example.com:443/") == "https://example.com/"
    assert canonical_url("https://example.com/x?b=2&a=1") == "https://example.com/x?a=1&b=2"
    assert canonical_url("ftp://example.com/file") is None
    assert canonical_url("not a url") is None


def test_clean_html_and_truncate() -> None:
    assert clean_html("<p>Up to <b>£9,000</b>&nbsp;now</p>") == "Up to £9,000 now"
    assert clean_html("plain   text\n here") == "plain text here"
    out = truncate("word " * 200, 50)
    assert len(out) <= 50 and out.endswith("…")


def test_simhash_near_and_far() -> None:
    same = simhash64(BASE)
    assert same == simhash64(BASE)
    near = simhash64(BASE.replace("Sunday", "Monday"))
    far = simhash64("The council approved plans to extend the tram line north to Granton by 2028.")
    assert hamming(same, near) <= 3
    assert hamming(same, far) > 10
    assert -(1 << 63) <= same < (1 << 63)


def test_bands_pigeonhole() -> None:
    # Within 3 bits, at least one 16-bit band is identical
    a = simhash64(BASE)
    for flips in ([0], [5, 20], [1, 30, 63]):
        b = a
        for bit in flips:
            b ^= 1 << bit
        b = b - (1 << 64) if b >= 1 << 63 else b
        assert any(x == y for x, y in zip(bands(a), bands(b), strict=True))


def test_detect_lang() -> None:
    assert detect_lang("anything", "en-GB") == "en"
    assert detect_lang(BASE) == "en"
    assert detect_lang("Les ménages des zones rurales pourront demander une aide pour une pompe à chaleur.") == "fr"
    assert detect_lang("short") is None


def test_uuid7_is_time_ordered_v7() -> None:
    ids = [uuid7() for _ in range(50)]
    assert all(u.version == 7 for u in ids)
    assert [u.int >> 80 for u in ids] == sorted(u.int >> 80 for u in ids)


def test_names() -> None:
    from news.pipeline.stories import names

    assert names("Aberdeen appoint Liechtenstein's Frick as head coach") == {"Aberdeen", "Liechtenstein", "Frick"}
    assert names("PMQs – Badenoch asks Burnham’s team about COP31") >= {"PMQs", "Badenoch", "Burnham", "COP31"}
