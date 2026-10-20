# Fiskalizimi me efis / Fiscalisation with efis

The Odoo module `metanow_fiscalization`. It sends each posted customer invoice to efis and stores
what efis returns. It does not sign, number or compute a security code, holds no certificate or
key, and never talks to the tax authority. The user guide is efis's `docs/guide/odoo.md`.

This repository is separate from efis on purpose. efis is certified, and a certified version is
expensive to change, so it moves rarely. The module follows Odoo, which ships a new series every
year. The two meet only at efis's public API (`contract/README.md`).

| Path | What it is |
| --- | --- |
| `metanow_fiscalization/` | The module. The Odoo Apps store reads this folder. |
| `tests/` | Unit tests. They need no Odoo and no network. |
| `tools/package.py` | Builds the reproducible zip. |
| `ci/` | The install check: the module in a disposable Odoo beside efis and its mock authority. |
| `contract/` | Copies from the efis version the module is tested against. |

## Branches and releases

- **`develop`**: day-to-day work. Merge requests target it. Its pipeline runs the tests, builds the
  zip and installs it into Odoo beside efis.
- **`main`**: releases. Merge `develop` into `main` to release. Bump `version` in
  `__manifest__.py` first: a version is released once, and the pipeline refuses to release it again.
- **`20.0`**: written only by the release job on `main`. It is what GitHub mirrors and what the Odoo
  Apps store reads. Never commit to it, here or on GitHub; the release push is not forced, so a
  stray commit there stops the next release.

A release publishes the zip, its SHA-256 and `manifest.json` to this project's package registry
(Deploy › Package registry, `metanow_fiscalization/<version>`), then moves `20.0`.

When Odoo 21 comes, first branch `release/20.0` from `main` if 20.0 will still need fixes. Then, on
`develop`, set `ODOO_SERIES` and `EFIS_ODOO_IMAGE` in `.gitlab-ci.yml` to 21.0 and the manifest's
version to `21.0.1.0.0`. The first release from `main` then creates branch `21.0`, which is
registered on the store as a new series.

## Working on it

```sh
python -m unittest discover -s tests -t tests      # Python 3.13, no Odoo needed
python tools/package.py --out dist/odoo            # prints the zip and its SHA-256
```

The install check needs Docker and the efis image: see `ci/odoo-install.sh`.

## Setting up (once)

1. **Job token.** In efis/efis, Settings › CI/CD › Job token permissions, add `efis/odoo-apps`, so
   the install job can pull the efis image.
2. **Release token.** In this project, Settings › Access tokens: role Maintainer, scope
   `write_repository`. Add it here as the CI/CD variable `ODOO_APPS_PUSH_TOKEN`, protected and
   masked.
3. **GitHub mirror.** Create `Metanow-dev/odoo-apps` on GitHub, empty. Here, Settings › Repository ›
   Mirroring repositories: push to `ssh://git@github.com/Metanow-dev/odoo-apps.git`, SSH public key
   authentication. Add the key GitLab shows to the GitHub repository's Deploy keys with write access.
4. **Odoo Apps store.** Register `ssh://git@github.com/Metanow-dev/odoo-apps.git#20.0` at
   apps.odoo.com/apps/upload.
