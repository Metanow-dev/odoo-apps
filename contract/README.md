# The efis version this module is built against

The module talks to efis over efis's public API and nothing else. Two files here are copies from
the efis repository (GitLab `efis/efis`), taken at one efis version, and must move together:

| File | Copied from efis |
| --- | --- |
| `contract/efis-openapi-v1.json` | `spec/openapi/v1.json` |
| `ci/efis-compose.yml` | `compose.yml` |

`EFIS_IMAGE` in `.gitlab-ci.yml` names the efis image of that same version. The unit tests check
the module's requests against the OpenAPI copy, and the `install` job runs the module against
that image.

Current version: efis commit `45ed0b62042e912d64df4fc417f47649636524ef` (develop, 2026-11-06).

To move to a newer efis: copy both files from the efis commit or tag, set `EFIS_IMAGE` to that
commit's image, update the line above, and merge it to develop like any other change. Never edit
the copies by hand; efis is their source.
