"""Stage 8 test helpers (a uniquely named module; `conftest` names collide across stage folders).

A `Site` is a complete application (database, object store, HTTP app) driven only through its HTTP
interface; jobs run when the test drains the worker, exactly as a separate worker process would.
"""

from __future__ import annotations

import gzip
import io
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_web import worker
from hvsr_web.api import create_app
from hvsr_web.auth import issue_admin_token, issue_upload_token
from hvsr_web.context import AppContext
from hvsr_web.settings import Settings
from hvsr_web.storage import LocalObjectStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROOT = Path(__file__).resolve().parents[2]
SIGNING_KEY = "stage8-test-signing-key-0123456789abcdef"
CSV_GZ = "sdof2hz_elapsed_50hz.csv.gz"
MSEED = "lognormal3hz_steim2_50hz.mseed"
# Spec section 9.1, verbatim; deliberately not imported from the code under test.
BAND_LABEL = "Estimated 95% pointwise CI for the selected-window mean"


@dataclass
class Site:
    ctx: AppContext
    client: TestClient
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def make_site(root: Path, **overrides) -> Site:
    s = Settings(
        database_url=f"sqlite:///{root / 'db.sqlite3'}",
        local_store_dir=str(root / "objects"),
        url_signing_key=SIGNING_KEY,
        **overrides,
    )
    ctx = AppContext.from_settings(s, store=LocalObjectStore(s.local_store_dir, SIGNING_KEY.encode()))
    with ctx.sessions() as session, session.begin():
        token, _ = issue_upload_token(session, private_email="stage8@example.org")
    return Site(ctx, TestClient(create_app(ctx)), token)


def reopen(site: Site, **overrides) -> Site:
    """The same database and objects under different settings (e.g. a lowered limit)."""
    base = site.ctx.settings.model_dump()
    s = Settings(**{**base, **overrides})
    ctx = AppContext.from_settings(s, store=LocalObjectStore(s.local_store_dir, SIGNING_KEY.encode()))
    return Site(ctx, TestClient(create_app(ctx)), site.token)


def admin_token(site: Site) -> str:
    with site.ctx.sessions() as session, session.begin():
        token, _ = issue_admin_token(session, "Stage 8 Administrator")
    return token


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def synthetic_csv_gz(duration_s: float, seed: int, fn: float = 2.0) -> bytes:
    """A synthetic three-component recording written as an elapsed-time CSV (50 Hz, 6 decimals)."""
    rec, _ = generate_3c(duration_s=duration_s, fs=50, tf=TFSpec("sdof", {"fn": fn, "zeta": 0.2}), seed=seed)
    data = rec.segments[0].data
    lines = ["time_s,N,E,Z\n"]
    for i in range(data.shape[1]):
        lines.append(f"{i / 50:.2f},{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write("".join(lines).encode())
    return buf.getvalue()


def png_photo() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 120, 60)).save(buf, format="PNG")
    return buf.getvalue()


def submission(
    key: str,
    *,
    lat: float = 19.4326,
    lon: float = -99.1332,
    attribution: str = "Example Observatory",
    waveform: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "idempotency_key": key,
        "waveform": waveform or {"csv_layout": "elapsed_time"},
        "metadata": {"latitude": lat, "longitude": lon, **(metadata or {})},
        "consent": {"consent_checked": True, "rights_confirmed": True, "attribution": attribution},
    }


def submit(site: Site, files: list[tuple[str, bytes, str]], key: str, **kw) -> dict:
    """POST /api/v1/submissions; `files` are (filename, bytes, role)."""
    r = site.client.post(
        "/api/v1/submissions",
        data={"submission": json.dumps(submission(key, **kw)), "roles": [f[2] for f in files]},
        files=[("files", (name, data)) for name, data, _ in files],
        headers=site.auth,
    )
    assert r.status_code == 201, r.text
    return r.json()


def analyze(site: Site, measurement_id: str, body: dict | None = None) -> str:
    r = site.client.post(
        f"/api/v1/measurements/{measurement_id}/analyses", json=body or {}, headers=site.auth
    )
    assert r.status_code == 202, r.text
    worker.drain(site.ctx)
    job = site.client.get(r.json()["job_url"]).json()
    assert job["state"] == "succeeded", job
    return job["result"]["run_id"]


def get_json(site: Site, url: str) -> dict:
    r = site.client.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return r.json()


def download(site: Site, url: str):
    """Bytes behind a stable artifact URL (following the signed redirect; generating on request)."""
    r = site.client.get(url)
    if r.status_code == 202:
        worker.drain(site.ctx)
        r = site.client.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return r.content, r


def entries(manifest: dict) -> list[dict]:
    return [e for items in manifest["categories"].values() for e in items]


def one(items: list[dict], **match) -> dict:
    found = [e for e in items if all(e.get(k) == v for k, v in match.items())]
    assert len(found) == 1, (match, [(e["kind"], e.get("method"), e.get("run_id")) for e in found])
    return found[0]


def csv_rows(data: bytes) -> list[dict[str, str]]:
    import csv

    text = (gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def as_float(cell: str) -> float:
    return float(cell) if cell != "" else float("nan")


def same(a, b) -> bool:
    return np.array_equal(np.asarray(a, dtype=float), np.asarray(b, dtype=float), equal_nan=True)


# ----------------------------------------------------------------------------------------- HTML
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
VISIBLE_ATTRS = ("alt", "title", "aria-label", "placeholder", "data-label", "label")


class PageText(HTMLParser):
    """Visible text and human-readable attributes, split by whether they are marked translate="no"."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.interface: list[str] = []  # text a translator must see (outside translate="no")
        self.content: list[str] = []  # verbatim data
        self.ids: dict[str, list[str]] = {}

    def _verbatim(self) -> bool:
        return any(v for _, v in self.stack)

    def _skip(self) -> bool:
        return any(tag in ("script", "style") for tag, _ in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        verbatim = self._verbatim() or a.get("translate") == "no"
        for name in VISIBLE_ATTRS:
            if a.get(name):
                (self.content if verbatim else self.interface).append(a[name])
        if tag not in VOID:
            self.stack.append((tag, a.get("translate") == "no"))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._skip() or not data.strip():
            return
        (self.content if self._verbatim() else self.interface).append(data)


def page_text(html: str) -> PageText:
    p = PageText()
    p.feed(html)
    p.close()
    return p


def element_text(html: str, element_id: str) -> str:
    """Text content of the element with the given id (tags stripped, whitespace collapsed)."""
    m = re.search(rf'<(\w+)[^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
    assert m, f"no element #{element_id}"
    tag, start, depth, pos = m.group(1), m.end(), 1, m.end()
    for t in re.finditer(rf"<(/?){tag}\b[^>]*>", html[start:]):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            pos = start + t.start()
            break
    inner = re.sub(r"<[^>]+>", " ", html[start:pos])
    from html import unescape

    return re.sub(r"\s+", " ", unescape(inner)).strip()


def svg_texts(svg: str) -> list[str]:
    from html import unescape

    return [unescape(s) for s in re.findall(r"<text\b[^>]*>(.*?)</text>", svg, flags=re.S)]
