"""Generate the shipped template banks into packages/hvsr_core/src/hvsr_core/banks/.

Deterministic: same code -> byte-identical NPZ files and manifests. Run:
    uv run python scripts/build_banks.py [--check]
--check regenerates in memory and fails if any shipped file differs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from hvsr_core.mdof import generate_mdof_bank
from hvsr_core.templates import (
    RAW,
    SHIPPED_DIR,
    SHIPPED_KO_BANDWIDTHS,
    generate_sdof_bank,
    ko_conditioning,
    write_bank,
)

MDOF2 = dict(n_dof=2, mass_ratio_grid=(0.25, 0.5, 1.0, 2.0), stiffness_ratio_grid=(0.25, 0.5, 1.0, 2.0), damping_grid=(0.02, 0.05, 0.1, 0.2))
MDOF3 = dict(n_dof=3, mass_ratio_grid=(0.5, 1.0, 2.0), stiffness_ratio_grid=(0.5, 1.0, 2.0), damping_grid=(0.02, 0.05, 0.1, 0.2))


def all_banks():
    yield lambda: generate_sdof_bank(RAW)
    for b in SHIPPED_KO_BANDWIDTHS:
        yield lambda b=b: generate_sdof_bank(ko_conditioning(b))
    for spec, name in ((MDOF2, "mdof2"), (MDOF3, "mdof3")):
        yield lambda spec=spec, name=name: generate_mdof_bank(conditioning=RAW, bank_id=f"{name}-raw-v1", **spec)
        yield lambda spec=spec, name=name: generate_mdof_bank(conditioning=ko_conditioning(40.0), bank_id=f"{name}-ko40-v1", **spec)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    bad = 0
    for make in all_banks():
        t0 = time.time()
        manifest, data = make()
        js = SHIPPED_DIR / f"{manifest['bank_id']}.json"
        npz = SHIPPED_DIR / manifest["data_file"]
        text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if args.check:
            same = js.is_file() and npz.is_file() and js.read_text() == text and npz.read_bytes() == data
            print(f"{manifest['bank_id']:<16} {'OK' if same else 'DIFFERS'}")
            bad += not same
        else:
            write_bank(SHIPPED_DIR, manifest, data)
            print(f"{manifest['bank_id']:<16} templates={manifest['n_templates']:<3} bytes={len(data):<7} {time.time() - t0:.1f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
