"""Stage 2: per-window and mean H/V against hvsrpy 2.1.0 under matched settings.

hvsrpy (GPL-3.0) is a test-only oracle. Matched settings: identical window samples (our half-open
integer windows), linear detrend (hvsrpy TimeSeries.detrend), Tukey 0.1 (hvsrpy window), FFT length =
window length (fft_settings n=None disables hvsrpy's zero padding to >= 2^15), RMS ("squared_average")
or geometric ("geometric_mean") horizontal combination, Konno-Ohmachi b = 40 evaluated by hvsrpy at
our grid frequencies, lognormal mean curve.

Tolerance: relative 1e-9. Both implementations evaluate the same formula in float64; differences
come only from summation order (sparse matrix product vs. hvsrpy's numba loop) and from our explicit
mean removal before detrending (algebraically a no-op for a linear detrend). Observed differences are
~1e-13; 1e-9 leaves margin without admitting any method difference.
"""

from __future__ import annotations

import numpy as np
import pytest

from hvsr_core.config import AnalysisConfig
from hvsr_core.core import run_core
from hvsr_core.curves import geometric_mean_curve
from hvsr_core.io.csv_reader import CSVOptions, read_csv
from hvsr_core.io.mseed_reader import read_miniseed

hvsrpy = pytest.importorskip("hvsrpy")
pytestmark = pytest.mark.reference

RTOL = 1e-9


def _load(fixture_dir, name):
    raw = (fixture_dir / name).read_bytes()
    if name.endswith(".mseed"):
        return read_miniseed([(name, raw)])
    rec, _ = read_csv(raw, CSVOptions(layout="elapsed_time"), filename=name)
    return rec


def _hvsrpy_curves(run, method, cols):
    from hvsrpy import SeismicRecording3C, TimeSeries
    from hvsrpy.processing import traditional_hvsr_processing
    from hvsrpy.settings import HvsrTraditionalProcessingSettings

    win = run.window
    dt = 1.0 / run.prep.fs
    records = []
    for k in range(win.count):
        seg = run.prep.segments[int(win.segment_pos[k])].data
        s = int(win.local_start[k])
        comps = [TimeSeries(seg[c, s : s + win.n_samples].copy(), dt) for c in range(3)]
        rec3 = SeismicRecording3C(comps[0], comps[1], comps[2])
        for ts in (rec3.ns, rec3.ew, rec3.vt):
            ts.detrend(type="linear")
        records.append(rec3)
    settings = HvsrTraditionalProcessingSettings(
        window_type_and_width=("tukey", 0.1),
        smoothing=dict(
            operator="konno_and_ohmachi", bandwidth=40, center_frequencies_in_hz=run.grid.freq[cols].tolist()
        ),
        method_to_combine_horizontals=method,
        fft_settings=dict(n=None),
    )
    return traditional_hvsr_processing(records, settings)


@pytest.mark.acceptance("AC-S2-04")
@pytest.mark.parametrize(
    ("fixture", "ours", "theirs"),
    [
        ("sdof2hz_elapsed_50hz.csv.gz", "rms", "squared_average"),
        ("lognormal3hz_steim2_50hz.mseed", "rms", "squared_average"),
        ("lognormal3hz_steim2_50hz.mseed", "geometric", "geometric_mean"),
    ],
)
def test_matches_hvsrpy(fixture_dir, fixture, ours, theirs):
    rec = _load(fixture_dir, fixture)
    cfg = AnalysisConfig.model_validate(
        {"windowing": {"window_length_s": 50, "step_s": 50}, "spectral": {"horizontal_combination": ours}}
    )
    run = run_core(rec, cfg)
    # hvsrpy returns 0/0 = NaN (and then refuses the curve) where no FFT bin is in the support
    cols = np.flatnonzero(run.grid.smoothing_bins > 0)
    ref = _hvsrpy_curves(run, theirs, cols)
    assert ref.amplitude.shape == (run.window.count, cols.size)
    np.testing.assert_allclose(ref.frequency, run.grid.freq[cols], rtol=0)
    ours_hv = run.spectra.hv[:, cols]
    ok = run.spectra.valid[:, cols]
    assert ok.sum() > 0.6 * run.spectra.valid.size
    np.testing.assert_allclose(ours_hv[ok], ref.amplitude[ok], rtol=RTOL)
    rows = np.arange(run.window.count)
    mean = geometric_mean_curve(run.spectra, rows)
    ref_mean = ref.mean_curve(distribution="lognormal")
    m = (mean.valid & (mean.count == rows.size))[cols]
    np.testing.assert_allclose(mean.hv[cols][m], ref_mean[m], rtol=RTOL)
    # every point we mark invalid lacks smoothing support or is above our anti-alias limit
    bad = ~run.grid.valid
    assert set(np.array(run.grid.invalid_reason, dtype=object)[bad]) <= {
        "insufficient_smoothing_bins",
        "above_anti_alias_limit",
    }
