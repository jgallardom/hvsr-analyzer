"""Deployment settings from environment variables (prefix HVSR_).

Secrets and operational limits are configuration, never code constants. Byte ceilings and quotas
are initial operational defaults to benchmark, not scientific standards.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from hvsr_core.io.limits import InputLimits

MiB = 1024 * 1024
GiB = 1024 * MiB


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database_url: str = "sqlite:///./var/hvsr.sqlite3"
    object_store: Literal["local", "s3"] = "local"
    local_store_dir: str = "./var/objects"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    url_signing_key: SecretStr | None = Field(
        None, description="HMAC key for local-store signed URLs; a random per-process key is used if unset."
    )
    signed_url_ttl_s: int = Field(300, ge=10, le=86400)
    moderation_contact: str | None = None

    # contributor budget (operational default, not a scientific standard)
    monthly_quota_bytes: int = Field(1 * GiB, ge=0)
    guest_uploads_enabled: bool = False
    guest_pool_monthly_bytes: int = Field(256 * MiB, ge=0)

    # decoding / input ceilings
    max_request_bytes: int = Field(128 * MiB, gt=0)
    max_decoded_bytes: int = Field(768 * MiB, gt=0)
    max_decompression_ratio: float = Field(200.0, gt=1)
    max_samples_per_channel: int = Field(8_640_000, gt=0)
    max_channels: int = Field(12, ge=3)
    max_files: int = Field(6, ge=1)
    max_duration_s: float = Field(172_800.0, gt=0)

    # compute ceilings
    max_candidate_windows: int = Field(20_000, gt=0)
    max_bootstrap_replicates: int = Field(2000, gt=0)
    max_template_comparisons: int = Field(200_000_000, gt=0)
    max_parameter_runs: int = Field(
        2, ge=1, description="Analysis runs per request (A plus robustness run B)."
    )
    max_concurrent_uploads: int = Field(
        1, ge=1, description="Uploads read and parsed at once per web process (previews, submissions, banks)."
    )
    upload_wait_s: float = Field(
        30.0, ge=0, description="How long an upload waits for a free slot before a 503 busy response."
    )
    max_concurrent_jobs: int = Field(1, ge=1)
    job_lease_s: int = Field(900, ge=10)
    job_max_attempts: int = Field(3, ge=1)
    job_wall_time_s: int = Field(900, ge=1)
    job_cpu_time_s: int = Field(900, ge=1)
    job_memory_bytes: int = Field(
        2 * GiB, ge=64 * MiB, description="Address space a job may add to its process baseline (Linux)."
    )
    job_isolation: Literal["process", "inline"] = Field(
        "process", description="Run each job in a fresh child process with CPU, memory, and wall-time limits."
    )
    numeric_threads: int = Field(1, ge=1, description="Threads per numerical library (BLAS, OpenMP).")
    staging_ttl_s: int = Field(86_400, ge=60)
    max_window_spectra_cells: int = Field(
        2_000_000, gt=0, description="Candidate windows x valid frequencies for an on-request per-window CSV."
    )
    max_plot_pixels: int = Field(
        16_000_000, gt=0, description="Largest PNG figure (width x height pixels), published or re-rendered."
    )
    max_plot_variants_per_run: int = Field(50, ge=0, description="Stored cosmetic plot variants per run.")

    # administrator-uploaded template banks
    bank_dir: str = Field("./var/banks", description="Local directory where uploaded banks are materialized.")
    max_bank_bytes: int = Field(64 * MiB, gt=0, description="Largest uploaded bank data file.")
    bank_licenses: str = Field(
        "CC0-1.0,CC-BY-4.0,BSD-3-Clause,MIT,Apache-2.0",
        description="Comma-separated SPDX license IDs accepted for uploaded banks.",
    )

    # serving
    public_base_url: str | None = Field(None, description="Absolute site URL used in citations.")
    embedded_worker: bool = Field(False, description="Run a job worker inside the web process.")
    worker_poll_s: float = Field(2.0, gt=0)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """Read `HVSR_<NAME>` variables. An empty variable counts as unset (a blank secret or URL in a
        copied .env file means "not configured", never an empty key or URL)."""
        env = os.environ if environ is None else environ
        values: dict = {}
        for name in cls.model_fields:
            value = env.get("HVSR_" + name.upper(), "")
            if value != "":
                values[name] = value
        return cls.model_validate(values)

    def input_limits(self) -> InputLimits:
        return InputLimits(
            max_request_bytes=self.max_request_bytes,
            max_decoded_bytes=self.max_decoded_bytes,
            max_decompression_ratio=self.max_decompression_ratio,
            max_samples_per_channel=self.max_samples_per_channel,
            max_channels=self.max_channels,
            max_files=self.max_files,
            max_duration_s=self.max_duration_s,
        )

    def public_limits(self) -> dict:
        """Limits shown to users and in cost reports (no secrets)."""
        return self.model_dump(
            exclude={
                "database_url",
                "url_signing_key",
                "s3_bucket",
                "s3_endpoint_url",
                "local_store_dir",
                "bank_dir",
            }
        )
