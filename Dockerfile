# Imazh i vetëm për API dhe workers Enterprise (komanda ndryshon në compose) + një target `central` për Central.
# Frontend-et ndërtohen veçmas (deploy/web.Dockerfile, deploy/web-central.Dockerfile) dhe shërbehen nga nginx.
# Stage-i i FUNDIT është Enterprise: `docker build .` / `build: .` mbetet i pandryshuar; Central ndërtohet me `--target central`.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Central (staf admin/operator): apps/central + kontratat/PDF të përbashkëta (`packages`). Pa sekrete, pa frontend, pa skedarë env.
# Pa HEALTHCHECK në imazh: i njëjti imazh shërben API-n, migrimin dhe jobet një-herë; kontrolli i gatishmërisë `/readyz`
# jeton te shërbimi `central-api` në compose (një mekanizëm i vetëm).
FROM base AS central
COPY apps ./apps
COPY packages ./packages
RUN useradd -r -u 10001 sms && chown -R sms /srv
USER sms
EXPOSE 8001
# --proxy-headers e heq: IP-në e klientit e trajton aplikacioni (CENTRAL_TRUSTED_PROXY_HOPS)
CMD ["uvicorn", "apps.central.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-proxy-headers", "--workers", "2"]

# --- Enterprise (stage-i i fundit = parazgjedhja)
FROM base AS enterprise
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# cp.v1 (kontratë e përbashkët, stdlib-only): `app.services.control_plane_sync` e importon
COPY packages ./packages
COPY scripts ./scripts
RUN useradd -r -u 10001 sms && chown -R sms /srv
USER sms
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=4 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).status == 200 else 1)"
# --proxy-headers e heq: IP-në e klientit e trajton aplikacioni (SMS_TRUSTED_PROXY_HOPS)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--workers", "2"]
