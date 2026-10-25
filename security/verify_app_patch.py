#!/usr/bin/env python3
"""Audit a pulled RecordWEB application patch without starting the application.

Only the six reviewed application files may change above the pinned CA image.
Docker archives are read in place, never extracted. Runtime probes use no
network, no host data, no application entrypoint, and no Docker healthcheck.
The healthcheck remains unchanged in the published image metadata.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile


BASE_IMAGE = "ghcr.io/pcbis/recordweb@sha256:25253c35e1e07dfa1234abbb01c173168d61c3f13506f7d099a5d16e0c479f0b"
VERSION = "1.2.9-patch-20261026"
APP_FILES = (
    "recordWEB.py", "module/file_manager.py", "module/data_manager.py",
    "module/storage_metrics.py", "templates/config.html",
    "templates/static/js/script.js",
)
LABELS = {
    "org.opencontainers.image.version": VERSION,
    "org.opencontainers.image.description": "recordWEB v1.2.9 directory metrics and application security fixes",
}


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def docker(*arguments, stdin=None, timeout=600):
    result = subprocess.run(["docker", *arguments], input=stdin, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    require(result.returncode == 0,
            "Docker operation failed: " + arguments[0] + ": " + result.stderr[-2000:])
    return result.stdout


def inspect(reference):
    documents = json.loads(docker("image", "inspect", reference))
    require(len(documents) == 1, "Expected exactly one inspected image")
    return documents[0]


def changed_keys(before, after):
    return sorted(key for key in before.keys() | after.keys()
                  if (key in before) != (key in after) or before.get(key) != after.get(key))


def validate_config(base, candidate, revision):
    for label, config in (("base", base), ("candidate", candidate)):
        require(config.get("os") == "linux" and config.get("architecture") == "amd64",
                label + " must be linux/amd64")
        require(config.get("rootfs", {}).get("type") == "layers", label + " has invalid rootfs")
    excluded = {"config", "created", "history", "rootfs"}
    require(not changed_keys({k: v for k, v in base.items() if k not in excluded},
                             {k: v for k, v in candidate.items() if k not in excluded}),
            "Unexpected top-level image config change")
    before = {k: v for k, v in base["config"].items() if k != "Labels"}
    after = {k: v for k, v in candidate["config"].items() if k != "Labels"}
    require(not changed_keys(before, after),
            "Runtime Config changed outside reviewed labels: " + str(changed_keys(before, after)))
    require(before.get("WorkingDir") == "/app", "Pinned image WorkingDir is not /app")
    expected_labels = dict(base["config"].get("Labels") or {})
    expected_labels.update(LABELS)
    expected_labels["org.opencontainers.image.revision"] = revision
    require(candidate["config"].get("Labels") == expected_labels,
            "Image labels differ from the base plus the three release labels")
    original = base["rootfs"]["diff_ids"]
    patched = candidate["rootfs"]["diff_ids"]
    require(len(original) == 12, "Pinned CA base must contain exactly 12 layers")
    require(patched[:len(original)] == original and len(patched) == len(original) + 6,
            "Candidate must inherit every CA base layer and add exactly six COPY layers")
    original_history = base.get("history", [])
    history = candidate.get("history", [])
    require(history[:len(original_history)] == original_history, "Inherited image history changed")
    require(sum(not item.get("empty_layer", False) for item in history[len(original_history):]) == 6,
            "Release history must contain exactly six added filesystem changes")
    return {
        "platform": "linux/amd64", "inherited_diffids": len(original),
        "added_diffids": 6, "runtime_config_preserved_except_three_labels": True,
        "native_docker_healthcheck_preserved": True, "base_history_preserved": True,
    }


def layer_path(raw):
    path = PurePosixPath(raw)
    require(not path.is_absolute() and ".." not in path.parts, "Unsafe layer path")
    return "" if str(path) == "." else str(path)


def hash_stream(stream):
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    return "sha256:" + digest.hexdigest()


def hash_layer_diffid(stream):
    # Classic Docker save writes uncompressed layer.tar files. The containerd
    # image store may keep an OCI gzip blob, whose diffID hashes its decoded tar.
    signature = stream.read(2)
    stream.seek(0)
    if signature == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=stream) as decoded:
            return hash_stream(decoded)
    return hash_stream(stream)


def audit_app_layer(stream, expected, already_seen):
    seen = set()
    files = {}
    with tarfile.open(fileobj=stream, mode="r|*") as layer:
        for member in layer:
            name = layer_path(member.name)
            require(name not in seen, "Duplicate added layer member: " + name)
            seen.add(name)
            require(not any("xattr" in key.lower() or "acl" in key.lower()
                            for key in member.pax_headers),
                    "Extended permissions or attributes are not reviewed: " + name)
            if member.isdir():
                require(not name or any(path.startswith(name + "/") for path in expected),
                        "Unreviewed directory in application layer: " + name)
                require(member.mode == 0o755 and member.uid == 0 and member.gid == 0,
                        "Structural directory must retain root-owned mode 0755")
                continue
            require(name in expected, "Unreviewed path in application layer: " + name)
            require(name not in already_seen, "Application file is written more than once: " + name)
            require(member.isfile() and not member.mode & 0o6000,
                    "Application entry must be a regular non-privileged file: " + name)
            require(member.mode == 0o644, "Application source mode must be 0644: " + name)
            require(member.uid == 0 and member.gid == 0, "Unexpected COPY file ownership: " + name)
            require(member.size <= 4 * 1024 * 1024, "Application source file is oversized: " + name)
            payload = layer.extractfile(member)
            require(payload is not None, "Application file has no payload")
            actual = hash_stream(payload)
            require(actual == expected[name], "Application source differs from checkout: " + name)
            files[name] = actual
            already_seen.add(name)
    require(files, "Added filesystem layer contains no reviewed application file")
    return files


def saved_image(reference, inspected, output):
    # Saving by config ID also works when a digest pull creates no RepoTag.
    docker("image", "save", "--output", str(output), inspected["Id"])
    archive = tarfile.open(output, "r:*")
    try:
        entries = archive.getmembers()
        names = [member.name for member in entries]
        require(len(names) == len(set(names)), "Image archive contains duplicate members")
        for member in entries:
            layer_path(member.name)
            require(member.isfile() or member.isdir(), "Image archive contains a link or special member")
        manifest_file = archive.extractfile("manifest.json")
        require(manifest_file is not None, "Image archive has no manifest.json")
        manifest = json.load(manifest_file)
        require(len(manifest) == 1, "Expected exactly one saved image")
        record = manifest[0]
        config_file = archive.extractfile(record["Config"])
        require(config_file is not None, "Image config blob is missing")
        config_data = config_file.read()
        require(sha256(config_data) == inspected["Id"], "Saved config does not match inspected image ID")
        config = json.loads(config_data)
        require(config["rootfs"]["diff_ids"] == inspected["RootFS"]["Layers"],
                "Saved rootfs graph differs from inspected image")
        require(len(record["Layers"]) == len(config["rootfs"]["diff_ids"]),
                "Layer graph length differs from config")
        for path, expected in zip(record["Layers"], config["rootfs"]["diff_ids"]):
            blob = archive.extractfile(path)
            require(blob is not None, "Layer blob is missing")
            require(hash_layer_diffid(blob) == expected, "Layer content diffID does not match graph")
        return archive, config, record
    except BaseException:
        archive.close()
        raise


RUNTIME_PROBE = r"""
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


