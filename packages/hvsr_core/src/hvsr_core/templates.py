"""Peak-aligned template banks: generation (offline), safe import, and lookup (sdof-r2log-q129-v1).

Templates are dimensionless shapes T(q), q = f / f_peak, sampled on a fixed grid of 129 points uniform
in x = ln q over [-ln 2, ln 2]. The designated peak is at q = 1 with amplitude 1 AFTER any
conditioning. Banks are stored as an NPZ of plain numeric arrays (read with allow_pickle=False) plus
a JSON manifest validated by schemas/template_bank.schema.json and bound to the NPZ by SHA-256.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import optimize

from .jsonutil import canonical_dumps, sha256_bytes
from .schema_validation import validation_errors
from .versions import SCHEMA_VERSIONS

Q_POINTS = 129
Q_HALF = (Q_POINTS - 1) // 2
X_GRID = (np.arange(Q_POINTS) - Q_HALF) * (math.log(2.0) / Q_HALF)
X_GRID.setflags(write=False)
Q_GRID = np.exp(X_GRID)

DEFAULT_SDOF_DAMPING = (0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20, 0.30)
SHIPPED_KO_BANDWIDTHS = (20.0, 30.0, 40.0, 50.0, 60.0, 80.0, 100.0)
GENERATOR = {"name": "hvsr_core.templates", "version": "bankgen-v1"}
BANK_LICENSE = "CC0-1.0 (generated numerical templates; no third-party data)"
SHIPPED_DIR = Path(__file__).with_name("banks")
MAX_NPZ_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024 * 1024
ALLOWED_ENTRIES = {"x", "log_templates", "param_values", "anchor_ratio", "modal_ratios", "modal_damping"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class BankError(ValueError):
    pass


# --------------------------------------------------------------------------- SDOF reference


def sdof_m(r: np.ndarray, zeta: float) -> np.ndarray:
    """|absolute mass displacement / harmonic base displacement|, r = f / fn."""
    r = np.asarray(r, dtype=float)
    two_zr = 2.0 * zeta * r
    return np.sqrt((1.0 + two_zr**2) / ((1.0 - r * r) ** 2 + two_zr**2))


def sdof_r_peak(zeta: float) -> float:
    """Location of the maximum of sdof_m in r (exact for the unsmoothed template)."""
    return math.sqrt(2.0 / (1.0 + math.sqrt(1.0 + 8.0 * zeta * zeta)))


# --------------------------------------------------------------------------- conditioning


def ko_continuous(h: Callable[[np.ndarray], np.ndarray], r: np.ndarray, b: float, support: float, n_quad: int = 4001) -> np.ndarray:
    """Continuous limit of Konno-Ohmachi smoothing on a LINEAR frequency axis.

    The discrete operator averages FFT bins uniformly spaced in f. With u = ln(f/fc) that is
    integral W(u) h(fc e^u) e^u du / integral W(u) e^u du, W(u) = [sin(b u / ln10) / (b u / ln10)]^4,
    truncated at |b log10(f/fc)| <= support. Scale invariant, so it applies on the r axis directly.
    """
    r = np.atleast_1d(np.asarray(r, dtype=float))
    umax = support * math.log(10.0) / b
    u = np.linspace(-umax, umax, n_quad)
    z = b * u / math.log(10.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        w = np.where(z == 0.0, 1.0, (np.sin(z) / z) ** 4)
    trap = np.full(n_quad, u[1] - u[0])
    trap[[0, -1]] *= 0.5
    wts = w * np.exp(u) * trap
    out = np.empty(r.size)
    chunk = max(1, 400_000 // n_quad)
    for a in range(0, r.size, chunk):
        rr = r[a : a + chunk]
        vals = h((rr[:, None] * np.exp(u)[None, :]).ravel()).reshape(rr.size, n_quad)
        out[a : a + chunk] = vals @ wts / wts.sum()
    return out


def local_peak(h: Callable[[np.ndarray], np.ndarray], lo: float, hi: float, n: int = 801) -> float | None:
    """Location of the largest interior local maximum of h on [lo, hi] (log-spaced search + refine)."""
    r = np.geomspace(lo, hi, n)
    v = h(r)
    i = int(np.argmax(v))
    if i == 0 or i == n - 1:
        return None
    res = optimize.minimize_scalar(lambda t: -float(h(np.array([math.exp(t)]))[0]), bounds=(math.log(r[i - 1]), math.log(r[i + 1])), method="bounded", options={"xatol": 1e-12})
    return float(math.exp(res.x))


def normalized_log_template(h: Callable[[np.ndarray], np.ndarray], r_anchor: float) -> np.ndarray:
    peak = float(h(np.array([r_anchor]))[0])
    vals = h(Q_GRID * r_anchor)
    out = np.log(vals / peak)
    out[Q_HALF] = 0.0
    return out


def conditioned(h: Callable[[np.ndarray], np.ndarray], conditioning: dict) -> Callable[[np.ndarray], np.ndarray]:
    if conditioning["kind"] == "raw":
        return h
    if conditioning["kind"] == "konno_ohmachi_continuous":
        b, s = conditioning["ko_bandwidth"], conditioning["ko_support_rad"]
        return lambda r: ko_continuous(h, r, b, s)
    raise BankError(f"unknown conditioning {conditioning['kind']!r}")


# --------------------------------------------------------------------------- bank objects


@dataclass(frozen=True)
class TemplateBank:
    manifest: dict
    x: np.ndarray
    log_t: np.ndarray
    param_values: np.ndarray
    anchor_ratio: np.ndarray
    modal_ratios: np.ndarray | None = None
    modal_damping: np.ndarray | None = None

    @property
    def bank_id(self) -> str:
        return self.manifest["bank_id"]

    @property
    def family(self) -> str:
        return self.manifest["family"]

    @property
    def n_templates(self) -> int:
        return int(self.log_t.shape[0])

    @property
    def param_names(self) -> list[str]:
        return list(self.manifest["parameters"]["names"])

    def provenance(self) -> dict:
        m = self.manifest
        return {
            "bank_id": m["bank_id"],
            "bank_version": m["bank_version"],
            "data_sha256": m["data_sha256"],
            "family": m["family"],
            "conditioning": m["conditioning"],
        }

    def template_label(self, k: int) -> dict:
        lab = {n: float(v) for n, v in zip(self.param_names, self.param_values[k], strict=True)}
        lab["template_index"] = int(k)
        lab["peak_to_reference_frequency_ratio"] = float(self.anchor_ratio[k])
        if self.modal_ratios is not None:
            lab["modal_frequency_ratios"] = [float(v) for v in self.modal_ratios[k]]
        return lab

    def boundary_mask(self, k: int) -> bool:
        """True when any parameter of template k sits at the edge of its grid."""
        for j in range(self.param_values.shape[1]):
            col = self.param_values[:, j]
            v = self.param_values[k, j]
            if np.unique(col).size > 1 and (v == col.min() or v == col.max()):
                return True
        return False

    @property
    def weights(self) -> np.ndarray:
        return np.full(Q_POINTS, 1.0 / Q_POINTS)


# --------------------------------------------------------------------------- serialization


def _npz_bytes(arrays: dict[str, np.ndarray]) -> bytes:
    """Deterministic NPZ (fixed timestamps, sorted entries, stored uncompressed)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name in sorted(arrays):
            arr = np.ascontiguousarray(arrays[name])
            if arr.dtype == object:
                raise BankError("object arrays are not allowed")
            body = io.BytesIO()
            np.lib.format.write_array(body, arr, allow_pickle=False)
            info = zipfile.ZipInfo(f"{name}.npy", date_time=FIXED_ZIP_TIME)
            info.external_attr = 0o644 << 16
            zf.writestr(info, body.getvalue())
    return buf.getvalue()


