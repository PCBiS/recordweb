#!/usr/bin/env python3
"""Remove two distrusted public CA roots without changing unrelated trust.

``apply`` filters the installed Debian CA sources, system store and certifi,
then regenerates the system store with the installed update-ca-certificates.
It verifies exact before-minus-target certificate sets in files and fresh
Python TLS contexts. ``verify`` checks an already patched image without writes.
No network requests, package updates or application imports are performed.
"""

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys


TARGETS = frozenset({
    # emSign ECC Root CA - G3
    "86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B",
    # AAA Certificate Services (Comodo AAA Services root)
    "D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4",
})
CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s*(.*?)\s*-----END CERTIFICATE-----", re.S
)
SOURCE_DIRECTORY = Path("/usr/share/ca-certificates")
CONFIGURATION = Path("/etc/ca-certificates.conf")
SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
CA_DIRECTORIES = (
    Path("/etc/ssl/certs"),
    SOURCE_DIRECTORY,
    Path("/usr/local/share/ca-certificates"),
)
ENVIRONMENT_OVERRIDES = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)


class PolicyError(RuntimeError):
    """A trust-store assumption or exact-set verification failed."""


def fingerprint(match):
    """Hash decoded DER bytes; certificate names and PEM whitespace are irrelevant."""
    try:
        der = base64.b64decode(re.sub(rb"\s+", b"", match.group(1)), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PolicyError("malformed certificate PEM") from exc
    if not der:
        raise PolicyError("empty certificate PEM")
    return hashlib.sha256(der).hexdigest().upper()


def certificate_hashes(payload):
    return frozenset(fingerprint(match) for match in CERTIFICATE.finditer(payload))


def filter_certificates(payload, targets=None):
    """Remove exact target certificates, retaining all other bytes verbatim."""
    targets = TARGETS if targets is None else frozenset(targets)
    return CERTIFICATE.sub(
        lambda match: b"" if fingerprint(match) in targets else match.group(0), payload
    )


def disable_sources(payload, sources):
    """Disable exact active Debian source entries; preserve comments and formatting."""
    sources = {source.encode("utf-8") for source in sources}
    result = []
    for line in payload.splitlines(keepends=True):
        if line.strip() in sources:
            indent = len(line) - len(line.lstrip(b" \t"))
            line = line[:indent] + b"!" + line[indent:]
        result.append(line)
    return b"".join(result)


def check_environment():
    if any(os.environ.get(name) for name in ENVIRONMENT_OVERRIDES):
        raise PolicyError("custom CA environment overrides require separate verification")


def trust_paths():
    import certifi

    defaults = ssl.get_default_verify_paths()
    certifi_path = Path(certifi.where())
    directories = set(CA_DIRECTORIES)
    if defaults.capath:
        directories.add(Path(defaults.capath))
    files = {SYSTEM_BUNDLE, certifi_path}
    if defaults.cafile:
        files.add(Path(defaults.cafile))
    return certifi_path, directories, files


def snapshot(directories, explicit_files):
    """Read public CA PEMs and active OpenSSL hash aliases, following file links."""
    paths = set(explicit_files)
    for directory in directories:
        if not directory.is_dir():
            continue
        for current, children, files in os.walk(directory, followlinks=False):
            children[:] = [name for name in children if name not in {"private", "keys"}]
            for name in files:
                if re.search(r"\.(?:pem|crt|[0-9]+)$", name, re.I):
                    paths.add(Path(current) / name)
    result = {}
    for path in sorted(paths):
        if not path.is_file():
            continue
        if path.stat().st_size > 4 * 1024 * 1024:
            raise PolicyError("unexpectedly large CA file")
        payload = path.read_bytes()
        if b"PRIVATE KEY-----" in payload:
            raise PolicyError("private key found in public CA paths")
        roots = certificate_hashes(payload)
        if roots:
            result[str(path)] = {
                "realpath": str(path.resolve(strict=True)),
                "symlink": path.is_symlink(),
                "roots": roots,
            }
    return result


def assert_exact_delta(before, after):
    known = set().union(*(item["roots"] for item in before.values())) - TARGETS
    for path, original in before.items():
        expected = original["roots"] - TARGETS
        actual = after.get(path, {}).get("roots", frozenset())
        if actual != expected:
            raise PolicyError("unexpected certificate-set change in " + path)
    for path, item in after.items():
        if item["roots"] & TARGETS:
            raise PolicyError("distrusted certificate remains in " + path)
        if path not in before and item["roots"] - known:
            raise PolicyError("new trust anchor appeared during regeneration")


def patch_files(before):
    disabled_sources = set()
    # Capture aliases and source names while their original targets still exist.
    aliases = [Path(path) for path, item in before.items()
               if item["symlink"] and item["roots"] <= TARGETS]
    for path, item in before.items():
        source = Path(path)
        if (source.is_relative_to(SOURCE_DIRECTORY)
                and item["roots"] <= TARGETS):
            disabled_sources.add(str(source.relative_to(SOURCE_DIRECTORY)))
    changed = 0
    visited = set()
    for item in before.values():
        if not item["roots"] & TARGETS or item["realpath"] in visited:
            continue
        visited.add(item["realpath"])
        path = Path(item["realpath"])
        original = path.read_bytes()
        filtered = filter_certificates(original)
        if certificate_hashes(filtered) != certificate_hashes(original) - TARGETS:
            raise PolicyError("certificate filtering changed unrelated trust")
        if certificate_hashes(filtered):
            mode = stat.S_IMODE(path.stat().st_mode)
            path.write_bytes(filtered)
            path.chmod(mode)
        else:
            # Removing target-only source files also prevents future capath rehash.
            path.unlink()
        changed += 1
    for alias in aliases:
        if alias.is_symlink():
            alias.unlink()
    original = CONFIGURATION.read_bytes()
    filtered = disable_sources(original, disabled_sources)
    if original != filtered:
        CONFIGURATION.write_bytes(filtered)
    return {"changed_CA_files": changed, "disabled_source_entries": len(disabled_sources)}


# Every runtime observation runs in a fresh interpreter, so imports cannot keep
# the before-apply CA context cached after the files have been patched.
RUNTIME_PROBE = r'''
import hashlib, json, os, ssl
from types import SimpleNamespace
import certifi, httpx._config, requests, requests.adapters

def hashes(context):
    return sorted({hashlib.sha256(der).hexdigest().upper()
                   for der in context.get_ca_certs(binary_form=True)})

certifi_path = os.path.realpath(certifi.where())
if os.path.realpath(requests.certs.where()) != certifi_path:
    raise RuntimeError("Requests certifi selection changed")
if os.path.realpath(requests.adapters.DEFAULT_CA_BUNDLE_PATH) != certifi_path:
    raise RuntimeError("Requests default CA bundle changed")
connection = SimpleNamespace()
requests.adapters.HTTPAdapter().cert_verify(
    connection, "https://example.invalid/", verify=True, cert=None)
if connection.cert_reqs != "CERT_REQUIRED":
    raise RuntimeError("Requests certificate verification disabled")
requests_path = getattr(connection, "ca_certs", None)
if not requests_path or os.path.realpath(requests_path) != certifi_path:
    raise RuntimeError("Requests adapter did not select certifi")
profiles = {
    "system": hashes(ssl.create_default_context()),
    "certifi": hashes(ssl.create_default_context(cafile=certifi.where())),
    "requests": hashes(ssl.create_default_context(cafile=requests_path)),
    "httpx": hashes(httpx._config.create_ssl_context()),
}
cached = getattr(requests.adapters, "_preloaded_ssl_context", None)
if cached is not None:
    profiles["requests_preloaded"] = hashes(cached)
print(json.dumps(profiles))
'''


def runtime_snapshot():
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", RUNTIME_PROBE],
        capture_output=True, timeout=60, check=False,
    )
    if result.returncode:
        raise PolicyError("fresh Python TLS consumer verification failed")
    try:
        profiles = json.loads(result.stdout)
        return {name: frozenset(roots) for name, roots in profiles.items()}
    except (ValueError, TypeError, AttributeError) as exc:
        raise PolicyError("invalid TLS consumer verification result") from exc


