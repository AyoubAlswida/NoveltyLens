# NoveltyLens frontend: Angular built to static files, served by nginx.
#
# Two OpenShift-specific choices, both load-bearing:
#
#   1. The runtime image is nginx-UNPRIVILEGED. Stock nginx binds port 80 and
#      writes its pid and caches into root-owned directories; under the default
#      `restricted-v2` SCC the container is an arbitrary non-root UID and can do
#      neither, so it crash-loops. The unprivileged variant listens on 8080 and
#      keeps its writable state where a random UID can reach it.
#   2. The backend URL is injected at CONTAINER START, not at build time. A
#      Route's hostname is not known when the image is built, and baking it in
#      would mean one image per environment. See docker-entrypoint.sh.

FROM node:24-alpine AS build
WORKDIR /src
# If registry.npmjs.org is blocked on the build network, point npm at the
# internal mirror:  --build-arg NPM_REGISTRY=https://<artifactory>/api/npm/npm/
ARG NPM_REGISTRY
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi
# package*.json first: dependency install is the slow layer and changes rarely.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:alpine
COPY --from=build /src/dist/novelty-lens/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.d/40-write-env-js.sh

# Arbitrary-UID fix: the entrypoint writes env.js into the web root on every
# start, so group 0 needs write access to that directory, not just read.
USER root
RUN chmod +x /docker-entrypoint.d/40-write-env-js.sh \
 && chgrp -R 0 /usr/share/nginx/html \
 && chmod -R g=u /usr/share/nginx/html
USER 101

EXPOSE 8080
