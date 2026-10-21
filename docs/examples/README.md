# Example exports

Real exports from one analysis, written by `scripts/export_examples.py`. The files are saved exactly
as downloaded from their stable artifact URLs; nothing was edited afterwards. Measurement IDs, run
IDs, record codes, timestamps, and URLs belong to the throwaway local site the script creates, so
they do not resolve on any public server.

The recording is synthetic. It is the response of a single-degree-of-freedom system (fn = 2 Hz,
damping 0.2) to random excitation, 1800 s at 50 Hz, generator seed 21. It was uploaded as an
elapsed-time CSV.GZ at (40.416775, -3.70379) and analyzed with the default request, except that the
1.5 x Lw robustness run is off (`{"robustness_check": false}`). It is long enough for the nominal
95% band to be estimated. The band is labelled "Estimated 95% pointwise CI for the selected-window
mean". It is selection-conditional and not calibrated; see `calibration` in the analysis JSON and
`docs/validation/report-v1.md`.

| File | Contents | Contract |
| --- | --- | --- |
| `manifest.json` | Every artifact of the measurement: URL, MIME type, size, SHA-256, availability, source references, plot recipes | schema `manifest` |
| `analysis_sdof.json` | SDOF-selected method: peak families, template scores, selection, mean curve and band, peak statistics | schema `analysis` |
| `analysis_standard.json` | Standard method (shape-neutral reference) | schema `analysis` |
| `mean_spectrum_sdof.csv`, `mean_spectrum_standard.csv` | Mean H/V per selection and frequency, with the band, count, and status; blank cells carry a reason | table `mean_spectrum` |
| `window_table_sdof.csv`, `window_table_standard.csv` | Every candidate window, selected or not, with QC, eligibility, ranking, and outcome | tables `window_table_sdof`, `window_table_standard` |
| `plot_sdof.svg` | Published SDOF figure (desktop layout) | recipe in the manifest entry's `generated_by.recipe` |
| `data_dictionary.json` | The data dictionary served at `/api/v1/data-dictionary` | schema `data_dictionary`; also `docs/data-dictionary.md` |

Two files are omitted to keep the repository small: the per-window spectra (about 1.8 MB
compressed for this record) and the normalized waveform CSV. Both are generated on request from the
stable URLs listed in the manifest, and the Stage 8 tests validate both.

Regenerate with `uv run python scripts/export_examples.py`. `tests/stage_08/test_examples.py`
checks that every file validates and matches its manifest checksum. It also re-analyzes the same
recording and requires identical numbers.
