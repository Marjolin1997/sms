# Imazh i vetëm për API dhe workers (komanda ndryshon në compose). Frontend-i ndërtohet veçmas
# (deploy/web.Dockerfile) dhe shërbehet nga nginx.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
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
