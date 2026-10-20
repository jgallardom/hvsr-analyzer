#!/usr/bin/env python3
"""Regenerate files derived from the code: configuration reference and config JSON Schema."""

from __future__ import annotations

from pathlib import Path

from hvsr_core.docgen import config_schema_text, configuration_markdown

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    (ROOT / "docs" / "configuration.md").write_text(configuration_markdown())
    (ROOT / "packages/hvsr_core/src/hvsr_core/schemas/config.schema.json").write_text(config_schema_text())
    print("wrote docs/configuration.md and config.schema.json")


if __name__ == "__main__":
    main()
