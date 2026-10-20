"""Linear n-DOF shear chains under harmonic base displacement (offline template generator).

Masses m_1 (bottom) ... m_n (top); spring k_1 connects mass 1 to the base, k_i connects masses i-1, i.
Relative coordinates U, base displacement Y:  D(w) U = w^2 M 1 Y,  D(w) = K - w^2 M + i w C.
Output: |absolute top displacement / base displacement| = |1 + e_top^T U / Y|.

Parameters are dimensionless: masses relative to m_1, stiffnesses relative to k_1, and the system is
scaled so that the first undamped natural frequency is 1 (r = f / f_1). Dimensionless damping is
therefore preserved under frequency scaling.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np
from scipy import linalg

from .templates import (
    Q_HALF,
    X_GRID,
    BankError,
    _npz_bytes,
    build_manifest,
    conditioned,
    local_peak,
    normalized_log_template,
)


@dataclass
class ShearChain:
    M: np.ndarray
    K: np.ndarray
    C: np.ndarray
    omega: np.ndarray  # undamped natural circular frequencies (first = 1)
    phi: np.ndarray  # mass-normalized mode shapes (columns)
    modal_damping: np.ndarray
    damping_convention: str


def chain_matrices(mass_ratios: list[float], stiffness_ratios: list[float]) -> tuple[np.ndarray, np.ndarray]:
    m = np.asarray([1.0, *mass_ratios], dtype=float)
    k = np.asarray([1.0, *stiffness_ratios], dtype=float)
    if m.size != k.size:
        raise BankError("mass and stiffness ratio lists must have equal length")
    if np.any(m <= 0) or np.any(k <= 0):
        raise BankError("masses and stiffnesses must be positive")
    n = m.size
    K = np.zeros((n, n))
    for i in range(n):
        K[i, i] += k[i]
        if i + 1 < n:
            K[i, i] += k[i + 1]
            K[i, i + 1] = K[i + 1, i] = -k[i + 1]
    return np.diag(m), K


def build_chain(
    mass_ratios: list[float],
    stiffness_ratios: list[float],
    damping: dict,
) -> ShearChain:
    """damping: {"kind": "modal", "ratios": [z1, z2, ...] | z} or {"kind": "rayleigh", "zeta_1": z1, "zeta_2": z2}."""
    M, K = chain_matrices(mass_ratios, stiffness_ratios)
    w2, phi = linalg.eigh(K, M)
    if np.any(w2 <= 0):
        raise BankError("non-positive stiffness eigenvalue")
    w = np.sqrt(w2)
    s = 1.0 / w[0]  # frequency scale so that omega_1 = 1: K -> s^2 K
    K = K * s * s
    w = w * s
    phi = phi / np.sqrt(np.einsum("ij,ij->j", phi, M @ phi))
    n = w.size
    if damping["kind"] == "modal":
        z = np.broadcast_to(np.asarray(damping["ratios"], dtype=float), (n,)).copy()
        C = M @ phi @ np.diag(2.0 * z * w) @ phi.T @ M
        conv = "modal"
    elif damping["kind"] == "rayleigh":
        if n < 2:
            raise BankError("Rayleigh damping needs at least two modes")
        z1, z2 = damping["zeta_1"], damping["zeta_2"]
        w1, w2_ = w[0], w[1]
        alpha = 2.0 * w1 * w2_ * (z1 * w2_ - z2 * w1) / (w2_**2 - w1**2)
        beta = 2.0 * (z2 * w2_ - z1 * w1) / (w2_**2 - w1**2)
        C = alpha * M + beta * K
        z = alpha / (2.0 * w) + beta * w / 2.0
        conv = f"rayleigh(alpha={alpha:.6g},beta={beta:.6g})"
    else:
        raise BankError(f"unknown damping kind {damping['kind']!r}")
    # actual modal damping and classical-damping check
    cm = phi.T @ C @ phi
    actual = np.diag(cm) / (2.0 * w)
    off = cm - np.diag(np.diag(cm))
    if np.max(np.abs(off)) > 1e-9 * max(1.0, np.max(np.abs(cm))):
        raise BankError("damping is not classical (modal superposition invalid)")
    if not np.allclose(actual, z, rtol=1e-9, atol=1e-12):
        raise BankError("declared and actual modal damping differ")
    if np.any(actual <= 0) or np.any(actual >= 1):
        raise BankError("modal damping ratios must lie in (0, 1)")
    # stability: eigenvalues of the first-order system have negative real parts
    n_ = M.shape[0]
    A = np.block([[np.zeros((n_, n_)), np.eye(n_)], [-linalg.solve(M, K), -linalg.solve(M, C)]])
    if np.max(np.linalg.eigvals(A).real) >= 0:
        raise BankError("unstable system")
    return ShearChain(M, K, C, w, phi, actual, conv)


def top_response_direct(ch: ShearChain, r: np.ndarray) -> np.ndarray:
    """|1 + e_top^T solve(D(w), w^2 M 1)| by batched complex linear solves."""
    r = np.atleast_1d(np.asarray(r, dtype=float))
    n = ch.M.shape[0]
    w = r[:, None, None]
    D = ch.K[None] - (w**2) * ch.M[None] + 1j * w * ch.C[None]
    rhs = (r**2)[:, None] * (ch.M @ np.ones(n))[None, :]
    u = np.linalg.solve(D, rhs[..., None])[..., 0]
    return np.abs(1.0 + u[:, -1])


def top_response_modal(ch: ShearChain, r: np.ndarray) -> np.ndarray:
    """Independent modal-superposition solution (valid for classical damping)."""
    r = np.atleast_1d(np.asarray(r, dtype=float))
    gamma = ch.phi.T @ ch.M @ np.ones(ch.M.shape[0])  # mass-normalized participation
    w = ch.omega[None, :]
    z = ch.modal_damping[None, :]
    rr = r[:, None]
    q = (rr**2) * gamma[None, :] / (w**2 - rr**2 + 2j * z * w * rr)
    return np.abs(1.0 + q @ ch.phi[-1, :])


@dataclass
class Anchor:
    r: float | None
    status: str
    reason: str = ""


def first_mode_anchor(h, omega: np.ndarray) -> Anchor:
    """First-mode-associated local peak: the largest local maximum of h in (0.3, sqrt(w1 w2))."""
    hi = math.sqrt(omega[0] * omega[1]) if omega.size > 1 else 3.0
    if hi <= 1.05:
        return Anchor(None, "ambiguous", "first and second modes too close to separate")
    rp = local_peak(h, 0.3, hi)
    if rp is None:
        return Anchor(None, "ambiguous", "no interior local maximum associated with mode 1")
    if abs(math.log(rp)) > 0.5 * math.log(hi):
        return Anchor(None, "ambiguous", "local maximum is not associated with mode 1")
    return Anchor(rp, "ok")


def generate_mdof_bank(
    n_dof: int,
    conditioning: dict,
    mass_ratio_grid: tuple[float, ...],
    stiffness_ratio_grid: tuple[float, ...],
    damping_grid: tuple[float, ...],
    bank_id: str,
    bank_version: str = "1.0.0",
    created_utc: str = "2026-10-20T00:00:00Z",
) -> tuple[dict, bytes]:
    """Uniform-ratio chains: every upper mass/stiffness ratio equals the grid value; equal modal damping."""
    if n_dof not in (2, 3):
        raise BankError("n_dof must be 2 or 3")
    logs, params, anchors, ratios, damps, excluded = [], [], [], [], [], []
    for mu, ka, z in itertools.product(mass_ratio_grid, stiffness_ratio_grid, damping_grid):
        ch = build_chain([mu] * (n_dof - 1), [ka] * (n_dof - 1), {"kind": "modal", "ratios": z})
        h = conditioned(lambda r, ch=ch: top_response_direct(ch, r), conditioning)
        anc = first_mode_anchor(h, ch.omega)
        if anc.r is None:
            excluded.append(
                {"mass_ratio": mu, "stiffness_ratio": ka, "damping_ratio": z, "reason": anc.reason}
            )
            continue
        lt = normalized_log_template(h, anc.r)
        if lt[Q_HALF - 1] >= 0 or lt[Q_HALF + 1] >= 0:
            excluded.append(
                {
                    "mass_ratio": mu,
                    "stiffness_ratio": ka,
                    "damping_ratio": z,
                    "reason": "anchor not a strict grid maximum",
                }
            )
            continue
        logs.append(lt)
        params.append([mu, ka, z])
        anchors.append(anc.r)
        ratios.append(ch.omega / ch.omega[0])
        damps.append(ch.modal_damping)
    if not logs:
        raise BankError("no admissible templates")
    arrays = {
        "x": X_GRID.copy(),
        "log_templates": np.vstack(logs),
        "param_values": np.asarray(params),
        "anchor_ratio": np.asarray(anchors),
        "modal_ratios": np.vstack(ratios),
        "modal_damping": np.vstack(damps),
    }
    data = _npz_bytes(arrays)
    family = {2: "mdof2", 3: "mdof3"}[n_dof]
    manifest = build_manifest(
        bank_id=bank_id,
        bank_version=bank_version,
        family=family,
        anchor={"rule": "first_mode_local_peak_after_conditioning", "mode": 1},
        conditioning=conditioning,
        parameters={
            "names": ["mass_ratio", "stiffness_ratio", "damping_ratio"],
            "mass_ratio": list(mass_ratio_grid),
            "stiffness_ratio": list(stiffness_ratio_grid),
            "damping_ratio": list(damping_grid),
            "chain": "uniform ratios: m_i/m_1 = mass_ratio, k_i/k_1 = stiffness_ratio for i >= 2",
            "frequency_normalization": "first undamped natural frequency = 1",
            "anchor_ratio_meaning": "anchor (first-mode peak) frequency / first undamped natural frequency",
            "excluded": excluded,
        },
        n_templates=len(logs),
        data_bytes=data,
        damping_convention="modal_equal_ratio",
        created_utc=created_utc,
        notes="Shear-chain top-mass absolute displacement over base displacement. Shapes only, not structural identification.",
    )
    return manifest, data