def roots(context):
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    return sorted(hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True))


def snapshot():
    profiles = {
        "system": roots(ssl.create_default_context()),
        "certifi": roots(ssl.create_default_context(cafile=certifi.where())),
    }
    with requests.Session() as session:
        verify = session.merge_environment_settings("https://recordweb.invalid/", {}, None, None, None)["verify"]
        assert verify is not False
        path = requests.adapters.DEFAULT_CA_BUNDLE_PATH if verify is True else verify
        profiles["requests"] = roots(ssl.create_default_context(
            capath=path if os.path.isdir(path) else None,
            cafile=None if os.path.isdir(path) else path))
    with httpx.Client() as client:
        profiles["httpx"] = roots(client._transport._pool._ssl_context)
    entries = {}
    paths = [Path("/etc/ca-certificates.conf"), Path(certifi.where())]
    for tree in (Path("/etc/ssl/certs"), Path("/usr/share/ca-certificates")):
        paths.extend(sorted(tree.rglob("*")))
    for path in paths:
        if path.is_symlink():
            entries[str(path)] = {"link": os.readlink(path)}
        elif path.is_file():
            entries[str(path)] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"profiles": profiles, "ca_files_sha256": hashlib.sha256(
        json.dumps(entries, sort_keys=True).encode()).hexdigest()}


before = snapshot()
if sys.argv[1] == "refresh":
    for flags in (["--fresh"], []):
        result = subprocess.run(["update-ca-certificates", *flags], capture_output=True, timeout=120)
        assert result.returncode == 0, "Native CA refresh failed"
        assert snapshot() == before, "Native CA refresh changed trust or CA files"
print(json.dumps(before, sort_keys=True))
"""


def probe(reference, refresh):
    return json.loads(docker("run", "--rm", "--interactive", "--network", "none",
        "--no-healthcheck", "--entrypoint", "python3", reference,
        "-I", "-B", "-", "refresh" if refresh else "baseline", stdin=RUNTIME_PROBE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE_IMAGE)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    require(args.base == BASE_IMAGE, "Only the pinned CA base may be used")
    require(len(args.revision) == 40 and all(c in "0123456789abcdef" for c in args.revision),
            "Revision must be the complete lowercase Git commit SHA")
    expected = {"app/" + name: sha256((args.source / name).read_bytes()) for name in APP_FILES}
    base_inspect, candidate_inspect = inspect(args.base), inspect(args.candidate)
    with tempfile.TemporaryDirectory(prefix="recordweb-app-audit-") as temporary:
        root = Path(temporary)
        base_archive, base, _ = saved_image(args.base, base_inspect, root / "base.tar")
        base_archive.close()
        archive, candidate, record = saved_image(args.candidate, candidate_inspect, root / "candidate.tar")
        with archive:
            metadata = validate_config(base, candidate, args.revision)
            seen = set()
            for layer_path_name in record["Layers"][len(base["rootfs"]["diff_ids"]):]:
                stream = archive.extractfile(layer_path_name)
                require(stream is not None, "Added application layer is absent")
                audit_app_layer(stream, expected, seen)
            require(seen == set(expected), "Not every reviewed application source was copied")
    base_trust = probe(args.base, refresh=False)
    candidate_trust = probe(args.candidate, refresh=True)
    require(base_trust == candidate_trust, "CA files or TLS consumer trust differ from pinned base")
    summary = {
        "verified": True, "base": BASE_IMAGE, "candidate_image_id": candidate_inspect["Id"],
        "source_revision": args.revision, "application_sources": expected,
        "metadata": metadata, "ca_files_unchanged": True,
        "trust_root_counts": {key: len(value) for key, value in base_trust["profiles"].items()},
        "native_ca_refresh_idempotent": True, "application_entrypoint_executed": False,
        "network": "none", "dependencies_unchanged": True,
    }
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    try:
        main()
    except (VerificationError, OSError, ValueError, KeyError, TypeError,
            subprocess.TimeoutExpired, tarfile.TarError) as error:
        print("Application image verification failed: " + str(error), file=sys.stderr)
        sys.exit(1)
