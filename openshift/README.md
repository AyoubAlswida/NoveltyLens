# Deploying NoveltyLens to OpenShift

Everything runs in one namespace: Postgres, Qdrant, the FastAPI backend, and
the nginx-served Angular frontend. Two Routes are exposed — the frontend for
people, the backend because the browser calls it directly.

**`oc start-build --from-dir` uploads a local directory to be built in the
cluster.** No git remote, no registry login, no image transfer from a laptop —
which is the whole reason this path exists here: the machine that can reach the
cluster cannot reach github.com, pypi.org, or registry.npmjs.org.

## Before starting, confirm three things

These are the failure modes that waste a day if discovered late:

1. **Can the build pull packages?** The images install from pypi.org and
   registry.npmjs.org during build. If cluster egress blocks them, get the
   internal mirror URLs and pass them as build args (both Dockerfiles accept
   `PIP_INDEX_URL` / `NPM_REGISTRY`). Ask before building, not after.
2. **Can pods reach `dev-metabrain-api.aramco.com.sa`?** The whole pipeline
   depends on it. Verify from inside the cluster, not from a laptop.
3. **Can pods reach `serpapi.com`?** This one is likely to be **no**, and it has
   no fallback — Google Patents is the only enabled provider, so a blocked
   SerpAPI means no real search regardless of everything else working. Settle
   the egress policy question before promising a demo date.

## Deploy

```bash
oc new-project noveltylens

# 1. Credentials. Do NOT edit 01-secrets.yaml - it is committed, so anything
#    written into it is public. Create the Secret directly instead.
oc create secret generic noveltylens-secrets \
    --from-literal=LLM_API_KEY='sk-...' \
    --from-literal=SERPAPI_KEY='...' \
    --from-literal=AUTH_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')" \
    --from-literal=POSTGRES_PASSWORD="$(python -c 'import secrets;print(secrets.token_hex(16))')"

# 2. The connection string, assembled from the password just generated. It is a
#    separate Secret because it embeds that password and the backend wants it as
#    one value.
PGPW=$(oc get secret noveltylens-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
oc create secret generic noveltylens-db-url \
    --from-literal=DATABASE_URL="postgresql://agenticai:${PGPW}@postgres:5432/agenticai"

# 3. Config, then the two stateful services.
oc apply -f 01-secrets.yaml        # applies the ConfigMap; Secret already exists
oc apply -f 02-postgres.yaml
oc apply -f 03-qdrant.yaml
oc rollout status deployment/postgres

# 4. Build both images IN the cluster, from this working copy.
oc new-build --name=backend --binary --strategy=docker
oc start-build backend  --from-dir=../backend  --follow
oc new-build --name=frontend --binary --strategy=docker
oc start-build frontend --from-dir=../frontend --follow

# 5. Backend, then point the frontend at its Route.
oc apply -f 04-backend.yaml
oc rollout status deployment/backend
oc apply -f 05-frontend.yaml
oc set env deployment/frontend \
    API_BASE_URL="https://$(oc get route backend -o jsonpath='{.spec.host}')"

oc get route frontend -o jsonpath='{.spec.host}{"\n"}'
```

Sign in with the account seeded by `migrations/0004.create-users.sql`:
`testnovelty@gmail.com` / `ABC123`. **That password is in a committed migration
and in this repository's docs** — replace the row before anyone outside the team
can reach the Route.

## Upgrading

Code change, no schema change:

```bash
oc start-build backend --from-dir=../backend --follow   # or frontend
```

The Deployment pulls `:latest` and rolls automatically.

Schema change — a completed Job is immutable, so it must be recreated:

```bash
oc delete job noveltylens-migrate
oc apply -f 04-backend.yaml
oc logs job/noveltylens-migrate
```

## Things that will bite

- **Arbitrary UID.** OpenShift's default SCC runs containers as a random,
  unnamed UID whose group is 0. Both Dockerfiles handle it (`chgrp -R 0` +
  `chmod -R g=u`, ports above 1024, nginx-unprivileged). If something
  crash-loops on a permission error, fix the image — do not reach for `anyuid`,
  which weakens the namespace.
- **`SERVER_HOST` must be `0.0.0.0`.** `config.py` defaults to `127.0.0.1` so a
  laptop does not expose an unauthenticated backend to its network. In a pod
  that default means the Service reports zero ready endpoints and nothing ever
  says why. The ConfigMap sets it.
- **The 300s Route timeout is load-bearing.** A run streams SSE for ~60s; the
  router's 30s default would sever it mid-run and the UI would show a failed
  search with no error text.
- **Postgres image.** Docker Hub's `postgres` does not run under the default
  SCC — its entrypoint chowns the data directory as root. `02-postgres.yaml`
  uses Red Hat's image, which is built for this. See the note in that file if
  `registry.redhat.io` is unreachable.
- **Qdrant is deployed but unused.** `semantic_search`, `index_patents` and
  `fuse` are all commented out in `graph/pipeline.py`. Drop `03-qdrant.yaml` if
  storage is tight; re-enabling that branch also needs
  `MIN_SIMILARITY_SCORE` re-measured against Qwen3-Embedding-4B first.
- **CORS is `allow_origins=["*"]`** in `server.py`. Fine while this is internal;
  narrow it to the frontend Route before it is exposed more widely.
