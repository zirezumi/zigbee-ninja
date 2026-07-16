# Releasing zigbee-ninja

Releases are cut by pushing an annotated version tag; everything else is
automated by `.github/workflows/release.yml`. Nothing releases from ordinary
pushes — `main` stays a moving development line (`:latest` + `:sha-*` images
from CI, as today).

## Versioning

Semantic versioning, pre-1.0 rules: **0.MINOR.PATCH**.

- **MINOR** — new capability or any behavior/API change a user could notice
  (view semantics, API shapes, probe/agent protocol, metric identifiers).
- **PATCH** — fixes and internal changes with no observable contract change.
- 1.0.0 comes when the HA add-on packaging lands and the API/probe contracts
  are declared stable.

The version lives in **two places that must agree** —
`collector/pyproject.toml` (`project.version`) and
`collector/zigbee_ninja/__init__.py` (`__version__`). The release workflow
fails if the tag does not match `pyproject.toml`.

## Cutting a release

1. On a green `main`, bump the version in both files above
   (e.g. `0.1.0.dev0` → `0.1.0`), update README status if it moved, commit
   (DCO sign-off as always), push, wait for CI green.
2. Tag and push:

   ```sh
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

3. The Release workflow then: re-runs every gate against the tagged commit
   (lint, tests, license policy, frontend build, tag↔version check), builds
   the multi-arch image (amd64/arm64), pushes
   `ghcr.io/zirezumi/zigbee-ninja:vX.Y.Z`, signs it with **cosign keyless**
   (GitHub Actions OIDC — no long-lived key exists), and creates the GitHub
   release with generated notes.
4. Post-release: bump `main` to the next `.devN` version
   (e.g. `0.1.1.dev0`) in both files.

## Verifying a release image

Anyone can verify provenance without any key distribution:

```sh
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp 'https://github.com/zirezumi/zigbee-ninja/\.github/workflows/release\.yml@refs/tags/v.*' \
  ghcr.io/zirezumi/zigbee-ninja:v0.1.0
```

This proves the image was built by this repository's release workflow from
the stated tag — the §15 image-signing posture.

## Open decision (owner)

Whether release tags should also move `:latest`. Today CI moves `:latest` on
every `main` push (development semantics). The conventional alternative once
releases exist: `main` pushes `:edge`, releases move `:latest`. Switching
changes what `docker pull zigbee-ninja` gives existing users of the GHCR
image, so it is deliberately **not** changed until ratified; the release
workflow pushes only immutable `vX.Y.Z` tags until then.
