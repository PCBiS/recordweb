# RecordWEB v1.2.9 application patch — 2026-10-26

This release publishes the application fixes deployed from source commit
`d027cdda41972a87273bb7e114baafbebc1e35ba` as
`ghcr.io/pcbis/recordweb:v1.2.9-patch-20261026` for **linux/amd64**.
The release workflow records the exact published digest and its source revision.

## Behavior and security fixes

- System status reports the filesystems containing configured monitoring
  directories, or channel output directories when no paths are selected.
  Repeated paths on the same filesystem produce one capacity entry. This is
  filesystem capacity and available space, not a recursive directory-size scan.
- Unauthenticated requests stop before protected configuration and cookie
  handlers execute, while page navigation still reaches the login screen.
- File-manager destination names reject traversal and path separators; the
  resolved destination must also remain inside an allowed root.
- Account changes do not log passwords. Account checks, deletion and session
  invalidation are covered by synthetic regression tests. Cookies and settings
  continue to use the existing account and data formats.

The image inherits the verified CA-only v1.2.9 base by digest:
`ghcr.io/pcbis/recordweb@sha256:25253c35e1e07dfa1234abbb01c173168d61c3f13506f7d099a5d16e0c479f0b`.
Only six reviewed application files and three image source/version labels change.
Dependencies, the container entrypoint, environment, volumes and the native
Docker healthcheck retain the base image configuration. CIME code and the
previously identified dependency CVEs are outside this release.

## Publication and validation

Pushes to `codex/recordweb-release-20261026` invoke the dedicated
`publish-app-patch.yml` workflow using only its runner `GITHUB_TOKEN`.
It builds and pushes a uniquely named OCI candidate, then pulls it by digest.
Before publishing the release tag, it checks:

1. All inherited layer diffIDs and image history are preserved; six added COPY
   layers contain exactly the six checkout files with matching SHA-256 hashes.
2. All runtime configuration is preserved apart from the three reviewed labels,
   including the original native Docker healthcheck metadata.
3. System, certifi, Requests and HTTPX CA fingerprints and on-disk CA data match
   the pinned base; both native CA refresh forms are idempotent.
4. All 31 regression tests and the existing CA policy verify command pass in the
   pulled final image, with no network, production mounts or app entrypoint.

The workflow refuses to overwrite an existing release tag and checks again
immediately before promotion. It promotes the exact verified single OCI manifest
without creating an index, then compares its raw manifest with the candidate.
The existing `publish-image.yml` is not invoked: this publication does not push
`main`, create a `v*` Git tag, or change `latest` or either CA image tag.
Verification reports and raw manifest evidence are retained as workflow artifacts.

## Deployment

Use the verified release digest shown in the workflow result. Existing mounts,
settings, cookies and recordings must be backed up and retained. Applying an
image update can restart active recording; obtain the operator's deployment
approval and use the existing Portainer stack procedure. Rollback uses the
previous image digest and the preserved stack configuration. Publishing this
release itself performs no production deployment or restart.
