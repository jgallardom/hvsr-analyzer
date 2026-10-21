"""The cost document is a rendering of measured sizes, recorded official prices, and the deployed
blueprint, for 100, 1,000, and 10,000 measurements per month."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/stage9"
OFFICIAL = ("render.com", "developers.cloudflare.com", "www.backblaze.com", "www.hetzner.com")


def _model():
    spec = importlib.util.spec_from_file_location("cost_model", ROOT / "scripts/cost_model.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.acceptance("AC-S9-07")
def test_cost_document_renders_measured_sizes_and_recorded_prices():
    mod = _model()
    perf = json.loads((EVIDENCE / "performance.json").read_text())
    prices = json.loads((EVIDENCE / "prices.json").read_text())
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    assert (ROOT / "docs/costs.md").read_text() == mod.render(perf, prices, blueprint), (
        "run scripts/cost_model.py"
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", prices["retrieved_utc"])
    for url in prices["sources"].values():
        assert url.startswith("https://") and url.split("/")[2] in OFFICIAL, url
    [web] = [s for s in blueprint["services"] if s["type"] == "web"]
    assert web["plan"] in prices["render"]["service_month"]
    assert blueprint["databases"][0]["plan"] in prices["render"]["postgres_month"]
    assert mod.WORKLOAD["monthly_new_measurements"] == [100, 1_000, 10_000]
    assert set(mod.WORKLOAD["size_profiles"]) <= {c["case"] for c in perf["cases"]}


@pytest.mark.acceptance("AC-S9-07")
def test_cost_model_charges_what_grows_and_respects_free_allowances():
    mod = _model()
    perf = json.loads((EVIDENCE / "performance.json").read_text())
    prices = json.loads((EVIDENCE / "prices.json").read_text())
    fp = mod.footprint(next(c for c in perf["cases"] if c["case"] == "large"))
    plan = {"web_plan": "1c-2g", "postgres_plan": "0.1c-256mb"}
    totals = [mod.month(prices, plan, fp, m, 12)["total"] for m in (100, 1_000, 10_000)]
    assert totals == sorted(totals) and totals[0] < totals[-1]
    assert mod.month(prices, plan, fp, 100, 12)["total"] > mod.month(prices, plan, fp, 100, 1)["total"]
    tiny = mod.month(prices, plan, fp, 1, 1)
    # within the free allowances only the fixed instances cost money
    assert tiny["r2_storage"] == tiny["r2_requests"] == tiny["render_bandwidth"] == 0
    assert (
        tiny["total"]
        == prices["render"]["service_month"]["1c-2g"] + prices["render"]["postgres_month"]["0.1c-256mb"]
    )
    # every stored byte crosses Render's outbound meter once on its way to the bucket
    big = mod.month(prices, plan, fp, 10_000, 1)
    assert big["outbound_bytes"] >= 10_000 * fp["stored_bytes"]
    assert (
        mod.postgres_disk_gb(0.5e9) == 1
        and mod.postgres_disk_gb(0.95e9) == 5
        and mod.postgres_disk_gb(14e9) == 20
    )
