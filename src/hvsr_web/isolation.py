"""Per-job process isolation: numerical thread pinning and CPU, memory, and wall-time limits.

The production worker runs each job in a fresh spawned child process. The child pins numerical
thread pools, claims one job, reports the claim to the parent, sets RLIMIT_CPU and RLIMIT_AS
relative to its post-import baseline, and runs the job. The parent renews the lease while the child
runs, kills the child at the wall-time limit, and records the outcome on the job:

- CPU, memory, or wall-time exhaustion: permanent failure (the same input would exceed it again);
- any other abnormal exit: retryable failure, up to `job_max_attempts`;
- the parent itself dying: the lease expires and another worker re-claims the job.

Publication is one fenced transaction inside the child, so a killed job never leaves a partially
published run.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import multiprocessing
import os
import signal
import time

from . import jobs
from .context import AppContext
from .settings import Settings

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)
DEFAULT_HANDLERS = "hvsr_web.worker:HANDLERS"
STARTUP_TIMEOUT_S = 120.0
CPU_GRACE_S = 5


def pin_threads(n: int) -> None:
    """Limit every numerical thread pool in this process (and children it starts) to `n` threads."""
    for var in THREAD_ENV_VARS:
        os.environ[var] = str(n)
    from threadpoolctl import threadpool_limits

    threadpool_limits(limits=n)


def _process_cpu_s() -> float:
    t = os.times()
    return t.user + t.system


def _address_space_bytes() -> int | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None


def apply_rlimits(cpu_s: int, memory_bytes: int) -> dict:
    """Set CPU-time and address-space limits for the rest of this process's life."""
    import resource

    soft_cpu = int(_process_cpu_s()) + cpu_s
    resource.setrlimit(resource.RLIMIT_CPU, (soft_cpu, soft_cpu + CPU_GRACE_S))
    applied = {"cpu_soft_s": soft_cpu}
    baseline = _address_space_bytes()
    if baseline is not None:
        resource.setrlimit(resource.RLIMIT_AS, (baseline + memory_bytes, baseline + memory_bytes))
        applied["address_space_bytes"] = baseline + memory_bytes
    return applied


def _resolve(ref: str):
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


def _child(settings: Settings, handlers_ref: str, wid: str, conn) -> None:
    pin_threads(settings.numeric_threads)
    ctx = AppContext.from_settings(settings, run_migrations=False)
    handlers = _resolve(handlers_ref)
    claim = jobs.claim(ctx, wid)
    conn.send(None if claim is None else dataclasses.asdict(claim))
    if claim is None:
        return
    applied = apply_rlimits(settings.job_cpu_time_s, settings.job_memory_bytes)
    res = jobs.run_claimed(ctx, claim, handlers)
    import resource

    ru = resource.getrusage(resource.RUSAGE_SELF)
    res["process"] = {
        "peak_rss_bytes": ru.ru_maxrss * 1024,
        "cpu_s": round(ru.ru_utime + ru.ru_stime, 3),
        "limits": applied,
    }
    conn.send(res)


@contextlib.contextmanager
def _thread_env(n: int):
    saved = {v: os.environ.get(v) for v in THREAD_ENV_VARS}
    os.environ.update({v: str(n) for v in THREAD_ENV_VARS})
    try:
        yield
    finally:
        for v, old in saved.items():
            if old is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = old


def _kill(proc) -> None:
    if proc.is_alive():
        proc.kill()
    proc.join(timeout=30)


def run_isolated_once(ctx: AppContext, wid: str, handlers_ref: str = DEFAULT_HANDLERS) -> dict | None:
    """Claim and run one job in a child process; None when no job was claimable."""
    s = ctx.settings
    mp = multiprocessing.get_context("spawn")
    recv, send = mp.Pipe(duplex=False)
    proc = mp.Process(target=_child, args=(s, handlers_ref, wid, send), name="hvsr-job")
    with _thread_env(s.numeric_threads):
        proc.start()
    send.close()
    try:
        if not recv.poll(STARTUP_TIMEOUT_S):
            _kill(proc)
            return {"job_id": None, "state": "startup_failed"}
        msg = recv.recv()
    except EOFError:
        _kill(proc)
        return {"job_id": None, "state": "startup_failed", "exitcode": proc.exitcode}
    if msg is None:
        proc.join(timeout=30)
        return None
    claim = jobs.Claim(**msg)
    deadline = time.monotonic() + s.job_wall_time_s
    beat = max(1.0, s.job_lease_s / 3)
    result = None
    child_ended = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if recv.poll(min(beat, remaining)):
            with contextlib.suppress(EOFError):
                result = recv.recv()
            child_ended = True
            break
        if not jobs.heartbeat(ctx, claim):
            _kill(proc)
            return {"job_id": claim.job_id, "state": "lease_lost"}
    if not child_ended:
        _kill(proc)
        error = (
            f"wall-time limit exceeded: the job ran longer than {s.job_wall_time_s} s (HVSR_JOB_WALL_TIME_S)"
        )
        return {
            "job_id": claim.job_id,
            "state": jobs.fail(ctx, claim, error, retryable=False),
            "limit": "wall_time",
        }
    proc.join(timeout=60)
    if proc.is_alive():
        _kill(proc)
    if result is not None:
        return result
    code = proc.exitcode
    if code == -signal.SIGXCPU:
        error = f"CPU-time limit exceeded: more than {s.job_cpu_time_s} s of CPU (HVSR_JOB_CPU_TIME_S)"
        return {
            "job_id": claim.job_id,
            "state": jobs.fail(ctx, claim, error, retryable=False),
            "limit": "cpu_time",
        }
    error = f"job process ended abnormally (exit code {code}); retried up to HVSR_JOB_MAX_ATTEMPTS times"
    return {"job_id": claim.job_id, "state": jobs.fail(ctx, claim, error, retryable=True), "exitcode": code}
