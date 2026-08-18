#!/bin/sh
# Regenerated on every container start from $API_URL, so the same built image
# can point at any backend without a rebuild. This is the runtime half of the
# window.NOVELTY_LENS_API_URL hook src/app/config.ts already reads - loaded
# via index.html's <script src="/env-config.js"> before the Angular bundle.
set -e

API_URL="${API_URL:-http://localhost:8001}"

cat > /usr/share/nginx/html/env-config.js <<EOF
window.NOVELTY_LENS_API_URL = "${API_URL}";
EOF
