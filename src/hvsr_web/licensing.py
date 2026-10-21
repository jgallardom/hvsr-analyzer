"""CC BY 4.0 data licensing, consent wording, and attribution (spec 3.1).

The consent text is versioned; its exact wording and SHA-256 are stored with every submission.
The software itself is BSD-3-Clause; CC BY 4.0 applies to submitted data and derived data/figures.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hvsr_core.jsonutil import sha256_bytes

LICENSE_ID = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CONSENT_VERSION = "consent-v1"
CONSENT_TEXT = (
    "I consent that the waveforms, coordinates, processed photographs, metadata, numerical results, and "
    "plots of this submission will be stored and made publicly downloadable under the Creative Commons "
    "Attribution 4.0 International license (CC BY 4.0). I confirm that I am authorized to share all "
    "submitted material under these terms."
)
CONSENT_TEXT_SHA256 = sha256_bytes(CONSENT_TEXT.encode("utf-8"))
REMOVAL_NOTICE = (
    "Removing a measurement from this repository stops further distribution here. It cannot recall copies "
    "that were already downloaded, and it does not revoke CC BY 4.0 rights already granted to recipients "
    "who received the material under that license."
)
EXCLUDED_FROM_RELICENSING = (
    "Third-party reference datasets, sensor documentation, and template banks keep their own licenses and "
    "are not relicensed under CC BY 4.0 by this repository."
)


class ConsentForm(BaseModel):
    """Upload-form consent block. Both checkboxes default to unchecked and are required."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    consent_checked: bool = False
    rights_confirmed: bool = False
    attribution: str = Field(
        "", max_length=200, description="Public attribution: person or organization name."
    )
    source_citation: str | None = Field(None, max_length=1000)
    consent_version: str = CONSENT_VERSION

    @field_validator("attribution")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def problems(self) -> list[str]:
        out = []
        if not self.consent_checked:
            out.append("explicit consent to public CC BY 4.0 distribution is required")
        if not self.rights_confirmed:
            out.append("confirmation that you are authorized to share the material is required")
        if not self.attribution:
            out.append("a public attribution name or organization is required")
        if self.consent_version != CONSENT_VERSION:
            out.append(f"consent version {self.consent_version!r} is not the current {CONSENT_VERSION!r}")
        return out


class ConsentRequired(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def licensing_record(form: ConsentForm, consent_utc: str) -> dict:
    problems = form.problems()
    if problems:
        raise ConsentRequired(problems)
    return {
        "license_id": LICENSE_ID,
        "license_url": LICENSE_URL,
        "attribution": form.attribution,
        "source_citation": form.source_citation,
        "consent_version": CONSENT_VERSION,
        "consent_text": CONSENT_TEXT,
        "consent_text_sha256": CONSENT_TEXT_SHA256,
        "consent_utc": consent_utc,
        "rights_confirmed": True,
        "modification_history": [
            {
                "utc": consent_utc,
                "action": "submitted",
                "metadata_revision": 1,
                "attribution": form.attribution,
            }
        ],
        "removal_notice": REMOVAL_NOTICE,
        "third_party_note": EXCLUDED_FROM_RELICENSING,
    }
