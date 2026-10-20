# Working in this repository

The Odoo module for efis, a fiscalisation platform. Read `README.md` first. The rules below come
from efis's `CLAUDE.md`; when one is unclear, efis's `BUILD_SPEC.md` and `docs/research/` decide.
Do not guess a fiscal rule.

The module is a client of efis. It never signs, numbers, computes a security code, holds a
certificate or key, or talks to the tax authority. `tests/test_module.py` enforces these:

| Invariant | Enforced by |
| --- | --- |
| `skip_readonly_check` is never used | `InvariantTests` |
| The authority's field names stay out of the module code | `InvariantTests` |
| No government host appears in the module | `InvariantTests` |
| The module never signs and never holds a key | `InvariantTests` |
| XML IDs are frozen for the module's life | `ManifestTests.test_xml_ids_are_frozen` |
| The zip is byte-identical on every rebuild | `PackageTests` |
| The install check sends to the mock authority only | `ci/odoo-install.sh` |

An invoice sent for fiscalisation is never edited, cancelled or deleted, and a resend uses the same
key, never a new document. Never retry a client fault. Format money with exactly two decimals.

Merge requests target `develop`. A human merges anything touching the payload, the XML IDs, or
`contract/`. Never commit to `20.0`.
