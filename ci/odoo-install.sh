#!/bin/sh
# DEV-1731: install metanow_fiscalization into a disposable Odoo 20 and post one invoice through
# efis to the mock authority. Run by the odoo-install CI job, in docker:27 with a dind service.
#
# Needs: EFIS_IMAGE (the efis image this repository is tested against, pinned in .gitlab-ci.yml),
# ci/efis-compose.yml (efis's compose.yml from that same version, see contract/README.md),
# dist/odoo/addons (the unpacked zip from the package job), and optionally EFIS_ODOO_IMAGE
# (default odoo:20.0).
set -eu

MOCK_ENDPOINT="http://mock-cis:8080/FiscalizationService-v3"
export EFIS_GATEWAY_ENDPOINT="$MOCK_ENDPOINT"
export EFIS_GATEWAY_SIGNER=mock
export EFIS_AUTHORITY_ENVIRONMENT=Test
export EFIS_PULL_POLICY=always
export ASPNETCORE_ENVIRONMENT=Production

# Never anywhere but the mock: this job has no business reaching the authority, test or production.
case "$EFIS_GATEWAY_ENDPOINT" in
  "$MOCK_ENDPOINT") ;;
  *) echo "odoo-install sends to the mock authority only." >&2; exit 1 ;;
esac

test -n "${EFIS_IMAGE:-}" || { echo "EFIS_IMAGE is required." >&2; exit 1; }
test -f dist/odoo/addons/metanow_fiscalization/__manifest__.py || { echo "dist/odoo/addons is missing; run odoo-package first." >&2; exit 1; }

random() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }
EFIS_POSTGRES_PASSWORD="$(random)"
EFIS_PROVISIONING_KEY="$(random)"
EFIS_ADMIN_EMAIL="admin-$(random | cut -c1-8)@example.invalid"
EFIS_ADMIN_PASSWORD="$(random)"
export EFIS_POSTGRES_PASSWORD EFIS_PROVISIONING_KEY EFIS_ADMIN_EMAIL EFIS_ADMIN_PASSWORD

# One compose project per job. When the dind service does not come up, jobs fall back to the
# runner's shared daemon, and a fixed project name let two concurrent jobs recreate and tear down
# each other's containers (pipelines #5321 and #5322).
PROJECT="efis-odoo-ci-${CI_JOB_ID:-local-$$}"
compose() { docker compose -p "$PROJECT" --project-directory . -f ci/efis-compose.yml -f ci/compose.odoo.yml "$@"; }
trap 'compose --profile odoo logs --no-color migrate api worker mock-cis > odoo-install-efis.log 2>&1 || true; compose --profile odoo down -v --remove-orphans > /dev/null 2>&1 || true' EXIT

compose up -d api worker mock-cis
healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q "$1")" 2>/dev/null)" = healthy ]; }
tries=0
until healthy api && healthy worker; do
  tries=$((tries + 1))
  [ "$tries" -le 60 ] || { echo "efis did not become healthy." >&2; exit 1; }
  sleep 5
done
compose --profile odoo build odoo driver

PAIRING="$(compose --profile odoo run --rm -T driver | tail -n 1)"
field() { printf '%s' "$PAIRING" | sed -n "s/.*\"$1\": *\"\\([^\"]*\\)\".*/\\1/p"; }

compose --profile odoo up -d --wait odoo-db
# The install output is kept as an artifact either way; set -e still fails the job on a bad install.
compose --profile odoo run --rm -T odoo odoo -d ci -i metanow_fiscalization --stop-after-init --no-http > odoo-install.log 2>&1 \
  || { tail -n 100 odoo-install.log >&2; exit 1; }

compose --profile odoo run --rm -T \
  -e EFIS_BASE_URL=http://api:8080 \
  -e EFIS_PAIRING_CODE="$(field code)" \
  -e EFIS_TAX_ID="$(field taxId)" \
  -e EFIS_BUSINESS_UNIT_CODE="$(field businessUnitCode)" \
  -e EFIS_OPERATOR_CODE="$(field operatorCode)" \
  -e EFIS_SOFTWARE_CODE="$(field softwareCode)" \
  odoo odoo shell -d ci < ci/post_invoice.py | tee -a odoo-install.log

grep -q '^ODOO-INSTALL-PASSED' odoo-install.log || { echo "The invoice was not fiscalised through efis." >&2; exit 1; }
