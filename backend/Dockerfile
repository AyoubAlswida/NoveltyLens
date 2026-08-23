# Backend container image. Environment-driven: every external dependency
# (LLM_API_KEY, SERPAPI_KEY, LENS_API_KEY, DATABASE_URL, QDRANT_URL,
# AUTH_SECRET, ...) comes from the environment at runtime, per backend/config.py
# - nothing is baked in at build time. See the repository-root
# docker-compose.yml for local wiring, and openshift/ for the cluster.
#
# This image targets OpenShift's default `restricted-v2` SCC as well as plain
# docker/compose. That constraint costs three lines and is handled below rather
# than by asking for the `anyuid` SCC, which would weaken the whole namespace:
#
#   1. OpenShift runs the container as an ARBITRARY UID, not the one named by
#      USER. That UID is absent from /etc/passwd and its primary group is always
#      GID 0, so anything the process reads or writes is given to group 0. Do
#      not "fix" a permission error here by adding `USER root`.
#   2. It cannot bind a privileged port. 8001 is already above 1024.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first so this layer stays cached across source-only edits.
# If pypi.org is unreachable from the build network - which it is from inside
# Aramco - point pip at the internal mirror:
#   --build-arg PIP_INDEX_URL=https://<artifactory>/api/pypi/pypi/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY . .

# The arbitrary-UID fix: group 0 owns everything and has the rights the owner
# does. `g=u` rather than `g+rw` so directories keep their execute bit.
RUN chgrp -R 0 /app && chmod -R g=u /app

USER 1001
EXPOSE 8001

# uvicorn is invoked directly rather than `python server.py`: config.py defaults
# SERVER_HOST to 127.0.0.1 so a laptop does not expose an unauthenticated
# backend to its network, and inside a container that default is reachable only
# from the container itself. Passing --host here makes the image correct even if
# SERVER_HOST is never set.
#
# Exec form so uvicorn is PID 1 and gets SIGTERM directly - a rolling update
# then drains instead of waiting out the grace period. Workers stay at 1
# deliberately: a run holds an open SSE stream and the LLM/embedding calls are
# I/O-bound, so scale with replicas, not with in-pod workers.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
