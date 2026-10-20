# Supported input formats

Declared after testing the pinned readers (`tests/stage_01`). Anything not listed is rejected with
an explicit error rather than assumed to work.

## MiniSEED

| Item | Status |
| --- | --- |
| MiniSEED 2.x records (any record length ObsPy 1.5.1 reads) | supported |
| Encodings INT16, INT32, FLOAT32, FLOAT64, STEIM1, STEIM2 | supported (round-trip tested) |
| Other MiniSEED 2 encodings (ASCII, GEOSCOPE, SRO, CDSN, DWWSSN, ...) | rejected (`unsupported_encoding`) |
| MiniSEED 3 | rejected (`unsupported_miniseed_version`): ObsPy 1.5.1 has no MiniSEED 3 reader |
| Separate files per component | supported (up to 6 files per submission by default) |
| Several stations/locations or 1/2/Z horizontals | supported only with an explicit N/E/Z mapping |
| Gaps | detected; data split into segments; never interpolated |
| Overlapping/duplicated records | rejected |
| Different sampling rates across components | rejected (never resampled) |
| Sub-sample channel offsets | allowed up to 0.1 sample (recorded), otherwise rejected |

Exact uploaded MiniSEED bytes are always preserved.

## CSV / CSV.GZ

UTF-8 text, one header row, optional `#` comment lines, delimiter `,`, `;`, or tab (auto-detected
from the header; ambiguous headers must declare it). Decimal point only (decimal commas rejected).

| Layout | Required columns | Required declarations |
| --- | --- | --- |
| `elapsed_time` | time in seconds since first sample + N, E, Z | start UTC optional (else start unknown) |
| `utc_timestamps` | ISO 8601 timestamps with `Z` or `+HH:MM` + N, E, Z | timestamps without offset need an explicit "these are UTC" declaration |
| `samples_with_rate` | N, E, Z | sampling rate (required), start UTC optional |

Recognized component headers (case-insensitive): `N/North/NS/HHN/...`, `E/East/EW/HHE/...`,
`Z/Vertical/Up/UD/V/HHZ/...`. Headers `1/2/H1/H2/HH1/HH2` require an explicit mapping. Units written
explicitly in headers (`N [m/s]`) are recorded; units are otherwise taken only from declared metadata.

Timing policy `strict` (default) accepts timestamp deviations up to `max(1 microsecond, 0.001 x dt)`
from a regular grid; larger deviations block analysis (no resampling). `nominal_rate` accepts a
declared tolerance (fraction of dt, at most 0.5); the original timestamp text is then retained in the
canonical sidecar.

## StationXML

Optional, separate upload. Parsed with ObsPy after rejecting DOCTYPE/ENTITY declarations. The overall
instrument sensitivity and input units of each matched channel epoch are attached; the file's
SHA-256 is stored. Full response deconvolution is NOT implemented; only scalar sensitivity
correction (valid within the passband) is available.

## Photographs

JPEG, PNG, WebP up to 15 MiB and 40 megapixels. Stored only as a 300 x 300 JPEG with orientation
applied, aspect ratio preserved by white padding, and all EXIF (including GPS) removed.
