# v1.2.9 application patch — 2026-10-25

The dashboard now measures configured recording directories instead of listing
every container mount. Authentication failures stop protected route handlers;
file manager names and destination boundaries are checked before writes; account
changes do not print credentials.

## Storage display

The settings page has a **표시할 저장 경로** field. Enter one directory per line,
for example `/recordings`. Leave it empty to use channel recording destinations.
The persisted setting is `systemMonitorPaths`, a list of strings. Relative paths
are resolved against the application directory, as recording paths are.

Paths on the same filesystem share one capacity row. Missing or unreadable
directories display **확인 불가**, without substituting a parent directory.
This reports filesystem total/used/free space; it does not recursively count
files or allocate a separate capacity to each folder. File manager roots and
recording destinations are unchanged by this display setting.

## Application security

- Unauthenticated protected HTML requests redirect to `/login` before handlers
  run. Protected API/JSON requests return HTTP 401 JSON. Settings, cookies,
  system metrics and account mutation routes use the same authentication guard.
- File manager endpoints enforce the enabled and read-only settings. New
  directory and rename names must be a single filename; the final destination
  must also stay within allowed roots, including when a destination is a symlink.
- Account mutation requires the existing password and emits no password,
  username or request body in diagnostics. Successful changes clear the session.

## Build and verify

`Dockerfile.patch` uses the immutable AMD64 CA-patched v1.2.9 image. It replaces
only six application files, runs the regression suite without networking and
checks the existing CA policy. It does not reinstall Python or OS packages.

```sh
docker buildx build --platform linux/amd64 --file Dockerfile.patch \
  --build-arg SOURCE_REVISION=reviewed-source-commit \
  --load --tag recordweb:v1.2.9-patch-20261025 .
```

The application regression tests use synthetic configuration and credentials,
temporary directories and an in-memory FastAPI app. They do not import the
application bootstrap, call external platforms or require operating data.
`test-app-patch.yml` builds and tests this candidate on pull requests and never
publishes a release image.

The existing general `Dockerfile` and publishing workflows are separate build
paths: they resolve dependencies again and must not be treated as equivalent to
this pinned, CA-preserving patch build.

## Deployment boundary

Back up the current stack definition and complete environment, application
configuration, image and any source overrides before replacing a container.
Check that bind-mounted source files do not mask the image's fixes. Verify
authentication, writable file operations, storage reporting, TLS policy and
recording compatibility after the replacement. Retain a matching image,
configuration and override set for rollback.

CIME recording code and dependency advisory updates are outside this application
patch. Homepage TLS checks alone cannot establish full platform recording or
cookie compatibility. This patch does not claim to remediate all image CVEs.
