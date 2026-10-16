# shellcheck shell=bash
# Funksione të përbashkëta për backup.sh / restore.sh (M11-I/F-05A). Burohet (`source`), nuk ekzekutohet.
#
# Parimi i kredencialeve: URL-ja ndahet në ndryshoret libpq (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSSLMODE),
# kështu fjalëkalimi NUK shfaqet te argumentet e proceseve (`ps`) dhe asnjë mesazh nuk e printon URL-në.

# Eksporton ndryshoret libpq nga një URL postgresql[+driver]://user[:pass]@host[:port]/db[?sslmode=…].
pg_parse_url() {
  local url="$1" re rest
  re='^postgres(ql)?(\+[a-z0-9]+)?://([^:@/]+)(:([^@]*))?@([^:/?]+|\[[^]]+\])(:([0-9]+))?/([^?]+)(\?(.*))?$'
  if [[ ! "$url" =~ $re ]]; then
    echo "invalid database URL (not printed: it may contain a password)" >&2
    return 2
  fi
  export PGUSER="${BASH_REMATCH[3]}"
  export PGPASSWORD
  PGPASSWORD="$(pg_urldecode "${BASH_REMATCH[5]}")"
  export PGHOST="${BASH_REMATCH[6]}"
  export PGPORT="${BASH_REMATCH[8]:-5432}"
  export PGDATABASE="${BASH_REMATCH[9]}"
  rest="${BASH_REMATCH[11]}"
  unset PGSSLMODE
  if [[ "$rest" =~ (^|&)sslmode=([a-z-]+) ]]; then export PGSSLMODE="${BASH_REMATCH[2]}"; fi
  return 0
}

pg_urldecode() {
  local s="${1//\\/\\\\}"  # një "\" i vërtetë në fjalëkalim nuk duhet të interpretohet nga %b
  printf '%b' "${s//%/\\x}"
}

# Emri i tabelës së versioneve sipas etiketës; etiketa e kundërt shërben si mbrojtje nga përzierja Enterprise/Central.
pg_version_table() {
  case "$1" in
    enterprise) echo "sms_alembic_version" ;;
    central) echo "central_alembic_version" ;;
    *) echo "unknown label '$1' (expected enterprise|central)" >&2; return 2 ;;
  esac
}

pg_other_label() { [[ "$1" == enterprise ]] && echo central || echo enterprise; }

# Ekziston tabela $1 në bazën e lidhur (PG* nga mjedisi)?
pg_has_table() {
  local out
  out="$(psql -X -v ON_ERROR_STOP=1 -Atc "select 1 from information_schema.tables where table_schema='public' and table_name='$1'")" || return 2
  [[ "$out" == 1 ]]
}

# Revizioni i regjistruar te tabela e versioneve (një rresht); bosh nëse tabela s'ka rresht.
pg_revision() {
  psql -X -v ON_ERROR_STOP=1 -Atc "select version_num from $1"
}

# Etiketa e një arkivi dump nga PËRMBAJTJA (tabela e versioneve), jo nga emri/meta: enterprise | central | (bosh = e pavlefshme/e paqartë).
dump_label() {
  local toc has_e has_c
  toc="$(pg_restore --list "$1")" || return 2
  has_e=0; has_c=0
  grep -q ' TABLE public sms_alembic_version ' <<<"$toc" && has_e=1
  grep -q ' TABLE public central_alembic_version ' <<<"$toc" && has_c=1
  if (( has_e == 1 && has_c == 0 )); then echo enterprise
  elif (( has_c == 1 && has_e == 0 )); then echo central
  fi
}
