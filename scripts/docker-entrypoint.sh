#!/bin/sh
# Container entry point. Records the deployed commit in analysis provenance (the GIT_COMMIT build
# argument, else Render's RENDER_GIT_COMMIT), then runs the given command; without one, the web
# server. One uvicorn process only: upload slots and the embedded worker are per process, so
# WEB_CONCURRENCY (set by some platforms) must not multiply them.
set -eu
export HVSR_GIT_COMMIT="${HVSR_GIT_COMMIT:-${RENDER_GIT_COMMIT:-}}"
if [ "$#" -eq 0 ]; then
    set -- uvicorn hvsr_web.api:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
fi
exec "$@"
