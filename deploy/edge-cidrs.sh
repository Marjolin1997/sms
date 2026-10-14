#!/bin/sh
# M11-I/F-02: gjeneron nga mjedisi (CIDR të dhëna nga operatori) skedarët e përfshirë nga nginx. Ekzekutohet nga entrypoint-i i imazhit nginx
# (/docker-entrypoint.d). NUK ka vlera të ngulitura: parazgjedhja është "mos beso asnjë proxy" dhe "asnjë rrjet i brendshëm përveç loopback".
#
#   EDGE_TRUSTED_PROXY_CIDRS   proxy-t e besuara para këtij nginx (LB/WAF/proxy TLS): real_ip + normalizimi i X-Forwarded-For/-Proto
#   EDGE_INTERNAL_ALLOW_CIDRS  kush mund të arrijë /healthz dhe /readyz (loopback lejohet gjithmonë)
#   EDGE_S2S_ALLOW_CIDRS       kush mund të arrijë listener-in e brendshëm Enterprise->Central (vetëm Central; parazgjedhja: askush)
# Format: lista e ndarë me hapësirë ose presje; IPv4/IPv6 me maskë opsionale. Çdo /0 (besim për gjithë rrjetin) REFUZOHET.
set -eu

DIR="${EDGE_NGINX_DIR:-/etc/nginx}"  # sobrescrivet vetëm nga testet

valid() {
  case "$1" in
    */0) return 1 ;;
  esac
  printf '%s' "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$|^[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(/[0-9]{1,3})?$'
}

each() { # each "<lista>" -> një CIDR për rresht, i vlefshëm ose dështim
  for c in $(printf '%s' "$1" | tr ',' ' '); do
    if ! valid "$c"; then
      echo "edge-cidrs: CIDR i pavlefshëm ose i refuzuar (/0 s'lejohet): '$c'" >&2
      exit 1
    fi
    echo "$c"
  done
}

TRUSTED="$(each "${EDGE_TRUSTED_PROXY_CIDRS:-}")"
INTERNAL="$(each "${EDGE_INTERNAL_ALLOW_CIDRS:-}")"
S2S="$(each "${EDGE_S2S_ALLOW_CIDRS:-}")"

{ echo "# gjeneruar nga edge-cidrs.sh"; for c in $TRUSTED; do echo "set_real_ip_from $c;"; done; } > "$DIR/trusted_proxies.conf"
{ echo "# gjeneruar nga edge-cidrs.sh"; for c in $TRUSTED; do echo "$c 1;"; done; } > "$DIR/trusted_proxies_geo.conf"
{ echo "# gjeneruar nga edge-cidrs.sh"; echo "allow 127.0.0.1;"; echo "allow ::1;"; for c in $INTERNAL; do echo "allow $c;"; done; echo "deny all;"; } > "$DIR/internal_allow.conf"
{ echo "# gjeneruar nga edge-cidrs.sh"; for c in $S2S; do echo "allow $c;"; done; echo "deny all;"; } > "$DIR/s2s_allow.conf"
echo "edge-cidrs: trusted_proxies=$(echo "$TRUSTED" | grep -c . || true) internal_allow=$(echo "$INTERNAL" | grep -c . || true) s2s_allow=$(echo "$S2S" | grep -c . || true)"
