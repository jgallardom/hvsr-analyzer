# Citing measurements, results, and the software

## Measurements

Every measurement is published under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)
unless its page says otherwise. Reuse requires:
- the attribution chosen by the contributor;
- a link to the license;
- a statement of any changes you made.

Each measurement page (`#license`) and the manifest's `licensing.suggested_citation` show a
suggested citation:

    {attribution} ({year}). HVSR measurement {record code}. HVSR Analyzer, {measurement page URL}. CC BY 4.0.

The year is the year the measurement was published on the site, not the recording date. The URL is
absolute when the deployment sets `HVSR_PUBLIC_BASE_URL`; otherwise it is the site-relative path.
If the contributor gave a source citation (for example a paper or report describing the survey),
it is shown next to the attribution and should be cited as well.

The record code identifies the measurement for good: codes are never reused or overwritten,
including after removal. Removal stops further distribution on the site. It does not revoke CC BY
rights already granted, and it cannot recall copies already downloaded.

## Derived results

When you use a number, curve, or figure, cite the measurement as above and identify exactly what
was computed:
- the run ID and method (`standard`, `standard_basic_audit`, or `sdof`);
- the selection ID and peak family, when relevant;
- the analysis JSON's SHA-256 (in the manifest) or its stable URL;
- `provenance.software_version` and `provenance.git_commit`;
- `config_hash`, and the template bank ID and version for the SDOF method.

The analysis JSON carries these fields, plus its `licensing` block (license, attribution, source
citation, record code, manifest URL). A replay with the same inputs and versions reproduces the
numbers within the recorded tolerance policy.

State the interpretation limits the outputs carry:
- The band is the "Estimated 95% pointwise CI for the selected-window mean". It is conditional on
  the window selection, and its coverage is not calibrated: the locked validation did not meet its
  targets (`calibration` in the analysis JSON, `docs/validation/report-v1.md`). Do not report it as
  a 95% interval for the site's resonance frequency.
- SDOF-selected results come from an original project design: peak-aligned SDOF template matching
  with R2_log ranking and greedy non-overlapping selection. That method is experimental. Neither
  SESAME nor hvsrpy endorses it.
- SESAME criteria in a report are diagnostics computed on the published curve. They never
  influence window selection, the mean, or the band.
- Annotations are human interpretations, attributed to their authors. They are not algorithmic
  results.

## The software

HVSR Analyzer is distributed under the BSD-3-Clause license (`LICENSE`). There is no DOI. Cite the
version and commit recorded in the analysis JSON, for example:

    HVSR Analyzer, version {software_version}, commit {git_commit}. BSD-3-Clause.

Template banks, third-party reference data, and sensor documentation keep their own licenses. A
bank's license is in its manifest (`templates` category).

## Methods the software implements

Cite the original sources for established methods. The project documentation identifies:
- SESAME (2004). Guidelines for the implementation of the H/V spectral ratio technique on ambient
  vibrations. European research project WP12, deliverable D23.12. Linked from the reports as
  http://sesame.geopsy.org/Papers/HV_User_Guidelines.pdf; the specification lists the https form
  of the same address.
- hvsrpy (https://hvsrpy.readthedocs.io/en/stable/), used as the reference implementation for
  the Standard method's agreement tests. It is not a runtime dependency.
- Konno-Ohmachi smoothing and the frequency-domain window rejection algorithm, as described in
  `docs/scientific-decisions.md`. That document separates established methods from project
  decisions.

The test suite does not fetch these links. Check them before publication.
