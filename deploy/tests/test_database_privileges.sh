#!/usr/bin/env bash
# Actual SQL against CI's disposable migrated production-schema fixture ONLY.
# SQL dollar-quoted literals below must reach PostgreSQL unchanged.
# shellcheck disable=SC2016
set -Eeuo pipefail
[[ ${FIREDASH_DISPOSABLE_DATABASE_TEST:-} == 1 ]] || {
    echo 'Refusing database tests without disposable-fixture opt-in' >&2; exit 1;
}
ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../lib/verify-postgresql.sh
source "$ROOT/deploy/lib/verify-postgresql.sh"
fail() { echo "FAIL: $*" >&2; }
ok() { echo "OK: $*"; }
owner() { PGPASSWORD="$OWNER_PW" psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U database_owner -d fire_backend "$@"; }
check() { owner -f "$ROOT/deploy/postgresql/verify-privileges.sql"; }
reject() { if expect_sqlstate "$@"; then echo 'Probe incorrectly passed' >&2; exit 1; fi; }
cleanup() { owner -c 'DROP TABLE IF EXISTS public.fd_security_probe; DROP SEQUENCE IF EXISTS public.fd_security_sequence;' >/dev/null; }
trap cleanup EXIT

# Reapply on an existing installation, including stale backup sequence grants.
owner -c 'CREATE TABLE public.fd_security_probe (id integer PRIMARY KEY, value text); INSERT INTO public.fd_security_probe VALUES (1, $$unchanged$$); CREATE SEQUENCE public.fd_security_sequence;'
owner -c 'GRANT USAGE, UPDATE ON SEQUENCE public.fd_security_sequence TO backup_role;'
owner -c 'ALTER DEFAULT PRIVILEGES FOR ROLE database_owner IN SCHEMA public GRANT USAGE, UPDATE ON SEQUENCES TO backup_role;'
owner -f "$ROOT/deploy/postgresql/roles.sql"
owner -f "$ROOT/deploy/postgresql/roles.sql"
check
PGPASSWORD="$RUNTIME_PW" psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U application_runtime -d fire_backend \
    -c 'BEGIN; INSERT INTO public.fd_security_probe VALUES (2, $$temporary$$); UPDATE public.fd_security_probe SET value = $$updated$$ WHERE id = 2; DELETE FROM public.fd_security_probe WHERE id = 2; ROLLBACK;'

expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'UPDATE public.audit_event SET id = id'
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'DELETE FROM public.audit_event'
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'TRUNCATE public.audit_event'
expect_sqlstate database_owner "$OWNER_PW" P0001 'DELETE FROM public.audit_event WHERE false'
expect_sqlstate database_owner "$OWNER_PW" P0001 'UPDATE public.audit_event SET id = id WHERE false'
expect_sqlstate database_owner "$OWNER_PW" P0001 'TRUNCATE public.audit_event'
for table in django_migrations publications_datasettyperegistry; do
    for verb in "UPDATE public.$table SET id = id" "DELETE FROM public.$table" "TRUNCATE public.$table"; do
        # Use the registry's actual column so syntax errors cannot mask denial.
        [[ $table != publications_datasettyperegistry ]] || verb=${verb//id = id/code = code}
        expect_sqlstate application_runtime "$RUNTIME_PW" 42501 "$verb"
    done
done
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'INSERT INTO public.django_migrations (app, name, applied) VALUES ($$probe$$, $$probe$$, now())'
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'INSERT INTO public.publications_datasettyperegistry (code) VALUES ($$probe$$)'
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'CREATE TABLE public.fd_forbidden (id integer)'
expect_sqlstate application_runtime "$RUNTIME_PW" 42501 'SET ROLE database_owner'
expect_sqlstate backup_role "$BACKUP_PW" 42501 'SET ROLE database_owner'
expect_sqlstate backup_role "$BACKUP_PW" 42501 'SET ROLE application_runtime'
expect_sqlstate backup_role "$BACKUP_PW" 42501 'UPDATE public.fd_security_probe SET value = $$changed$$'
expect_sqlstate backup_role "$BACKUP_PW" 42501 "SELECT nextval('public.fd_security_sequence')"
expect_sqlstate backup_role "$BACKUP_PW" 42501 "SELECT setval('public.fd_security_sequence', 99)"
sequence_state=$(PGPASSWORD="$BACKUP_PW" psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U backup_role -d fire_backend \
    -c 'SELECT last_value, is_called FROM public.fd_security_sequence')
[[ $sequence_state == '1|f' ]]

# Successful mutations must fail the probe AND leave the rows intact.
for statement in \
    'UPDATE public.fd_security_probe SET value = $$changed$$' \
    'DELETE FROM public.fd_security_probe' \
    'TRUNCATE public.fd_security_probe'; do
    reject database_owner "$OWNER_PW" 42501 "$statement"
    [[ $(owner -c 'SELECT id::text || value FROM public.fd_security_probe') == 1unchanged ]]
done
reject database_owner "$OWNER_PW" 42501 'SELECT * FROM public.fd_nonexistent_relation'
reject database_owner "$OWNER_PW" 42501 'THIS IS INVALID SQL'
reject database_owner "$OWNER_PW" 42501 'SELECT 1 / 0'
reject fd_nonexistent_role invalid 42501 'SELECT 1'

# Each intentional broken protection is transactional, never left installed.
broken() {
    local statement=$1 out rc
    out=$(PGPASSWORD="$ADMIN_PW" psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres -d fire_backend -c BEGIN -c "$statement" -c '\echo FD_MUTATION_APPLIED' \
        -f "$ROOT/deploy/postgresql/verify-privileges.sql" 2>/dev/null) && rc=0 || rc=$?
    [[ $rc -ne 0 ]] && grep -Fxq FD_MUTATION_APPLIED <<<"$out"
}
broken 'GRANT UPDATE ON public.django_migrations TO application_runtime'
broken 'GRANT INSERT (app) ON public.django_migrations TO application_runtime'
broken 'GRANT DELETE ON public.publications_datasettyperegistry TO application_runtime'
broken 'GRANT UPDATE ON public.audit_event TO application_runtime'
broken 'GRANT USAGE ON SEQUENCE public.fd_security_sequence TO backup_role'
broken 'GRANT UPDATE ON public.fd_security_probe TO backup_role'
broken 'GRANT CREATE ON SCHEMA public TO application_runtime'
broken 'REVOKE DELETE ON public.organizations_department FROM application_runtime'
broken 'GRANT database_owner TO application_runtime'
broken 'GRANT application_runtime TO backup_role'
broken 'ALTER TABLE public.django_migrations RENAME TO fd_hidden_migration_history'
check

# Missing/disabled audit triggers are detected by the same bounded live probe.
for change in 'ALTER TABLE public.audit_event DISABLE TRIGGER audit_event_immutable' \
    'DROP TRIGGER audit_event_immutable ON public.audit_event'; do
    out=$(owner -c BEGIN -c "$change" -c 'DELETE FROM public.audit_event WHERE false' \
        -c '\echo FD_TRIGGER_MISSING' -c ROLLBACK)
    grep -Fxq FD_TRIGGER_MISSING <<<"$out"
done
expect_sqlstate database_owner "$OWNER_PW" P0001 'DELETE FROM public.audit_event WHERE false'

# Exercise the shared probe's failure result with a missing or disabled clone
# of the real statement-level audit trigger, keeping operational audit DDL intact.
owner -c 'CREATE TRIGGER fd_probe_immutable BEFORE DELETE OR UPDATE OR TRUNCATE ON public.fd_security_probe FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_event_mutation();'
expect_sqlstate database_owner "$OWNER_PW" P0001 'DELETE FROM public.fd_security_probe'
owner -c 'ALTER TABLE public.fd_security_probe DISABLE TRIGGER fd_probe_immutable;'
reject database_owner "$OWNER_PW" P0001 'DELETE FROM public.fd_security_probe'
owner -c 'DROP TRIGGER fd_probe_immutable ON public.fd_security_probe;'
reject database_owner "$OWNER_PW" P0001 'DELETE FROM public.fd_security_probe'
[[ $(owner -c 'SELECT id::text || value FROM public.fd_security_probe') == 1unchanged ]]

# New sequences must also inherit read-only backup defaults.
owner -c 'DROP SEQUENCE public.fd_security_sequence; CREATE SEQUENCE public.fd_security_sequence;'
check
runtime_value=$(PGPASSWORD="$RUNTIME_PW" psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U application_runtime -d fire_backend \
    -c "SELECT nextval('public.fd_security_sequence')")
[[ $runtime_value == 1 ]]
[[ $(owner -c "SELECT nextval('public.fd_security_sequence')") == 2 ]]
echo 'OK: disposable production-schema privilege and rollback regressions'
