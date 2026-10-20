"""Stage 3: template generation, normalization, q grid, MDOF physics, safe bank import."""

from __future__ import annotations

import io
import json
import math
import zipfile

import numpy as np
import pytest
from scipy import optimize

from hvsr_core.config import AnalysisConfig
from hvsr_core.jsonutil import sha256_bytes
from hvsr_core.mdof import build_chain, first_mode_anchor, top_response_direct, top_response_modal
from hvsr_core.templates import (
    DEFAULT_SDOF_DAMPING,
    Q_GRID,
    Q_HALF,
    RAW,
    X_GRID,
    BankError,
    _npz_bytes,
    available_banks,
    generate_sdof_bank,
    get_bank,
    ko_conditioning,
    ko_continuous,
    load_bank_bytes,
    load_bank_files,
    sdof_m,
    sdof_r_peak,
    sign_manifest,
)

SHIPPED = [
    "sdof-raw-v1",
    *[f"sdof-ko{b}-v1" for b in (20, 30, 40, 50, 60, 80, 100)],
    "mdof2-raw-v1",
    "mdof2-ko40-v1",
    "mdof3-raw-v1",
    "mdof3-ko40-v1",
]


@pytest.mark.acceptance("AC-S3-05")
def test_q_grid_is_129_uniform_symmetric_with_q1():
    assert X_GRID.size == 129 and Q_HALF == 64
    assert X_GRID[0] == pytest.approx(-math.log(2), abs=1e-15) and X_GRID[-1] == pytest.approx(
        math.log(2), abs=1e-15
    )
    assert X_GRID[Q_HALF] == 0.0 and Q_GRID[Q_HALF] == 1.0
    np.testing.assert_array_equal(X_GRID, -X_GRID[::-1])  # exact symmetry
    np.testing.assert_allclose(np.diff(X_GRID), math.log(2) / 64, rtol=1e-12)
    assert Q_GRID[0] == pytest.approx(0.5) and Q_GRID[-1] == pytest.approx(2.0)
    assert not X_GRID.flags.writeable


@pytest.mark.acceptance("AC-S3-01")
@pytest.mark.parametrize("bank_id", SHIPPED)
def test_shipped_template_peaks_align_at_q1_with_amplitude_1(bank_id):
    assert bank_id in available_banks()
    bank = get_bank(bank_id)
    lt = bank.log_t
    assert np.all(lt[:, Q_HALF] == 0.0)
    assert np.all(lt[:, Q_HALF - 1] < 0) and np.all(lt[:, Q_HALF + 1] < 0)
    if bank.family == "sdof":
        assert np.all(lt <= 1e-15)  # global maximum for SDOF


@pytest.mark.acceptance("AC-S3-01")
@pytest.mark.parametrize("zeta", [0.01, 0.05, 0.2])
def test_conditioned_peak_located_after_smoothing(zeta):
    bank = get_bank("sdof-ko40-v1")
    k = list(DEFAULT_SDOF_DAMPING).index(zeta)
    rp = bank.anchor_ratio[k]
    h = lambda r: ko_continuous(lambda s: sdof_m(s, zeta), r, 40.0, 3.0)  # noqa: E731
    res = optimize.minimize_scalar(
        lambda t: -h(np.array([math.exp(t)]))[0],
        bounds=(math.log(rp) - 0.05, math.log(rp) + 0.05),
        method="bounded",
        options={"xatol": 1e-12},
    )
    assert math.exp(res.x) == pytest.approx(rp, rel=1e-6)
    # smoothing moves the peak: conditioned anchor differs from the raw r_peak
    assert abs(rp / sdof_r_peak(zeta) - 1) > 1e-5


@pytest.mark.acceptance("AC-S3-02")
def test_dense_direct_sdof_agrees_with_bank_lookup():
    raw = get_bank("sdof-raw-v1")
    for k, z in enumerate(DEFAULT_SDOF_DAMPING):
        rp = sdof_r_peak(z)
        direct = np.log(sdof_m(Q_GRID * rp, z) / sdof_m(np.array([rp]), z)[0])
        np.testing.assert_allclose(raw.log_t[k], direct, rtol=1e-12, atol=1e-14)
    # conditioned bank vs. independent higher-resolution quadrature (declared tolerance 1e-6 in ln)
    ko = get_bank("sdof-ko40-v1")
    for k in (0, 3, 7):
        z = DEFAULT_SDOF_DAMPING[k]
        rp = ko.anchor_ratio[k]
        fine = lambda r, z=z: ko_continuous(lambda s: sdof_m(s, z), r, 40.0, 3.0, n_quad=16001)  # noqa: E731
        direct = np.log(fine(Q_GRID * rp) / fine(np.array([rp]))[0])
        np.testing.assert_allclose(ko.log_t[k], direct, atol=1e-6)


