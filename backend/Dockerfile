# NoveltyLens backend. Built to run under OpenShift's `restricted-v2` SCC,
# which is the default and which breaks most first attempts in the same two
# ways - both handled explicitly below rather than by granting `anyuid`:
#
#   1. The container runs as an ARBITRARY UID, not the one named by USER. That
#      UID is not in /etc/passwd and its primary group is always GID 0. So
#      anything the process must read or write is given to group 0, and USER is
#      set to a non-root placeholder purely so the image is also correct outside
#      OpenShift. Do not "fix" a permission error here by adding `USER root`.
#   2. It cannot bind a privileged port. The app listens on 8001 (see
#      config.server_port) and must bind 0.0.0.0, not the 127.0.0.1 default -
#      the Deployment sets SERVER_HOST. A pod left on loopback is reachable only
#      from itself, which shows up as a Service with no ready endpoints.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so edits to application code do not invalidate this layer.
# If pypi.org is blocked on the build network, point pip at the internal mirror:
#   --build-arg PIP_INDEX_URL=https://<artifactory>/api/pypi/pypi/simple
ARG PIP_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The arbitrary-UID fix: group 0 owns everything and has the same rights the
# owner does. `g=u` rather than `g+rw` so directories keep their execute bit.
RUN chgrp -R 0 /app && chmod -R g=u /app

USER 1001
EXPOSE 8001

# Exec form: uvicorn becomes PID 1 and receives SIGTERM directly, so a rolling
# update drains cleanly instead of waiting out the grace period. Workers stay at
# 1 deliberately - a run holds an open SSE stream, and the LLM and embedding
# calls are I/O-bound, so scale with replicas, not with in-pod workers.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
