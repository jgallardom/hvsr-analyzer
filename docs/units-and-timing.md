# Units, timing, and numerical conventions

Applies to the scientific core (`hvsr_core`), its exports, and the web API.

## Time

- All absolute times are UTC. Internally they are integer nanoseconds since the Unix epoch;
  externally they are ISO 8601 strings ending in `Z`, e.g. `2026-10-20T17:57:00.25Z`.
- Uploaded timestamps must carry an explicit offset (`Z` or `+HH:MM`). Timestamps without an offset
  are rejected unless the uploader explicitly declares that they are UTC. The time zone is never
  inferred.
- Relative analysis times (`tmin_s`, `tmax_s`, window bounds) are seconds after the first canonical
  sample of the recording.
- Sample intervals are half-open `[start, stop)` integer indices on the recording's sample clock.
  Time bounds are derived as `t0 + index / fs`. They are never accumulated by repeated floating
  addition.
- `tmin_s` maps to `ceil(tmin_s * fs)` and `tmax_s` maps to `floor(tmax_s * fs)` (half-open stop), so
  the analyzed span never extends beyond the requested one.
- Window length and step are rounded to integer sample counts (`round(Lw * fs)`); the actual values
  `n / fs` are recorded in every analysis.

## Decimal year (record code)

`decimal_year = Y + (t - Jan1(Y)) / (Jan1(Y+1) - Jan1(Y))` in UTC, using the actual duration of
year `Y` (365 or 366 days). It is rounded to four decimals for the record code. The source is
`recording_start` when a trustworthy recording start exists, otherwise `upload_time`; the source is
stored with the record.

## Frequency

- FFT resolution is `fs / n_window = 1 / Lw` (no zero padding by default). It is reported separately
  from the analysis grid spacing.
- The analysis grid is anchored: `f_i = 10^(i / P)` for integer `i`, `P` points per decade (default
  200). Runs with the same `P` share grid points, which makes robustness comparisons exact.
- Frequencies are in Hz everywhere (`*_hz`).

## Amplitudes

- Fourier amplitudes are `|rfft(x_tapered)| / fs` (continuous-transform scaling). The scale cancels
  in H/V.
- H/V is dimensionless. Logarithms in statistics and template scoring are natural logarithms.
  SESAME dispersion is reported as a multiplicative factor (`exp(std(ln A))`, identical to
  `10^std(log10 A)`).
- Invalid values are `null` (JSON) or empty (CSV) together with a reason. They are never zero, NaN,
  or Infinity in published documents.

## Sample values and units

- Canonical numerical samples are IEEE-754 binary64 (or int32 when every uploaded value is an integer
  literal in range). Decimal text is parsed with correctly rounded binary64 conversion; the decimal
  formatting itself is not part of the canonical preservation contract.
- Recorded quantity is one of `counts`, `velocity`, `acceleration`, `displacement`, `other`,
  `unknown`; units are free text as declared (e.g. `m/s`). The core never guesses units.