@pytest.mark.acceptance("AC-S3-04")
@pytest.mark.parametrize("zeta", DEFAULT_SDOF_DAMPING)
def test_damped_peak_is_not_the_undamped_natural_frequency(zeta):
    rp = sdof_r_peak(zeta)
    res = optimize.minimize_scalar(
        lambda r: -sdof_m(np.array([r]), zeta)[0],
        bounds=(0.3, 1.5),
        method="bounded",
        options={"xatol": 1e-13},
    )
    assert res.x == pytest.approx(rp, rel=1e-8)
    assert rp < 1.0  # peak below fn for every positive damping
    raw = get_bank("sdof-raw-v1")
    k = list(DEFAULT_SDOF_DAMPING).index(zeta)
    assert raw.anchor_ratio[k] == pytest.approx(rp, rel=1e-15)
    assert raw.template_label(k)["peak_to_reference_frequency_ratio"] == pytest.approx(rp)


@pytest.mark.acceptance("AC-S3-13")
@pytest.mark.parametrize(
    ("masses", "stiff", "damping"),
    [
        ([1.0], [1.0], {"kind": "modal", "ratios": 0.05}),
        ([0.5], [2.0], {"kind": "modal", "ratios": [0.02, 0.1]}),
        ([1.0, 1.0], [1.0, 1.0], {"kind": "modal", "ratios": 0.1}),
        ([2.0, 0.5], [0.5, 1.5], {"kind": "rayleigh", "zeta_1": 0.05, "zeta_2": 0.08}),
    ],
)
def test_mdof_top_response_matches_independent_modal_solution(masses, stiff, damping):
    ch = build_chain(masses, stiff, damping)
    assert ch.omega[0] == pytest.approx(1.0)
    r = np.geomspace(0.05, 6.0, 3000)
    np.testing.assert_allclose(top_response_direct(ch, r), top_response_modal(ch, r), rtol=1e-10)
    assert top_response_direct(ch, np.array([1e-5]))[0] == pytest.approx(1.0, abs=1e-8)  # rigid-body limit
    if damping["kind"] == "rayleigh":
        w = ch.omega
        alpha_beta = np.linalg.solve([[1 / (2 * w[0]), w[0] / 2], [1 / (2 * w[1]), w[1] / 2]], [0.05, 0.08])
        np.testing.assert_allclose(
            ch.modal_damping, alpha_beta[0] / (2 * w) + alpha_beta[1] * w / 2, rtol=1e-10
        )
    anc = first_mode_anchor(lambda x: top_response_direct(ch, x), ch.omega)
    assert anc.status == "ok" and 0.6 < anc.r < 1.0


@pytest.mark.acceptance("AC-S3-13")
def test_one_dof_and_rigid_two_dof_reduce_to_sdof():
    for z in (0.02, 0.1, 0.3):
        one = build_chain([], [], {"kind": "modal", "ratios": z})
        r = np.geomspace(0.05, 5.0, 500)
        np.testing.assert_allclose(top_response_direct(one, r), sdof_m(r, z), rtol=1e-12)
    rigid = build_chain([1.0], [1e8], {"kind": "modal", "ratios": 0.05})
    r = np.geomspace(0.05, 3.0, 500)
    np.testing.assert_allclose(top_response_direct(rigid, r), sdof_m(r, 0.05), rtol=1e-4)


@pytest.mark.acceptance("AC-S3-12")
def test_shipped_banks_are_reproducible_byte_for_byte():
    from hvsr_core.mdof import generate_mdof_bank
    from hvsr_core.templates import SHIPPED_DIR, SHIPPED_KO_BANDWIDTHS

    regenerated = [generate_sdof_bank(RAW)] + [
        generate_sdof_bank(ko_conditioning(b)) for b in SHIPPED_KO_BANDWIDTHS
    ]
    regenerated.append(
        generate_mdof_bank(
            2, RAW, (0.25, 0.5, 1.0, 2.0), (0.25, 0.5, 1.0, 2.0), (0.02, 0.05, 0.1, 0.2), "mdof2-raw-v1"
        )
    )
    regenerated.append(
        generate_mdof_bank(3, RAW, (0.5, 1.0, 2.0), (0.5, 1.0, 2.0), (0.02, 0.05, 0.1, 0.2), "mdof3-raw-v1")
    )
    for manifest, data in regenerated:
        shipped = json.loads((SHIPPED_DIR / f"{manifest['bank_id']}.json").read_text())
        assert shipped == manifest
        assert (SHIPPED_DIR / manifest["data_file"]).read_bytes() == data


def test_mdof_rejects_inadmissible_systems():
    with pytest.raises(BankError):
        build_chain([-1.0], [1.0], {"kind": "modal", "ratios": 0.05})
    with pytest.raises(BankError):
        build_chain([1.0], [1.0], {"kind": "modal", "ratios": 1.5})


# ---------------------------------------------------------------- safe import


def _good():
    return generate_sdof_bank(
        RAW, damping=(0.05, 0.1, 0.2), bank_id="test-sdof", created_utc="2026-10-20T00:00:00Z"
    )


def _rehash(manifest, data):
    return dict(manifest, data_sha256=sha256_bytes(data))


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in entries.items():
            zf.writestr(k, v)
    return buf.getvalue()


def _npy(arr, allow_pickle=False) -> bytes:
    b = io.BytesIO()
    np.lib.format.write_array(b, arr, allow_pickle=allow_pickle)
    return b.getvalue()