def _signing_payload(manifest: dict) -> bytes:
    return canonical_dumps({k: v for k, v in manifest.items() if k != "signature"}).encode()


def sign_manifest(manifest: dict, key: bytes, key_id: str) -> dict:
    sig = hmac.new(key, _signing_payload(manifest), hashlib.sha256).hexdigest()
    return dict(manifest, signature={"alg": "HMAC-SHA256", "key_id": key_id, "value": sig})


def verify_signature(manifest: dict, key: bytes) -> bool:
    sig = manifest.get("signature")
    if not sig or sig.get("alg") != "HMAC-SHA256":
        return False
    expect = hmac.new(key, _signing_payload(manifest), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, str(sig.get("value", "")))


def build_manifest(
    *,
    bank_id: str,
    bank_version: str,
    family: str,
    anchor: dict,
    conditioning: dict,
    parameters: dict,
    n_templates: int,
    data_bytes: bytes,
    damping_convention: str,
    created_utc: str,
    notes: str | None = None,
) -> dict:
    return {
        "schema_id": "hvsr-analyzer/template-bank",
        "schema_version": SCHEMA_VERSIONS["template_bank"],
        "bank_id": bank_id,
        "bank_version": bank_version,
        "family": family,
        "excitation": "harmonic_base_displacement",
        "output": "absolute_top_mass_displacement",
        "transfer_quantity": "abs_top_displacement_over_base_displacement",
        "anchor": anchor,
        "grid": {"kind": "uniform_ln_q", "n_points": Q_POINTS, "x_min": float(X_GRID[0]), "x_max": float(X_GRID[-1])},
        "conditioning": conditioning,
        "parameters": parameters,
        "damping_convention": damping_convention,
        "n_templates": n_templates,
        "generator": GENERATOR,
        "license": BANK_LICENSE,
        "data_file": f"{bank_id}.npz",
        "data_sha256": sha256_bytes(data_bytes),
        "created_utc": created_utc,
        "signature": None,
        "notes": notes,
    }


