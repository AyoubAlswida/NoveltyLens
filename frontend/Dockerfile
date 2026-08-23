# Multi-stage build: compile the Angular app, then serve the static output with
# nginx. The backend URL is NOT baked in at build time - docker-entrypoint.sh
# writes it into env-config.js from $API_URL when the container starts, using
# the same window.NOVELTY_LENS_API_URL hook src/app/config.ts already reads.
# That means one built image can point at any backend without a rebuild.
#
# The runtime base is nginx-UNPRIVILEGED, not stock nginx, so the same image
# runs under OpenShift's default `restricted-v2` SCC: stock nginx binds port 80
# and writes its pid and caches into root-owned directories, and under that SCC
# the container is an arbitrary non-root UID that can do neither, so it
# crash-loops. The unprivileged variant listens on 8080 and keeps its writable
# state where a random UID can reach it. Note the published port in
# docker-compose.yml maps to 8080 for the same reason.

FROM node:22-slim AS build
WORKDIR /app
# If registry.npmjs.org is unreachable from the build network - which it is from
# inside Aramco - point npm at the internal mirror:
#   --build-arg NPM_REGISTRY=https://<artifactory>/api/npm/npm/
ARG NPM_REGISTRY
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY --from=build /app/dist/novelty-lens/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# nginx's own base image runs every executable script in /docker-entrypoint.d/
# before starting nginx - no custom ENTRYPOINT needed.
COPY docker-entrypoint.sh /docker-entrypoint.d/40-novelty-lens-env.sh

# Root only to set permissions, then back to the unprivileged user. The web root
# needs GROUP write, not just read: the entrypoint writes env-config.js into it
# on every start, and under OpenShift the writer is an arbitrary UID in group 0.
USER root
RUN chmod +x /docker-entrypoint.d/40-novelty-lens-env.sh \
 && chgrp -R 0 /usr/share/nginx/html \
 && chmod -R g=u /usr/share/nginx/html
USER 101

EXPOSE 8080
