# Backend container image. Environment-driven: every external dependency
# (HF_TOKEN, SERPAPI_KEY, DATABASE_URL, QDRANT_URL, AUTH_SECRET, ...) comes
# from the environment at runtime, per backend/config.py - nothing is baked
# in at build time. See the repository-root docker-compose.yml for how this
# image is wired to Postgres, Qdrant, and backend/.env.

FROM python:3.13-slim

WORKDIR /app

# Dependencies first so this layer stays cached across source-only edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# uvicorn is invoked directly rather than `python server.py` - that file's own
# __main__ block binds to 127.0.0.1, which is unreachable from outside the
# container. 0.0.0.0 is the only change from local dev.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
