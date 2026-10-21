# Licensing, consent, records, and moderation

## Consent and license

- Uploads are published under CC BY 4.0 (`https://creativecommons.org/licenses/by/4.0/`).
- The consent and rights checkboxes are unchecked by default and both are required, together with
  a public attribution (person or organization). Missing consent is refused before anything is
  parsed, stored, or charged.
- Each measurement stores: license ID and URL, attribution, optional source citation, consent
  wording, version (`consent-v1`), its SHA-256, the consent timestamp, the rights confirmation,
  the modification history, the removal notice, and a note that third-party reference data,
  sensor documentation, and template banks keep their own licenses.
- The same record is an independent downloadable artifact ("License, consent, and attribution")
  and appears in the manifest's `licensing` block.

## Record codes and metadata revisions

- The record code is `{geohash part}-{decimal year}` from the coordinates and the recording start
  (or the upload time when the recording has no absolute time; the source is recorded).
- Collisions get `-02`, `-03`, ... from a per-base counter incremented inside the publishing
  transaction. An aborted transaction does not consume a suffix; codes are never reused or
  overwritten, including after removal or purge.
- Metadata corrections create a new revision with reason and attribution; earlier snapshots are
  kept; the record code does not change. Each analysis run references the metadata revision it was
  computed with, and its documents carry that snapshot.

## Moderation

- Anyone can report a measurement (`POST /api/v1/measurements/{id}/reports`). It becomes
  `under_review` and stays available while an administrator reviews it.
- Administrators (separate `hva_...` tokens) can remove or restore. Removal withholds metadata and
  every artifact (HTTP 410 with the notice), keeps the record code and licensing history, and is
  recorded as a moderation event.
- Removal notice: "Removing a measurement from this repository stops further distribution here. It
  cannot recall copies that were already downloaded, and it does not revoke CC BY 4.0 rights already
  granted to recipients who received the material under that license."
- Purge is a separate, explicit administrator action for removed measurements only. It requires the
  record code typed back, deletes stored objects that no other measurement references (shared
  content-addressed objects are kept), keeps the database rows for audit, and cannot be undone.
- Quota is never refunded by removal or purge.
