#!/usr/bin/env python3
"""Verify an offline CA-only derivative of the pinned RecordWEB image.

Run on a disposable Docker runner after building recordweb-ca:verify.  This
script never starts RecordWEB's entrypoint, exposes ports, mounts host data,
or contacts application services.  The original Docker healthcheck remains
image metadata; verification containers explicitly disable its execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


BASE_IMAGE = (
    "ghcr.io/pcbis/recordweb@sha256:"
    "7953c7491829599227275c6811d2773a2c2936656cdda14efc7fd647f7da61db"
)
BAD_SHA256 = {
    "86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B",
    "D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4",
}
CERTIFI = "usr/local/lib/python3.12/site-packages/certifi/cacert.pem"
ALLOWED_TREES = ("etc/ssl/certs", "usr/share/ca-certificates")
ALLOWED_FILES = {"etc/ca-certificates.conf", CERTIFI}
EXPECTED_BASE_COUNTS = {
    "system": 142,
    "certifi": 118,
    "requests": 118,
    "httpx": 118,
}


class VerificationError(RuntimeError):
    """A required invariant was not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def docker(*args: str, stdin: str | None = None, timeout: int = 300) -> str:
    result = subprocess.run(
        ["docker", *args],
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    require(
        result.returncode == 0,
        f"docker {args[0]} failed ({result.returncode}): {result.stderr[-3000:]}",
    )
    return result.stdout


def inspect_image(reference: str) -> dict:
    documents = json.loads(docker("image", "inspect", reference, timeout=60))
    require(len(documents) == 1, "Expected exactly one inspected image")
    return documents[0]


def validate_metadata(base: dict, candidate: dict) -> dict:
    for label, inspected in (("base", base), ("candidate", candidate)):
        require(inspected.get("Os") == "linux", f"{label} image is not Linux")
        require(
            inspected.get("Architecture") == "amd64",
            f"{label} image is not the pinned amd64 architecture",
        )
        require(
            inspected.get("RootFS", {}).get("Type") == "layers",
            f"{label} image has an unexpected root filesystem type",
        )
    base_layers = base["RootFS"]["Layers"]
    candidate_layers = candidate["RootFS"]["Layers"]
    require(len(base_layers) == 11, "Pinned base must have exactly 11 layers")
    require(
        len(candidate_layers) == 12 and candidate_layers[:-1] == base_layers,
        "Candidate must inherit all 11 original diffIDs and add exactly one layer",
    )
    base_config = {k: v for k, v in base["Config"].items() if k != "Labels"}
    candidate_config = {
        k: v for k, v in candidate["Config"].items() if k != "Labels"
    }
    changed = sorted(
        key
        for key in base_config.keys() | candidate_config.keys()
        if base_config.get(key) != candidate_config.get(key)
        or (key in base_config) != (key in candidate_config)
    )
    require(not changed, f"Runtime Config changed outside Labels: {changed}")
    require(
        base_config.get("Healthcheck") == candidate_config.get("Healthcheck"),
        "Original Docker healthcheck metadata was not preserved",
    )
    return {
        "platform": "linux/amd64",
        "inherited_layers": len(base_layers),
        "added_layers": 1,
        "runtime_config_preserved_except_labels": True,
        "healthcheck_preserved": True,
    }


def allowed_path(name: str) -> bool:
    return name in ALLOWED_FILES or any(
        name == tree or name.startswith(tree + "/") for tree in ALLOWED_TREES
    )


def structural_directory(name: str) -> bool:
    if not name:
        return True
    return any(
        target.startswith(name + "/")
        for target in (*ALLOWED_TREES, *ALLOWED_FILES)
    )


def normalize_layer_path(name: str) -> str:
    path = PurePosixPath(name)
    require(not path.is_absolute(), f"Absolute layer member is forbidden: {name}")
    require(".." not in path.parts, f"Layer traversal is forbidden: {name}")
    return "" if str(path) == "." else str(path)


def audit_ca_layer(reference: str, candidate: dict) -> dict:
    """Read the added layer without extracting any archive onto the runner."""
    with tempfile.TemporaryDirectory(prefix="recordweb-ca-audit-") as temporary:
        archive_path = Path(temporary) / "image.tar"
        docker("image", "save", "--output", str(archive_path), reference, timeout=600)
        with tarfile.open(archive_path, "r:*") as archive:
            manifest_file = archive.extractfile("manifest.json")
            require(manifest_file is not None, "Docker archive has no manifest")
            manifest = json.load(manifest_file)
            require(len(manifest) == 1, "Expected one image in Docker archive")
            record = manifest[0]
            require(len(record["Layers"]) == 12, "Saved image must have 12 layers")
            config_file = archive.extractfile(record["Config"])
            require(config_file is not None, "Docker archive has no image config")
            config = json.load(config_file)
            require(
                config["rootfs"]["diff_ids"] == candidate["RootFS"]["Layers"],
                "Saved image diffIDs differ from the inspected candidate",
            )
            layer_file = archive.extractfile(record["Layers"][-1])
            require(layer_file is not None, "Added CA layer is absent")
            seen: set[str] = set()
            changed_paths: list[str] = []
            payload_bytes = 0
            with tarfile.open(fileobj=layer_file, mode="r|*") as layer:
                for member in layer:
                    name = normalize_layer_path(member.name)
                    require(name not in seen, f"Duplicate CA layer member: {name}")
                    seen.add(name)
                    if member.isdir():
                        require(
                            allowed_path(name) or structural_directory(name),
                            f"Non-CA directory in added layer: {name}",
                        )
                        continue
                    require(
                        not member.isdev() and not member.isfifo(),
                        f"Special file in CA layer: {name}",
                    )
                    path = PurePosixPath(name)
                    if path.name.startswith(".wh."):
                        require(
                            path.name != ".wh..wh..opq",
                            f"Opaque directory whiteout is forbidden: {name}",
                        )
                        removed = str(path.with_name(path.name[4:]))
                        require(
                            member.isfile() and member.size == 0 and allowed_path(removed),
                            f"Invalid or non-CA whiteout: {name}",
                        )
                    else:
                        require(allowed_path(name), f"Non-CA file in added layer: {name}")
                        require(
                            member.isfile() or member.issym() or member.islnk(),
                            f"Unsupported CA layer entry: {name}",
                        )
                        if member.issym() or member.islnk():
                            target = (
                                member.linkname.lstrip("/")
                                if member.linkname.startswith("/")
                                else posixpath.join(
                                    "" if member.islnk() else str(path.parent),
                                    member.linkname,
                                )
                            )
                            require(
                                allowed_path(posixpath.normpath(target)),
                                f"CA link escapes permitted CA paths: {name}",
                            )
                        payload_bytes += member.size
                    changed_paths.append(name)
            require(changed_paths, "Added layer contains no CA changes")
            require(payload_bytes < 16 * 1024 * 1024, "CA layer payload exceeds 16 MiB")
    return {
        "changed_entries": len(changed_paths),
        "payload_bytes": payload_bytes,
        "allowed_ca_paths_only": True,
        "application_and_package_paths_unchanged": True,
    }


# Executed as stdin to python3 in a disposable container with no network.  Do
# not import RecordWEB: its imports may start recording or background workers.
RUNTIME_PROBE = r'''
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys

import certifi
import httpx
import requests


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fingerprints(context):
    require(isinstance(context, ssl.SSLContext), "Profile has no SSLContext")
    require(context.verify_mode == ssl.CERT_REQUIRED, "TLS verification is disabled")
    require(context.check_hostname, "TLS hostname verification is disabled")
    return sorted({hashlib.sha256(der).hexdigest().upper()
                   for der in context.get_ca_certs(binary_form=True)})


def profiles():
    result = {
        "system": fingerprints(ssl.create_default_context()),
        "certifi": fingerprints(ssl.create_default_context(cafile=certifi.where())),
    }
    # Resolve the exact Requests trust_env policy without sending a request.
    with requests.Session() as session:
        settings = session.merge_environment_settings(
            "https://recordweb.invalid/", {}, None, None, None)
        verify = settings["verify"]
        require(verify is not False, "Requests certificate verification is disabled")
        prepared = requests.Request("GET", "https://recordweb.invalid/").prepare()
        adapter = session.get_adapter(prepared.url)
        if hasattr(adapter, "build_connection_pool_key_attributes"):
            _, pool_kwargs = adapter.build_connection_pool_key_attributes(
                prepared, verify, None)
            context = pool_kwargs.get("ssl_context")
        else:
            context = None
        if context is None:
            # Older Requests configures urllib3's SSLContext at connect time.
            # Load the same resolved CA file/directory, with verification on.
            path = (requests.adapters.DEFAULT_CA_BUNDLE_PATH
                    if verify is True else verify)
            require(isinstance(path, str), "Unsupported Requests CA configuration")
            context = ssl.create_default_context(
                capath=path if os.path.isdir(path) else None,
                cafile=None if os.path.isdir(path) else path)
        result["requests"] = fingerprints(context)
    # Inspect HTTPX's actual default transport context; constructing it makes
    # no HTTP request and honors its trust_env CA selection.
    with httpx.Client() as client:
        context = client._transport._pool._ssl_context
        result["httpx"] = fingerprints(context)
    return result


def ca_snapshot():
    entries = {}
    paths = [Path("/etc/ca-certificates.conf"), Path(certifi.where())]
    for root in (Path("/etc/ssl/certs"), Path("/usr/share/ca-certificates")):
        paths.extend(sorted(root.rglob("*")))
    for path in paths:
        if path.is_symlink():
            entries[str(path)] = {"link": os.readlink(path)}
        elif path.is_file():
            entries[str(path)] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def smoke(source_root):
    root = Path(source_root)
    require(root.is_absolute() and str(root) != "/" and root.is_dir(),
            "Application WorkingDir must be an existing non-root directory")
    sources = sorted(root.rglob("*.py"))
    require(sources, "No Python application sources found in WorkingDir")
    source_hash = hashlib.sha256()
    for path in sources:
        require(path.resolve().is_relative_to(root.resolve()),
                "Python source symlink escapes application WorkingDir")
        data = path.read_bytes()
        compile(data, str(path), "exec")
        source_hash.update(str(path.relative_to(root)).encode() + b"\0")
        source_hash.update(hashlib.sha256(data).digest())
    ffmpeg = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                            text=True, timeout=30, check=True)
    return {"python_sources_compiled": len(sources),
            "python_sources_sha256": source_hash.hexdigest(),
            "ffmpeg_version": ffmpeg.stdout.splitlines()[0]}


initial = profiles()
output = {"profiles": initial, "smoke": smoke(sys.argv[1])}
if sys.argv[2] == "refresh":
    before = ca_snapshot()
    for flags in (["--fresh"], []):
        refresh = subprocess.run(["update-ca-certificates", *flags],
                                 capture_output=True, text=True, timeout=60)
        require(refresh.returncode == 0, "Native CA refresh failed")
        require(profiles() == initial, "Native refresh changed the exact runtime trust sets")
        require(ca_snapshot() == before, "Native refresh changed CA file contents or links")
    output["native_refresh"] = {"runs": 2, "idempotent": True,
                                "ca_snapshot_sha256": before}
print(json.dumps(output, sort_keys=True))
'''


def probe_image(reference: str, working_dir: str, refresh: bool) -> dict:
    result = docker(
        "run", "--rm", "--interactive", "--network", "none",
        "--no-healthcheck", "--entrypoint", "python3", reference,
        "-", working_dir, "refresh" if refresh else "baseline",
        stdin=RUNTIME_PROBE, timeout=300,
    )
    return json.loads(result)


def compare_profiles(base: dict, candidate: dict) -> dict:
    require(
        set(base["profiles"]) == set(EXPECTED_BASE_COUNTS)
        and set(candidate["profiles"]) == set(EXPECTED_BASE_COUNTS),
        "Expected system, certifi, Requests, and HTTPX trust profiles",
    )
    summary = {}
    for profile, expected_count in EXPECTED_BASE_COUNTS.items():
        original = set(base["profiles"][profile])
        patched = set(candidate["profiles"][profile])
        require(
            len(original) == expected_count,
            f"Pinned base {profile} root count differs from {expected_count}",
        )
        require(original & BAD_SHA256, f"Pinned base {profile} lacks the targeted CA")
        require(
            patched == original - BAD_SHA256,
            f"{profile} trust set differs from the original minus the two denied roots",
        )
        summary[profile] = {
            "base_roots": len(original),
            "candidate_roots": len(patched),
            "removed_sha256": sorted(original - patched),
            "unexpected_added_or_removed": 0,
        }
    require(
        set(base["profiles"]["system"]) & BAD_SHA256 == BAD_SHA256,
        "Pinned base system trust must contain both targeted roots",
    )
    require(base["smoke"] == candidate["smoke"], "Offline application smoke differs")
    require(
        candidate.get("native_refresh", {}).get("idempotent") is True,
        "Native CA refresh idempotence was not verified",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE_IMAGE)
    parser.add_argument("--candidate", default="recordweb-ca:verify")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    require(args.base == BASE_IMAGE, "Verification requires the exact pinned original base")
    base = inspect_image(args.base)
    candidate = inspect_image(args.candidate)
    metadata = validate_metadata(base, candidate)
    layer = audit_ca_layer(args.candidate, candidate)
    working_dir = base["Config"].get("WorkingDir", "")
    original_runtime = probe_image(args.base, working_dir, refresh=False)
    patched_runtime = probe_image(args.candidate, working_dir, refresh=True)
    profile_summary = compare_profiles(original_runtime, patched_runtime)
    summary = {
        "verified": True,
        "base": BASE_IMAGE,
        "candidate_image_id": candidate["Id"],
        "metadata": metadata,
        "added_layer": layer,
        "runtime_trust": profile_summary,
        "native_refresh": patched_runtime["native_refresh"],
        "offline_smoke": patched_runtime["smoke"],
        "application_entrypoint_executed": False,
        "network": "none",
    }
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, subprocess.TimeoutExpired, ValueError,
            KeyError, OSError, tarfile.TarError) as error:
        print(f"CA image verification failed: {error}", file=sys.stderr)
        sys.exit(1)
