# Konsola (React) ndërtohet me Node dhe shërbehet nga nginx, që bën edhe proxy drejt API-së.
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
# M11-I/F-02: CIDR-t e proxy-ve të besuara/rrjetit të brendshëm vijnë nga mjedisi në nisje (asnjë vlerë e ngulitur; parazgjedhja = mos beso asnjë).
# Skedarët bosh më poshtë garantojnë që `nginx -t` punon edhe pa entrypoint; edge-cidrs.sh i rigjeneron në nisje.
COPY deploy/edge-cidrs.sh /docker-entrypoint.d/40-edge-cidrs.sh
RUN chmod 0755 /docker-entrypoint.d/40-edge-cidrs.sh \
 && : > /etc/nginx/trusted_proxies.conf && : > /etc/nginx/trusted_proxies_geo.conf \
 && printf 'allow 127.0.0.1;\nallow ::1;\ndeny all;\n' > /etc/nginx/internal_allow.conf \
 && printf 'deny all;\n' > /etc/nginx/s2s_allow.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/nginx-health || exit 1
