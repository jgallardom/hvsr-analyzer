"""Job worker: `python -m hvsr_web.worker` (or embedded in the web process when HVSR_EMBEDDED_WORKER=true).

All state lives in the jobs table, so any number of workers can run; the concurrency ceiling,
leases, and fencing in `jobs` keep them from duplicating or interleaving work. With
HVSR_JOB_ISOLATION=process (the default) each job runs in a fresh child process under CPU, memory,
and wall-time limits (see `isolation`).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading

from . import analysis_jobs, isolation, jobs, ondemand
from .context import AppContext
from .settings import Settings

HANDLERS = {**analysis_jobs.HANDLERS, **ondemand.HANDLERS}
log = logging.getLogger("hvsr_web.worker")


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{threading.get_ident()}"


def drain(
    ctx: AppContext, max_jobs: int | None = None, wid: str | None = None, isolated: bool = False
) -> list[dict]:
    """Run queued jobs until none is claimable (or `max_jobs` ran); inline unless `isolated`."""
    wid = wid or worker_id()
    out: list[dict] = []
    while max_jobs is None or len(out) < max_jobs:
        res = isolation.run_isolated_once(ctx, wid) if isolated else jobs.run_once(ctx, wid, HANDLERS)
        if res is None:
            break
        log.info("job %s: %s", res["job_id"], res["state"])
        out.append(res)
        if res["state"] == "startup_failed":
            break
    return out


def run_forever(ctx: AppContext, stop: threading.Event) -> None:
    """Run jobs one at a time until `stop` is set; a stop request lets the current job finish."""
    wid = worker_id()
    isolated = ctx.settings.job_isolation == "process"
    while not stop.is_set():
        try:
            if not drain(ctx, max_jobs=1, wid=wid, isolated=isolated):
                stop.wait(ctx.settings.worker_poll_s)
        except Exception:  # a database outage must not kill the loop; jobs stay durable
            log.exception("worker loop error")
            stop.wait(ctx.settings.worker_poll_s)


def start_embedded(ctx: AppContext) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=run_forever, args=(ctx, stop), name="hvsr-worker", daemon=True)
    thread.start()
    return thread, stop


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    isolation.pin_threads(settings.numeric_threads)
    ctx = AppContext.from_settings(settings)
    stop = threading.Event()
    # platforms stop containers with SIGTERM: finish the current job, then exit
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        run_forever(ctx, stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