def write_bank(directory: Path, manifest: dict, data: bytes) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    npz = directory / manifest["data_file"]
    js = directory / f"{manifest['bank_id']}.json"
    npz.write_bytes(data)
    js.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return js, npz


# --------------------------------------------------------------------------- SDOF bank generation


def generate_sdof_bank(
    conditioning: dict,
    damping: tuple[float, ...] = DEFAULT_SDOF_DAMPING,
    bank_id: str | None = None,
    bank_version: str = "1.0.0",
    created_utc: str = "2026-10-20T00:00:00Z",
) -> tuple[dict, bytes]:
    logs, anchors = [], []
    for z in damping:
        h = conditioned(lambda r, z=z: sdof_m(r, z), conditioning)
        rp_raw = sdof_r_peak(z)
        rp = rp_raw if conditioning["kind"] == "raw" else local_peak(h, rp_raw * 0.6, rp_raw * 1.6)
        if rp is None:
            raise BankError(f"conditioned SDOF zeta={z} has no interior peak")
        logs.append(normalized_log_template(h, rp))
        anchors.append(rp)
    arrays = {
        "x": X_GRID.copy(),
        "log_templates": np.vstack(logs),
        "param_values": np.asarray(damping, dtype=float)[:, None],
        "anchor_ratio": np.asarray(anchors),
    }
    data = _npz_bytes(arrays)
    if bank_id is None:
        bank_id = "sdof-raw-v1" if conditioning["kind"] == "raw" else f"sdof-ko{conditioning['ko_bandwidth']:g}-v1"
    manifest = build_manifest(
        bank_id=bank_id,
        bank_version=bank_version,
        family="sdof",
        anchor={"rule": "global_maximum_after_conditioning", "mode": 1},
        conditioning=conditioning,
        parameters={
            "names": ["damping_ratio"],
            "damping_ratio": list(damping),
            "anchor_ratio_meaning": "template peak frequency / undamped natural frequency (r_peak)",
        },
        n_templates=len(damping),
        data_bytes=data,
        damping_convention="viscous_ratio",
        created_utc=created_utc,
        notes="Phenomenological SDOF base-excitation templates. Damping is a shape label, not a site damping estimate.",
    )
    return manifest, data


def ko_conditioning(b: float, support: float = 3.0) -> dict:
    return {"kind": "konno_ohmachi_continuous", "ko_bandwidth": float(b), "ko_support_rad": float(support)}


RAW = {"kind": "raw", "ko_bandwidth": None, "ko_support_rad": None}


# --------------------------------------------------------------------------- safe import


