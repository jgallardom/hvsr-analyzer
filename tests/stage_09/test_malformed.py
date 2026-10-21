"""Malformed input never crashes the service: every case is rejected with a clear 4xx reason, and
nothing is stored or charged."""

from __future__ import annotations

import gzip
import json
import random
import re
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hvsr_core.io.csv_reader import CSVOptions, iter_lines, read_csv
from hvsr_core.io.mseed_reader import read_miniseed
from hvsr_core.io.stationxml import parse_stationxml
from hvsr_core.model import InputError
from hvsr_web.api import create_app

from stage9_support import make_ctx, new_contributor, stationxml_bytes, submission, synthetic_csv_gz

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CSV_OPTS = json.dumps({"csv_layout": "elapsed_time"})
GOOD_CSV = "time_s,N,E,Z\n" + "".join(
    f"{i / 50:.2f},{i % 7 - 3},{i % 5 - 2},{i % 3 - 1}\n" for i in range(3000)
)
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<FDSNStationXML>&lol2;</FDSNStationXML>"""


def _mutate(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    op = rng.choice(["flip", "truncate", "insert", "duplicate"])
    if op == "flip":
        for _ in range(rng.randint(1, 20)):
            b[rng.randrange(len(b))] = rng.randrange(256)
    elif op == "truncate":
        b = b[: rng.randrange(len(b))]
    elif op == "insert":
        i = rng.randrange(len(b))
        b[i:i] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 50)))
    else:
        i = rng.randrange(len(b))
        b[i:i] = b[i : min(len(b), i + rng.randint(1, 2000))]
    return bytes(b)


@pytest.mark.acceptance("AC-S9-02")
def test_readers_raise_only_input_errors_on_corrupted_bytes():
    rng = random.Random(20261020)
    csv = (FIXTURES / "flat_utc_20hz.csv").read_bytes()
    mseed = (FIXTURES / "lognormal3hz_steim2_50hz.mseed").read_bytes()
    rejected = {"csv": 0, "mseed": 0}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(200):
            try:
                read_csv(_mutate(rng, csv), CSVOptions(layout="utc_timestamps", timestamps_declared_utc=True))
            except InputError as exc:
                assert exc.code and exc.message
                rejected["csv"] += 1
            try:
                read_miniseed([("x.mseed", _mutate(rng, mseed))])
            except InputError as exc:
                assert exc.code and exc.message
                rejected["mseed"] += 1
    assert rejected["csv"] > 100 and rejected["mseed"] > 100  # the corruption was mostly detectable


def _with_dtd(decl: str, encoding: str, comment_bytes: int = 0, entity: bool = True) -> bytes:
    body = stationxml_bytes().decode().split("?>", 1)[1]
    if entity:
        body = re.sub(r"<Source>[^<]*</Source>", "<Source>&x;</Source>", body, count=1)
    pad = f"<!--{'p' * comment_bytes}-->" if comment_bytes else ""
    return (f'<?xml version="1.0" encoding="{encoding.upper()}"?>' + pad + decl + body).encode(encoding)


@pytest.mark.acceptance("AC-S9-02")
def test_stationxml_dtd_is_rejected_in_any_encoding():
    inv, _ = parse_stationxml(stationxml_bytes())
    assert inv.source == "stage9"
    internal = '<!DOCTYPE FDSNStationXML [<!ENTITY x "INJECTED">]>'
    laughs = (
        '<!DOCTYPE FDSNStationXML [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY x "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
    )
    external = '<!DOCTYPE FDSNStationXML SYSTEM "file:///etc/passwd">'
    variants = {
        "utf-8 internal entity": _with_dtd(internal, "utf-8"),
        "utf-8 entity after a long prolog comment": _with_dtd(internal, "utf-8", 5000),
        # These two were accepted (and the entity expanded) before the encoding-independent check.
        "utf-16 internal entity": _with_dtd(internal, "utf-16"),
        "utf-16 after a long prolog comment": _with_dtd(internal, "utf-16", 5000),
        "utf-16 nested expansion": _with_dtd(laughs, "utf-16"),
        "utf-16 external DTD": _with_dtd(external, "utf-16", entity=False),
    }
    for name, data in variants.items():
        with pytest.raises(InputError) as err:
            parse_stationxml(data)
        assert err.value.code in ("stationxml_unsafe", "stationxml_invalid"), name
        assert "INJECTED" not in str(err.value) and "root:" not in str(err.value), name
    for name in ("utf-16 internal entity", "utf-16 nested expansion"):
        with pytest.raises(InputError) as err:
            parse_stationxml(variants[name])
        assert err.value.code == "stationxml_unsafe", name


@pytest.mark.acceptance("AC-S9-02")
def test_streaming_csv_lines_match_splitlines_and_elapsed_fuzz_only_raises_input_errors():
    rng = random.Random(7)
    alphabet = ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
    alphabet += ["a", ",", " ", "#", "\t", "1"]
    for _ in range(20000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        assert list(iter_lines(s)) == s.splitlines(), repr(s)
    good = GOOD_CSV.encode()
    accepted = 0
    for _ in range(200):
        try:
            read_csv(_mutate(rng, good), CSVOptions(layout="elapsed_time"))
            accepted += 1
        except InputError as exc:
            assert exc.code and exc.message
    assert accepted < 200


def _cases():
    good = GOOD_CSV.encode()
    gz_half = gzip.compress(good, mtime=0)
    return [
        ("empty file", [("rec.csv", b"")], CSV_OPTS, None, 422),
        ("binary garbage", [("rec.csv", random.Random(1).randbytes(4096))], CSV_OPTS, None, 422),
        ("truncated gzip", [("rec.csv.gz", gz_half[: len(gz_half) // 2])], CSV_OPTS, None, 422),
        (
            "decompression bomb",
            [("rec.csv.gz", gzip.compress(bytes(64 * 1024 * 1024), mtime=0))],
            CSV_OPTS,
            None,
            413,
        ),
        ("NaN sample", [("rec.csv", good.replace(b"\n0.10,", b"\n0.10,nan,", 1))], CSV_OPTS, None, 422),
        ("missing column", [("rec.csv", b"time_s,N,E\n0,1,2\n0.02,1,2\n")], CSV_OPTS, None, 422),
        ("time goes backwards", [("rec.csv", good.replace(b"\n0.10,", b"\n0.04,", 1))], CSV_OPTS, None, 422),
        ("header only", [("rec.csv", b"time_s,N,E,Z\n")], CSV_OPTS, None, 422),
        ("ragged row", [("rec.csv", good.replace(b"\n0.10,", b"\n0.10,1,", 1))], CSV_OPTS, None, 422),
        ("one enormous line", [("rec.csv", b"time_s," + b"9" * 8_000_000)], CSV_OPTS, None, 422),
        ("layout not declared", [("rec.csv", good)], "{}", None, 422),
        ("unknown layout", [("rec.csv", good)], json.dumps({"csv_layout": "magic"}), None, 422),
        ("options not JSON", [("rec.csv", good)], "{csv_layout:", None, 422),
        (
            "truncated MiniSEED",
            [("x.mseed", (FIXTURES / "lognormal3hz_steim2_50hz.mseed").read_bytes()[:300])],
            "{}",
            None,
            422,
        ),  # fmt: skip
        (
            "StationXML entity bomb",
            [("rec.csv", good), ("r.xml", BILLION_LAUGHS)],
            CSV_OPTS,
            ["waveform", "stationxml"],
            422,
        ),
        (
            "StationXML entity in UTF-16",
            [
                ("rec.csv", good),
                ("r.xml", _with_dtd('<!DOCTYPE FDSNStationXML [<!ENTITY x "I">]>', "utf-16")),
            ],
            CSV_OPTS,
            ["waveform", "stationxml"],
            422,
        ),
        (
            "StationXML garbage",
            [("rec.csv", good), ("r.xml", b"<FDSNStationXML><oops")],
            CSV_OPTS,
            ["waveform", "stationxml"],
            422,
        ),  # fmt: skip
        ("roles mismatch", [("rec.csv", good)], CSV_OPTS, ["waveform", "stationxml"], 422),
        ("too many files", [(f"x{i}.mseed", b"x") for i in range(7)], "{}", None, 413),
    ]


@pytest.mark.acceptance("AC-S9-02")
def test_malformed_uploads_are_rejected_clearly_and_store_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    token, _ = new_contributor(ctx)
    auth = {"Authorization": f"Bearer {token}"}
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    assert client.post("/api/v1/previews", headers=auth, data={"waveform": CSV_OPTS},
                       files=[("files", ("ok.csv", GOOD_CSV.encode()))]).status_code == 200  # fmt: skip
    for name, files, opts, roles, expected in _cases():
        data = {"waveform": opts}
        if roles:
            data["roles"] = roles
        r = client.post(
            "/api/v1/previews", headers=auth, data=data, files=[("files", (n, b)) for n, b in files]
        )
        assert r.status_code == expected, (name, r.status_code, r.text[:300])
        detail = r.json()["detail"]
        text = json.dumps(detail)
        assert len(text) > 10 and "Traceback" not in text, (name, detail)

        sub = submission(f"bad-{abs(hash(name)) % 10**8:08d}").model_dump(mode="json")
        sub["waveform"] = (
            json.loads(opts) if opts.startswith("{") and opts.endswith("}") and len(opts) > 2 else {}
        )
        r = client.post(
            "/api/v1/submissions",
            headers=auth,
            data={"submission": json.dumps(sub), **({"roles": roles} if roles else {})},
            files=[("files", (n, b)) for n, b in files],
        )
        assert r.status_code in (413, 422), (name, r.status_code, r.text[:300])
    committed = [
        p for p in Path(ctx.settings.local_store_dir).rglob("*") if p.is_file() and "staging" not in p.parts
    ]
    assert committed == []
    usage = client.get("/api/v1/limits", headers=auth)
    assert usage.status_code == 200


@pytest.mark.acceptance("AC-S9-02")
def test_malformed_requests_are_rejected_clearly(tmp_path):
    ctx = make_ctx(tmp_path)
    token, c = new_contributor(ctx)
    auth = {"Authorization": f"Bearer {token}"}
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    sub = submission("good-0000001").model_dump(mode="json")
    r = client.post(
        "/api/v1/submissions",
        headers=auth,
        data={"submission": json.dumps(sub)},
        files=[("files", ("rec.csv.gz", synthetic_csv_gz(300, seed=2)))],
    )
    assert r.status_code == 201, r.text
    mid = r.json()["measurement_id"]
    fresh = {**sub, "idempotency_key": "fresh-key-0002"}
    bad_submissions = [
        "{not json",
        json.dumps({**sub, "idempotency_key": "x"}),
        json.dumps({**fresh, "consent": {**sub["consent"], "consent_checked": False}}),
        json.dumps({**fresh, "unexpected": 1}),
        json.dumps({**fresh, "metadata": {**sub["metadata"], "latitude": 91}}),
    ]
    for s in bad_submissions:
        r = client.post(
            "/api/v1/submissions", headers=auth, data={"submission": s},
            files=[("files", ("rec.csv.gz", synthetic_csv_gz(300, seed=2)))],
        )  # fmt: skip
        assert r.status_code == 422, (s[:60], r.status_code, r.text[:200])
    bad_requests = [
        {"config": {"unknown_section": {}}},
        {"config": {"uncertainty": {"n_bootstrap": -5}}},
        {"config": {"windowing": {"window_length_s": "long"}}},
        {"config": {"sdof": {"template_bank": "no-such-bank"}}},
        {"display": {"dpi": 10_000}},
    ]
    for body in bad_requests:
        r = client.post(f"/api/v1/measurements/{mid}/analyses", headers=auth, json=body)
        assert r.status_code == 422, (body, r.status_code, r.text[:200])
        assert "Traceback" not in r.text
    r = client.post(f"/api/v1/measurements/{mid}/analyses", headers=auth, content=b"\xff\xfe not json")
    assert r.status_code == 422
    assert client.get(f"/api/v1/measurements/{mid}/manifest?token={token}").status_code == 400
    assert client.get("/api/v1/artifacts/../../etc/passwd").status_code == 404
    r = client.get("/api/v1/blobs/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 422 and "root:" not in r.text
