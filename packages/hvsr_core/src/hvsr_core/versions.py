"""Package, algorithm, and schema version identifiers.

Every algorithm whose output can change a scientific number has its own version string. Changing
behaviour of any of these components requires bumping the corresponding identifier and recording the
change in docs/scientific-decisions.md.
"""

from __future__ import annotations

PACKAGE_VERSION = "0.1.0"

ALGORITHM_VERSIONS: dict[str, str] = {
    "csv_parser": "csv-parse-v1",
    "preprocessing": "prep-v1",
    "windowing": "win-halfopen-v1",
    "spectra": "fas-ko-sparse-v1",
    "basic_qc": "basic-qc-v1",
    "standard_selection": "std-earliest-nonoverlap-v1",
    "frequency_outlier_rejection": "fdwra-cox2020-impl-v1",
    "peak_discovery": "peak-families-kde-v1",
    "source_diagnostics": "narrow-line-v1",
    "sdof_matching": "sdof-r2log-q129-v1",
    "sdof_selection": "sdof-greedy-nonoverlap-v1",
    "uncertainty": "temporal-block-bootstrap-v1",
    "peak_bootstrap": "peak-replicate-v1",
    "sesame": "sesame-d23.12-impl-v1",
    "robustness": "robust-lw1.5-v1",
    "quality_dimensions": "quality-dims-v1",
    "plotting": "mpl-static-v1",
    "canonical_conversion": "canon-mseed2-v1",
}

SCHEMA_VERSIONS: dict[str, str] = {
    "config": "2.0.0",
    "analysis": "2.0.0",
    "manifest": "2.0.0",
    "template_bank": "1.0.0",
    "sesame_report": "1.0.0",
    "robustness": "1.0.0",
}
