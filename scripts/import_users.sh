#!/usr/bin/env sh
# Add real users and their roles on the server, in one go. Edit the list below, then from syllabus-ai-backend/:
#   ./scripts/import_users.sh               # they sign in with Google (@narxoz.kz)
#   ./scripts/import_users.sh --password    # also one shared password for all of them (asked for, not echoed)
#
# Safe to run again: existing people only get the roles that are missing (nothing is removed).
# All or nothing: if any line is wrong, nobody is added and the wrong lines are printed.
# Needs the stack running (docker compose up -d). Same as: python -m app.cli import-users users.csv
#
# Columns: email,role,school,program,name,title  (only email and role are required)
#   role:    admin | provost | kok | school_admin | program_head | professor
#   school:  school_admin, program_head: economics | humanities | law | digital (or the School's slug or name)
#   program: program_head only, instead of school: the program's slug
# One line per role: a person with two roles takes two lines. Lines starting with # are skipped.
set -eu
cd "$(dirname "$0")/.."

USERS=$(cat <<'CSV'
email,role,school,program,name,title
# it.admin@narxoz.kz,admin,,,IT Administrator,
# daniar.aitanov@narxoz.kz,provost,,,Daniar Aitanov,Provost
# kok.secretary@narxoz.kz,kok,,,KOK Secretary,
# ivanov@narxoz.kz,school_admin,digital,,Ivan Ivanov,
# petrova@narxoz.kz,program_head,law,,Anna Petrova,Head of Program
# sidorov@narxoz.kz,professor,,,Petr Sidorov,Associate Professor
CSV
)

if ! printf '%s\n' "$USERS" | sed '1d' | grep -qv '^[[:space:]]*\(#\|$\)'; then
  echo "The list in $0 is empty: add one line per person and role (see the examples), then run it again." >&2
  exit 1
fi

IMPORT_USERS_PASSWORD=""
if [ "${1:-}" = "--password" ]; then
  printf 'password for all of them (at least 8 characters): ' >&2
  stty -echo; read -r IMPORT_USERS_PASSWORD; stty echo; printf '\n' >&2
  printf 'again: ' >&2
  stty -echo; read -r again; stty echo; printf '\n' >&2
  [ "$IMPORT_USERS_PASSWORD" = "$again" ] || { echo "error: the passwords differ" >&2; exit 1; }
fi
export IMPORT_USERS_PASSWORD

# -T: the list goes in on stdin; -e NAME passes the password from this shell's environment, not the command line.
printf '%s\n' "$USERS" | docker compose exec -T -e IMPORT_USERS_PASSWORD api python -m app.cli import-users -