@pytest.mark.acceptance("AC-S3-15")
def test_valid_bank_roundtrip_and_signature(tmp_path):
    m, d = _good()
    bank = load_bank_bytes(m, d)
    assert bank.n_templates == 3 and bank.provenance()["data_sha256"] == sha256_bytes(d)
    assert _good()[1] == d  # deterministic bytes
    signed = sign_manifest(m, b"secret", "k1")
    assert load_bank_bytes(signed, d, hmac_key=b"secret", require_signature=True).bank_id == "test-sdof"
    with pytest.raises(BankError, match="signature"):
        load_bank_bytes(signed, d, hmac_key=b"other")
    with pytest.raises(BankError, match="signature"):
        load_bank_bytes(m, d, hmac_key=b"secret", require_signature=True)
    tampered = dict(signed, notes="changed")
    with pytest.raises(BankError, match="signature"):
        load_bank_bytes(tampered, d, hmac_key=b"secret")


@pytest.mark.acceptance("AC-S3-15")
def test_rejects_pickle_object_arrays_and_unsafe_archives(tmp_path):
    m, d = _good()
    good = dict(np.load(io.BytesIO(d), allow_pickle=False))
    # object array (pickle) inside the NPZ
    evil = _zip(
        {
            **{f"{k}.npy": _npy(v) for k, v in good.items() if k != "param_values"},
            "param_values.npy": _npy(np.array([{"a": 1}] * 3, dtype=object), allow_pickle=True),
        }
    )
    with pytest.raises(BankError, match="rejected|non-numeric"):
        load_bank_bytes(_rehash(m, evil), evil)
    # path traversal / unexpected entries
    for name in ("../x.npy", "sub/x.npy", "/abs.npy", "evil.npy", "x.pkl"):
        bad = _zip({**{f"{k}.npy": _npy(v) for k, v in good.items()}, name: _npy(np.zeros(3))})
        with pytest.raises(BankError, match="unsafe|unexpected"):
            load_bank_bytes(_rehash(m, bad), bad)
    # not a zip
    with pytest.raises(BankError, match="zip"):
        load_bank_bytes(_rehash(m, b"\x80\x04pickle"), b"\x80\x04pickle")
    # manifest data_file traversal is rejected by the schema and by the file loader
    js = tmp_path / "b.json"
    js.write_text(json.dumps(dict(m, data_file="../b.npz")))
    with pytest.raises(BankError):
        load_bank_files(js)


@pytest.mark.acceptance("AC-S3-15")
def test_rejects_schema_hash_and_domain_violations():
    m, d = _good()
    with pytest.raises(BankError, match="SHA-256"):
        load_bank_bytes(m, d + b"\x00")
    for bad in (
        dict(m, family="sdof7"),
        dict(m, grid=dict(m["grid"], n_points=128)),
        {k: v for k, v in m.items() if k != "license"},
        dict(m, extra=1),
    ):
        with pytest.raises(BankError, match="manifest invalid"):
            load_bank_bytes(bad, d)
    arrays = dict(np.load(io.BytesIO(d), allow_pickle=False))

    def variant(**over):
        a = dict(arrays, **over)
        data = _npz_bytes(a)
        return _rehash(m, data), data

    lt = arrays["log_templates"].copy()
    lt[1, 10] = np.nan
    with pytest.raises(BankError, match="non-finite"):
        load_bank_bytes(*variant(log_templates=lt))
    lt = arrays["log_templates"] + 0.1
    with pytest.raises(BankError, match="normalized"):
        load_bank_bytes(*variant(log_templates=lt))
    with pytest.raises(BankError, match="grid"):
        load_bank_bytes(*variant(x=arrays["x"] * 1.01))
    with pytest.raises(BankError, match="shapes"):
        load_bank_bytes(*variant(log_templates=arrays["log_templates"][:2]))
    with pytest.raises(BankError, match="non-numeric"):
        load_bank_bytes(*variant(anchor_ratio=np.array(["a", "b", "c"])))


@pytest.mark.acceptance("AC-S3-12")
def test_bank_versions_change_provenance(tmp_path, monkeypatch):
    m, d = generate_sdof_bank(ko_conditioning(40.0), bank_id="sdof-ko40-v1", bank_version="1.0.1")
    other = tmp_path / "banks"
    other.mkdir()
    (other / "custom-v2.json").write_text(json.dumps(dict(m, bank_id="custom-v2", data_file="custom-v2.npz")))
    (other / "custom-v2.npz").write_bytes(d)
    monkeypatch.setenv("HVSR_BANK_DIRS", str(other))
    a = get_bank("sdof-ko40-v1").provenance()
    b = get_bank("custom-v2").provenance()
    assert a["bank_id"] != b["bank_id"] and a["bank_version"] == "1.0.0" and b["bank_version"] == "1.0.1"
    c1 = AnalysisConfig()
    c2 = AnalysisConfig.model_validate({"sdof": {"template_bank": "custom-v2"}})
    assert c1.scientific_hash() != c2.scientific_hash()
