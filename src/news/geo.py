"""Places, from GeoNames (CC BY 4.0, geonames.org): towns and cities over
15,000 people, first-level regions (e.g. Scotland = GB.SCT) and countries.
Loaded once with `news geonames load`; lookups are local, never external."""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

BASE = "https://download.geonames.org/export/dump/"
FILES = ("cities15000.zip", "admin1CodesASCII.txt", "countryInfo.txt")
csv.field_size_limit(10_000_000)


@dataclass
class Place:
    geonames_id: int
    name: str
    country: str | None
    admin1: str | None
    feature_code: str | None
    population: int | None
    lat: float | None
    lon: float | None

    @property
    def kind(self) -> str:
        if self.feature_code and self.feature_code.startswith("PCL"):
            return "country"
        if self.feature_code == "ADM1":
            return "admin1"
        return "place"


def download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for name in FILES:
            path = dest / name
            if path.exists():
                continue
            log.info("downloading", extra={"file": name})
            resp = client.get(BASE + name)
            resp.raise_for_status()
            path.write_bytes(resp.content)


def _rows(dest: Path) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    # Countries: ISO, ISO3, ISO-num, fips, name, capital, area, population, continent, tld,
    # currency, currency name, phone, postal format, postal regex, languages, geonameid, …
    for line in (dest / "countryInfo.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = line.split("\t")
        if len(f) > 16 and f[16].isdigit():
            rows.append((int(f[16]), f[4], f[4], [f[4].lower(), f[1].lower(), f[0].lower()], f[0], None, "A", "PCLI",
                         int(f[7] or 0), None, None))
    # Regions: "GB.SCT", name, ascii name, geonameid
    for line in (dest / "admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) >= 4 and f[3].isdigit():
            country, _, admin1 = f[0].partition(".")
            rows.append((int(f[3]), f[1], f[2], [f[1].lower(), f[2].lower()], country, admin1, "A", "ADM1",
                         None, None, None))
    # Places: geonameid, name, asciiname, alternatenames, lat, lon, feature class, feature code,
    # country, cc2, admin1, admin2, admin3, admin4, population, …
    with zipfile.ZipFile(dest / "cities15000.zip") as z, z.open("cities15000.txt") as fh:
        for f in csv.reader(io.TextIOWrapper(fh, encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE):
            alts = sorted({a.lower() for a in f[3].split(",") if a and len(a) < 60})[:60]
            rows.append((int(f[0]), f[1], f[2], alts, f[8], f[10] or None, f[6], f[7],
                         int(f[14] or 0), float(f[4]), float(f[5])))
    return rows


async def load(session: AsyncSession, dest: Path) -> int:
    rows = _rows(dest)
    await session.execute(text("UPDATE item_locations SET geonames_id = NULL"))
    await session.execute(text("DELETE FROM geonames"))
    stmt = text("""
        INSERT INTO geonames (geonameid, name, ascii_name, alt_names, country, admin1, feature_class,
                              feature_code, population, geom)
        VALUES (:id, :name, :ascii, :alts, :country, :admin1, :fclass, :fcode, :pop,
                CASE WHEN CAST(:lat AS float8) IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)), 4326)::geography END)
        ON CONFLICT (geonameid) DO NOTHING
    """)
    keys = ("id", "name", "ascii", "alts", "country", "admin1", "fclass", "fcode", "pop", "lat", "lon")
    for i in range(0, len(rows), 2000):
        await session.execute(stmt, [dict(zip(keys, r, strict=True)) for r in rows[i:i + 2000]])
    await session.commit()
    return len(rows)


_SELECT = """
    SELECT geonameid, name, country, admin1, feature_code, population,
           ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon
    FROM geonames
"""
# Countries first, then regions, then the most populous place
_ORDER = """
    ORDER BY CASE WHEN feature_code LIKE 'PCL%' THEN 0 WHEN feature_code = 'ADM1' THEN 1 ELSE 2 END,
             population DESC NULLS LAST
"""


def _place(r: object) -> Place:
    m = r._mapping  # type: ignore[attr-defined]
    return Place(geonames_id=m["geonameid"], name=m["name"], country=m["country"], admin1=m["admin1"],
                 feature_code=m["feature_code"], population=m["population"], lat=m["lat"], lon=m["lon"])


async def geocode(session: AsyncSession, name: str, country: str | None = None) -> Place | None:
    """The place a name most likely means, within `country` (ISO code) when
    given. Unresolved names return None and are kept as text only."""
    n = name.strip().lower()
    if not n:
        return None
    rows = await session.execute(text(_SELECT + """
        WHERE (lower(name) = :n OR lower(ascii_name) = :n OR :n = ANY(alt_names))
          AND (CAST(:c AS text) IS NULL OR country = CAST(:c AS text))
    """ + _ORDER + " LIMIT 1"), {"n": n, "c": (country or "").upper()[:2] or None})
    row = rows.first()
    return _place(row) if row else None


async def resolve(session: AsyncSession, q: str, limit: int = 10) -> list[Place]:
    """Candidates for a place name typed by a person: exact matches first,
    then names starting with it."""
    n = q.strip().lower()
    if len(n) < 2:
        return []
    rows = await session.execute(text(_SELECT + """
        WHERE lower(name) = :n OR lower(ascii_name) = :n OR :n = ANY(alt_names)
           OR lower(ascii_name) LIKE :prefix
    """ + """
        ORDER BY (lower(name) = :n OR lower(ascii_name) = :n) DESC,
                 CASE WHEN feature_code LIKE 'PCL%' THEN 0 WHEN feature_code = 'ADM1' THEN 1 ELSE 2 END,
                 population DESC NULLS LAST
        LIMIT :limit
    """), {"n": n, "prefix": n.replace("%", "").replace("_", "") + "%", "limit": limit})
    return [_place(r) for r in rows]
