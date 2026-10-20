"""Deterministic synthetic three-component ambient-noise generator with KNOWN H/V properties.

Construction: each component is independent Gaussian noise shaped in the frequency domain by a
common base amplitude spectrum B(f). Horizontal components are additionally multiplied by a site
transfer amplitude |T(f)|. Because N, E, V share B(f) and are statistically independent,
E[(N^2 + E^2)/2] / E[V^2] = |T(f)|^2, so the expected (RMS-combined) H/V amplitude ratio is |T(f)|
before smoothing and estimator bias. Optional contaminations (machine tones, transients, slow
amplitude episodes, regime switches, horizontal gain mismatch) are added explicitly and recorded in
the returned truth dictionary.

These signals are research fixtures, not physical simulations of a real wavefield. The H/V truth
is a property of the construction, not of any geological site. Generated data are redistributable
under the repository license.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..model import ChannelMeta, Recording, RecordingDiagnostics, Segment

GENERATOR_VERSION = "synthetic-3c-v1"


@dataclass(frozen=True)
class TFSpec:
    kind: str
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "params": dict(self.params)}


def sdof_transmissibility(r: np.ndarray, zeta: float) -> np.ndarray:
    """|absolute displacement / base displacement| for a linear SDOF, r = f/fn."""
    r = np.asarray(r, dtype=float)
    num = 1.0 + (2.0 * zeta * r) ** 2
    den = (1.0 - r**2) ** 2 + (2.0 * zeta * r) ** 2
    return np.sqrt(num / den)


def layered_sh_transfer(f: np.ndarray, layers: list[dict], halfspace: dict) -> np.ndarray:
    """|surface / outcropping-rock| SH amplitude for vertically incident waves (Thomson-Haskell).

    layers: [{"vs": m/s, "h": m, "rho": kg/m3, "damping": ratio}, ...] top to bottom.
    halfspace: {"vs":..., "rho":..., "damping":...}.
    """
    f = np.asarray(f, dtype=float)
    w = 2.0 * np.pi * f
    # complex shear moduli
    def cvs(layer: dict) -> np.ndarray:
        return layer["vs"] * (1.0 + 1j * layer.get("damping", 0.0))

    # Up/down-going amplitudes A (up) and B (down); free surface: A1 = B1 = 1.
    A = np.ones_like(w, dtype=complex)
    B = np.ones_like(w, dtype=complex)
    for i, layer in enumerate(layers):
        vs_i = cvs(layer)
        k_i = w / vs_i
        nxt = layers[i + 1] if i + 1 < len(layers) else halfspace
        vs_n = cvs(nxt)
        alpha = (layer["rho"] * vs_i) / (nxt["rho"] * vs_n)
        e_pos = np.exp(1j * k_i * layer["h"])
        e_neg = np.exp(-1j * k_i * layer["h"])
        A_n = 0.5 * A * (1 + alpha) * e_pos + 0.5 * B * (1 - alpha) * e_neg
        B_n = 0.5 * A * (1 - alpha) * e_pos + 0.5 * B * (1 + alpha) * e_neg
        A, B = A_n, B_n
    # surface motion = A1 + B1 = 2 ; outcrop motion = 2 * A_halfspace
    return np.abs(2.0 / (2.0 * A))


def transfer_amplitude(spec: TFSpec, f: np.ndarray) -> np.ndarray:
    f = np.asarray(f, dtype=float)
    p = spec.params
    if spec.kind == "flat":
        return np.ones_like(f)
    if spec.kind == "sdof":
        return sdof_transmissibility(f / p["fn"], p["zeta"])
    if spec.kind in ("lognormal_peak", "asymmetric_peak"):
        fc, amp = p["fc"], p["amplitude"]
        wl = p.get("width_left_decades", p.get("width_decades"))
        wr = p.get("width_right_decades", p.get("width_decades"))
        x = np.log10(np.maximum(f, 1e-300) / fc)
        w = np.where(x < 0, wl, wr)
        return 1.0 + (amp - 1.0) * np.exp(-0.5 * (x / w) ** 2)
    if spec.kind == "multi_peak":
        out = np.ones_like(f)
        for pk in p["peaks"]:
            x = np.log10(np.maximum(f, 1e-300) / pk["fc"])
            out = out + (pk["amplitude"] - 1.0) * np.exp(-0.5 * (x / pk["width_decades"]) ** 2)
        return out
    if spec.kind == "layered_sh":
        return layered_sh_transfer(f, p["layers"], p["halfspace"])
    raise ValueError(f"unknown transfer kind {spec.kind!r}")


def _base_amplitude(f: np.ndarray, alpha: float, f_lo: float, f_hi: float) -> np.ndarray:
    """Band-limited base amplitude spectrum ~ f^(-alpha/2) with smooth cosine edges."""
    out = np.zeros_like(f)
    pos = f > 0
    out[pos] = f[pos] ** (-alpha / 2.0)
    lo_edge = (f >= f_lo / 2) & (f < f_lo)
    out[f < f_lo / 2] = 0.0
    out[lo_edge] *= 0.5 * (1 - np.cos(np.pi * (f[lo_edge] - f_lo / 2) / (f_lo / 2)))
    hi_edge = (f > f_hi) & (f <= 1.25 * f_hi)
    out[f > 1.25 * f_hi] = 0.0
    out[hi_edge] *= 0.5 * (1 + np.cos(np.pi * (f[hi_edge] - f_hi) / (0.25 * f_hi)))
    return out


def generate_3c(
    *,
    duration_s: float,
    fs: float,
    tf: TFSpec,
    seed: int,
    noise_alpha: float = 0.0,
    band_hz: tuple[float, float] | None = None,
    horizontal_gain_mismatch: float = 1.0,
    tones: list[dict] | None = None,
    transients: list[dict] | None = None,
    episodes: dict | None = None,
    regimes: list[dict] | None = None,
    scale: float = 1000.0,
    integer: bool = False,
    t0_utc_ns: int | None = 1_790_000_000 * 1_000_000_000,
) -> tuple[Recording, dict[str, Any]]:
    """Generate a synthetic recording and its truth record.

    tones: [{"freq_hz", "amplitude_rel", "components": "NEZ"}] - sinusoid amplitude relative to the
        component's noise RMS.
    transients: [{"time_s", "amplitude_rel", "duration_s"}] - Ricker-like bursts on all components.
    episodes: {"tau_s", "sigma_ln"} - slow common log-amplitude modulation (AR(1)).
    regimes: [{"start_s", "end_s", "tf": TFSpec}] - alternative horizontal transfer in a time span.
    """
    n = int(round(duration_s * fs))
    rng = np.random.default_rng(seed)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    lo, hi = band_hz if band_hz is not None else (0.05, 0.4 * fs)
    base = _base_amplitude(f, noise_alpha, lo, hi)
    tf_amp = transfer_amplitude(tf, f)

    def shaped(amp: np.ndarray) -> np.ndarray:
        white = rng.standard_normal(n)
        return np.fft.irfft(np.fft.rfft(white) * amp, n=n)

    north = shaped(base * tf_amp)
    east = shaped(base * tf_amp)
    vert = shaped(base)
    regime_truth = []
    if regimes:
        t = np.arange(n) / fs
        for reg in regimes:
            alt = transfer_amplitude(reg["tf"], f)
            n_alt = shaped(base * alt)
            e_alt = shaped(base * alt)
            mask = (t >= reg["start_s"]) & (t < reg["end_s"])
            north = np.where(mask, n_alt, north)
            east = np.where(mask, e_alt, east)
            regime_truth.append({"start_s": reg["start_s"], "end_s": reg["end_s"], "tf": reg["tf"].as_dict()})

    east = east * horizontal_gain_mismatch
    data = np.vstack([north, east, vert])
    rms = data.std(axis=1, keepdims=True)
    rms[rms == 0] = 1.0
    data = data / rms[2]  # normalise by vertical RMS (keeps H/V ratio intact)

    if episodes:
        tau, sig = episodes["tau_s"], episodes["sigma_ln"]
        step = max(1, int(fs))  # one AR step per second
        m = n // step + 2
        phi = np.exp(-1.0 / tau)
        z = np.empty(m)
        z[0] = rng.standard_normal() * sig
        innov = rng.standard_normal(m) * sig * np.sqrt(1 - phi**2)
        for i in range(1, m):
            z[i] = phi * z[i - 1] + innov[i]
        env = np.exp(np.interp(np.arange(n) / step, np.arange(m), z))
        data = data * env

    comp_rms = data.std(axis=1)
    tone_truth = []
    for tone in tones or []:
        t = np.arange(n) / fs
        phase = rng.uniform(0, 2 * np.pi)
        for ci, comp in enumerate("NEZ"):
            if comp in tone.get("components", "NEZ"):
                data[ci] += tone["amplitude_rel"] * comp_rms[ci] * np.sin(2 * np.pi * tone["freq_hz"] * t + phase)
        tone_truth.append(dict(tone))

    transient_truth = []
    for tr in transients or []:
        t = np.arange(n) / fs - tr["time_s"]
        sigma = tr.get("duration_s", 1.0) / 4
        wav = (1 - (t / sigma) ** 2) * np.exp(-0.5 * (t / sigma) ** 2)
        for ci in range(3):
            data[ci] += tr["amplitude_rel"] * comp_rms[ci] * wav * rng.choice([-1.0, 1.0])
        transient_truth.append(dict(tr))

    data = data * scale
    dtype = "float64"
    if integer:
        data = np.round(data).astype(np.int32).astype(np.float64)
        dtype = "int32"

    chans = (
        ChannelMeta("N", "N", quantity="counts", units="counts"),
        ChannelMeta("E", "E", quantity="counts", units="counts"),
        ChannelMeta("Z", "Z", quantity="counts", units="counts"),
    )
    rec = Recording(
        sampling_rate_hz=float(fs),
        t0_utc_ns=t0_utc_ns,
        t0_source="declared" if t0_utc_ns is not None else "unknown",
        channels=chans,
        segments=[Segment(0, 0.0, np.ascontiguousarray(data))],
        sampling_rate_source="declared",
        sample_dtype=dtype,  # type: ignore[arg-type]
        diagnostics=RecordingDiagnostics(),
        source={"format": "synthetic", "generator": GENERATOR_VERSION, "seed": seed},
    )
    fgrid = np.logspace(np.log10(max(lo, 1e-3)), np.log10(hi), 4000)
    tf_grid = transfer_amplitude(tf, fgrid)
    truth = {
        "generator": GENERATOR_VERSION,
        "seed": seed,
        "duration_s": duration_s,
        "fs": fs,
        "tf": tf.as_dict(),
        "noise_alpha": noise_alpha,
        "band_hz": [lo, hi],
        "horizontal_gain_mismatch": horizontal_gain_mismatch,
        "tones": tone_truth,
        "transients": transient_truth,
        "episodes": episodes,
        "regimes": regime_truth,
        "tf_peak_frequency_hz": float(fgrid[int(np.argmax(tf_grid))]),
        "tf_peak_amplitude": float(tf_grid.max()),
    }
    return rec, truth
