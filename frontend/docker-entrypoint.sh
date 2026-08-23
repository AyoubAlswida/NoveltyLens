#!/bin/sh
# Write the runtime backend address into the web root before nginx starts.
#
# nginx's official entrypoint runs everything in /docker-entrypoint.d/ in name
# order, so this executes once per container start, ahead of the server itself.
#
# Why this exists: a Route's hostname is not known at image build time, and the
# frontend needs an ABSOLUTE backend URL because the API is a separate Service
# behind its own Route. Baking it in would mean rebuilding the image per
# environment; injecting it here means one image runs anywhere.
#
# API_BASE_URL is set by the Deployment. If it is missing, write nothing at all
# and let config.ts keep its own default rather than emitting `undefined` - a
# broken URL fails at request time with a confusing CORS error, whereas no file
# fails visibly and immediately against localhost.
set -eu

TARGET=/usr/share/nginx/html/env.js

if [ -z "${API_BASE_URL:-}" ]; then
    echo "[env.js] API_BASE_URL not set - leaving frontend on its built-in default"
    exit 0
fi

# Single-quoted heredoc would block the expansion this needs, so the value is
# interpolated deliberately. It comes from the Deployment, not from a request.
cat > "$TARGET" <<EOF
window.NOVELTY_LENS_API_URL = "${API_BASE_URL}";
EOF

echo "[env.js] frontend will call ${API_BASE_URL}"
