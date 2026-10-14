# Konsola Central (React/Vite, vetëm stafi) ndërtohet me Node dhe shërbehet nga nginx, që bën proxy VETËM drejt API-së Central.
# Pa sekrete në build: frontend-i s'ka variabla mjedisi; API-ja thirret me rrugë relative (/auth, /admin) në të njëjtin origin.
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend-central/package.json frontend-central/package-lock.json ./
RUN npm ci
COPY frontend-central/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.central.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8081
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8081/nginx-health || exit 1