def _check_zip(data: bytes) -> dict[str, np.ndarray]:
    if len(data) > MAX_NPZ_BYTES:
        raise BankError("bank data file too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BankError("bank data is not a valid NPZ/zip archive") from e
    arrays: dict[str, np.ndarray] = {}
    total = 0
    for info in zf.infolist():
        name = info.filename
        if "/" in name or "\\" in name or name.startswith(".") or ".." in name or not name.endswith(".npy"):
            raise BankError(f"unsafe or unexpected archive entry {name!r}")
        key = name[:-4]
        if key not in ALLOWED_ENTRIES:
            raise BankError(f"unexpected array {key!r} in bank")
        if info.file_size > MAX_ENTRY_BYTES:
            raise BankError(f"array {key!r} too large")
        total += info.file_size
        if total > MAX_NPZ_BYTES:
            raise BankError("bank arrays too large in total")
        try:
            arr = np.lib.format.read_array(io.BytesIO(zf.read(info)), allow_pickle=False)
        except ValueError as e:
            raise BankError(f"array {key!r} rejected: {e}") from e
        if arr.dtype.kind not in "fiu":
            raise BankError(f"array {key!r} has non-numeric dtype {arr.dtype}")
        arrays[key] = np.asarray(arr, dtype=np.float64)
    return arrays


def load_bank_bytes(manifest: dict, data: bytes, *, hmac_key: bytes | None = None, require_signature: bool = False) -> TemplateBank:
    errs = validation_errors(manifest, "template_bank")
    if errs:
        raise BankError("manifest invalid: " + "; ".join(errs[:5]))
    if sha256_bytes(data) != manifest["data_sha256"]:
        raise BankError("bank data SHA-256 does not match the manifest")
    if require_signature and (hmac_key is None or not verify_signature(manifest, hmac_key)):
        raise BankError("bank signature missing or invalid")
    if hmac_key is not None and manifest.get("signature") and not verify_signature(manifest, hmac_key):
        raise BankError("bank signature invalid")
    a = _check_zip(data)
    for req in ("x", "log_templates", "param_values", "anchor_ratio"):
        if req not in a:
            raise BankError(f"bank lacks array {req!r}")
    x, lt, pv, ar = a["x"], a["log_templates"], a["param_values"], a["anchor_ratio"]
    k = manifest["n_templates"]
    names = manifest["parameters"].get("names")
    if not isinstance(names, list) or not names:
        raise BankError("manifest parameters.names must list the template parameters")
    if x.shape != (Q_POINTS,) or not np.allclose(x, X_GRID, rtol=0, atol=1e-14):
        raise BankError("bank x grid differs from the fixed 129-point ln-q grid")
    if lt.shape != (k, Q_POINTS) or pv.shape != (k, len(names)) or ar.shape != (k,):
        raise BankError("bank array shapes inconsistent with manifest")
    for nm, arr in (("log_templates", lt), ("param_values", pv), ("anchor_ratio", ar)):
        if not np.all(np.isfinite(arr)):
            raise BankError(f"non-finite values in {nm}")
    if np.any(np.abs(lt[:, Q_HALF]) > 1e-12):
        raise BankError("templates are not normalized to amplitude 1 at q = 1")
    if np.any(lt[:, Q_HALF - 1] >= 0) or np.any(lt[:, Q_HALF + 1] >= 0):
        raise BankError("designated peak at q = 1 is not a strict local maximum")
    if np.any(ar <= 0) or np.any(lt > 50) or np.any(lt < -50):
        raise BankError("template values outside the admissible numerical domain")
    mr = a.get("modal_ratios")
    md = a.get("modal_damping")
    if manifest["family"] != "sdof":
        if mr is None or md is None or mr.shape[0] != k or md.shape != mr.shape:
            raise BankError("MDOF bank requires modal_ratios and modal_damping arrays")
        if not (np.all(np.isfinite(mr)) and np.all(mr > 0) and np.all((md > 0) & (md < 1))):
            raise BankError("MDOF modal data outside admissible domain")
    for arr in (x, lt, pv, ar):
        arr.setflags(write=False)
    return TemplateBank(manifest, x, lt, pv, ar, mr, md)


def load_bank_files(json_path: Path, **kw) -> TemplateBank:
    manifest = json.loads(Path(json_path).read_text(encoding="utf-8"))
    data_file = manifest.get("data_file", "")
    if not isinstance(data_file, str) or "/" in data_file or "\\" in data_file or ".." in data_file:
        raise BankError("unsafe data_file in manifest")
    return load_bank_bytes(manifest, (Path(json_path).parent / data_file).read_bytes(), **kw)


def bank_dirs() -> list[Path]:
    dirs = [SHIPPED_DIR]
    extra = os.environ.get("HVSR_BANK_DIRS", "")
    dirs += [Path(p) for p in extra.split(os.pathsep) if p]
    return dirs


@lru_cache(maxsize=64)
def _load_cached(path: str, mtime: float) -> TemplateBank:
    return load_bank_files(Path(path))


def get_bank(bank_id: str) -> TemplateBank:
    for d in bank_dirs():
        p = d / f"{bank_id}.json"
        if p.is_file():
            return _load_cached(str(p), p.stat().st_mtime)
    raise BankError(f"template bank {bank_id!r} not found")


def available_banks() -> list[str]:
    out = set()
    for d in bank_dirs():
        if d.is_dir():
            out.update(p.stem for p in d.glob("*.json"))
    return sorted(out)


def resolve_sdof_bank(template_bank: str, ko_bandwidth: float) -> tuple[TemplateBank, list[str]]:
    """'auto' -> conditioned bank for this b if shipped, else the raw bank with a warning."""
    if template_bank != "auto":
        return get_bank(template_bank), []
    bid = f"sdof-ko{ko_bandwidth:g}-v1"
    if bid in available_banks():
        return get_bank(bid), []
    return get_bank("sdof-raw-v1"), [
        f"no SDOF bank conditioned for Konno-Ohmachi b = {ko_bandwidth:g}; raw templates used (shape mismatch possible)"
    ]
