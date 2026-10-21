"""Job handlers imported by spawned job processes in the Stage 9 isolation tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_info

from hvsr_web.isolation import THREAD_ENV_VARS


def sleep(_ctx, claim):
    time.sleep(claim.payload["s"])
    return {"slept": claim.payload["s"]}


def spin(_ctx, _claim):
    x = 0
    while True:
        x += 1


def alloc(_ctx, claim):
    a = np.ones(claim.payload["bytes"] // 8)
    return {"n": int(a.size)}


def crash(_ctx, _claim):
    os._exit(3)


def crash_once(_ctx, claim):
    marker = Path(claim.payload["marker"])
    if not marker.exists():
        marker.write_text("crashed once")
        os._exit(9)
    return {"recovered": True}


def threads(_ctx, _claim):
    np.ones((64, 64)) @ np.ones((64, 64))
    return {
        "pools": [{"api": p["internal_api"], "num_threads": p["num_threads"]} for p in threadpool_info()],
        "env": {v: os.environ.get(v) for v in THREAD_ENV_VARS},
        "cpu_count": os.cpu_count(),
    }


HANDLERS = {
    "sleep": sleep,
    "spin": spin,
    "alloc": alloc,
    "crash": crash,
    "crash_once": crash_once,
    "threads": threads,
}
REF = "stage9_handlers:HANDLERS"
