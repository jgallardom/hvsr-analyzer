"""Record identity: geohash, UTC decimal year, human record code (spec 2.3).

The human code ``ggggg-yyyy.yyyy`` uses geohash characters 3-7 (one-based, inclusive) and the UTC
decimal year rounded to four decimals. It is NOT unique; the database UUID is the identity and
collision suffixes ``-02``, ``-03`` ... are allocated transactionally by the web layer.
"""

from __future__ import annotations

import datetime as dt

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int = 12) -> str:
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ValueError("latitude/longitude out of range")
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    out = []
    bits = 0
    nbits = 0
    even = True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                bits = (bits << 1) | 1
                lon_lo = mid
            else:
                bits <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                bits = (bits << 1) | 1
                lat_lo = mid
            else:
                bits <<= 1
                lat_hi = mid
        even = not even
        nbits += 1
        if nbits == 5:
            out.append(_BASE32[bits])
            bits = 0
            nbits = 0
    return "".join(out)


def geohash_code_part(full_geohash: str) -> str:
    """Characters 3 through 7 inclusive, one-based indexing."""
    if len(full_geohash) < 7:
        raise ValueError("geohash must have at least 7 characters")
    return full_geohash[2:7]


def decimal_year(when_ns: int) -> float:
    """UTC decimal year using the actual length of that calendar year (leap-aware)."""
    sec, rem = divmod(int(when_ns), 1_000_000_000)
    t = dt.datetime.fromtimestamp(sec, tz=dt.UTC)
    y = t.year
    start = dt.datetime(y, 1, 1, tzinfo=dt.UTC)
    nxt = dt.datetime(y + 1, 1, 1, tzinfo=dt.UTC)
    elapsed_ns = (sec - int(start.timestamp())) * 1_000_000_000 + rem
    year_ns = (int(nxt.timestamp()) - int(start.timestamp())) * 1_000_000_000
    return y + elapsed_ns / year_ns


def record_code(lat: float, lon: float, when_ns: int) -> dict:
    gh = geohash_encode(lat, lon, 12)
    dy = decimal_year(when_ns)
    return {"geohash": gh, "geohash_part": geohash_code_part(gh), "decimal_year": dy, "base_code": f"{geohash_code_part(gh)}-{dy:.4f}"}


def with_suffix(base_code: str, n: int) -> str:
    """n = 1 -> base code; n >= 2 -> base-02, base-03, ..."""
    if n < 1:
        raise ValueError("suffix index starts at 1")
    return base_code if n == 1 else f"{base_code}-{n:02d}"
