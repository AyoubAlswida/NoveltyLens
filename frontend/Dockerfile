# Multi-stage build: compile the Angular app, then serve the static output
# with nginx. The backend URL is NOT baked in at build time - docker-entrypoint.sh
# writes it into env-config.js from $API_URL when the container starts, using
# the same window.NOVELTY_LENS_API_URL hook src/app/config.ts already reads.
# That means one built image can point at any backend without a rebuild.

FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist/novelty-lens/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# nginx's own base image runs every executable script in /docker-entrypoint.d/
# before starting nginx - no custom ENTRYPOINT needed.
COPY docker-entrypoint.sh /docker-entrypoint.d/40-novelty-lens-env.sh
RUN chmod +x /docker-entrypoint.d/40-novelty-lens-env.sh

EXPOSE 80