def verify_profiles(files, certifi_path, profiles):
    try:
        system = files[str(SYSTEM_BUNDLE)]["roots"]
        certifi_roots = files[str(certifi_path)]["roots"]
    except KeyError as exc:
        raise PolicyError("required system or certifi bundle missing") from exc
    if not system or not certifi_roots:
        raise PolicyError("required CA store is empty")
    if not {"system", "certifi", "requests", "httpx"} <= set(profiles):
        raise PolicyError("required TLS consumer profile missing")
    for name, roots in profiles.items():
        expected = system if name == "system" else certifi_roots
        if roots != expected:
            raise PolicyError("TLS consumer trust selection mismatch: " + name)


def assert_targets_absent(files):
    if any(item["roots"] & TARGETS for item in files.values()):
        raise PolicyError("distrusted CA remains in a public store or active capath")


def apply():
    check_environment()
    certifi_path, directories, explicit_files = trust_paths()
    before = snapshot(directories, explicit_files)
    runtime_before = runtime_snapshot()
    verify_profiles(before, certifi_path, runtime_before)
    # This image has no CA update hooks. Refuse new hook execution during an
    # otherwise offline, CA-only operation rather than silently broadening it.
    hooks = Path("/etc/ca-certificates/update.d")
    if hooks.is_dir() and any(path.is_file() and os.access(path, os.X_OK)
                             for path in hooks.iterdir()):
        raise PolicyError("CA regeneration hooks require separate review")
    changes = patch_files(before)
    result = subprocess.run(
        ["/usr/sbin/update-ca-certificates", "--fresh"],
        capture_output=True, timeout=120, check=False,
    )
    if result.returncode:
        raise PolicyError("native CA regeneration failed")
    after = snapshot(directories, explicit_files)
    assert_exact_delta(before, after)
    assert_targets_absent(after)
    runtime_after = runtime_snapshot()
    verify_profiles(after, certifi_path, runtime_after)
    if runtime_before.keys() != runtime_after.keys():
        raise PolicyError("TLS consumer profiles changed")
    for name in runtime_before:
        if runtime_after[name] != runtime_before[name] - TARGETS:
            raise PolicyError("unexpected runtime trust-set change: " + name)
    return {
        "action": "apply", "passed": True, "network_requests": 0,
        "exact_certificate_set_delta": True, "active_capath_checked": True,
        "CA_files_checked": len(after), **changes,
        "before_counts": {key: len(value) for key, value in runtime_before.items()},
        "after_counts": {key: len(value) for key, value in runtime_after.items()},
        "target_certificates_remaining": 0,
    }


def verify():
    check_environment()
    certifi_path, directories, explicit_files = trust_paths()
    files = snapshot(directories, explicit_files)
    assert_targets_absent(files)
    profiles = runtime_snapshot()
    verify_profiles(files, certifi_path, profiles)
    return {
        "action": "verify", "passed": True, "network_requests": 0,
        "active_capath_checked": True, "CA_files_checked": len(files),
        "counts": {key: len(value) for key, value in profiles.items()},
        "target_certificates_remaining": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "verify"))
    arguments = parser.parse_args()
    try:
        result = apply() if arguments.action == "apply" else verify()
    except (PolicyError, OSError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, PolicyError) else type(exc).__name__
        print(json.dumps({"action": arguments.action, "passed": False,
                          "error": message}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
