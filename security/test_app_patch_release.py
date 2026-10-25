"""Offline release-audit tests; no Docker daemon or RecordWEB imports required."""

import copy
import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import verify_app_patch as audit


REVISION = "0123456789abcdef0123456789abcdef01234567"
PAYLOAD = b"# reviewed application source\n"
APP_PATH = "app/recordWEB.py"
EXPECTED = {APP_PATH: audit.sha256(PAYLOAD)}


def member(name, payload=b"", *, kind=tarfile.REGTYPE, linkname="", mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.type = kind
    entry.mode = mode
    entry.uid = entry.gid = 0
    entry.linkname = linkname
    entry.size = len(payload) if kind == tarfile.REGTYPE else 0
    return entry, payload


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for entry, payload in entries:
            archive.addfile(entry, io.BytesIO(payload) if entry.isfile() else None)
    return stream.getvalue()


def image_configs():
    original = {
        "architecture": "amd64", "os": "linux", "created": "2026-10-23",
        "config": {
            "WorkingDir": "/app", "User": "", "Env": ["PATH=/usr/bin"],
            "Entrypoint": ["python3"], "Cmd": ["recordWEB.py"],
            "Healthcheck": {"Test": ["CMD", "python3", "health.py"]},
            "Labels": {"unchanged-label": "preserved"},
        },
        "rootfs": {"type": "layers", "diff_ids": ["base" + str(i) for i in range(12)]},
        "history": [{"created_by": "base" + str(i)} for i in range(12)],
    }
    candidate = copy.deepcopy(original)
    candidate["created"] = "2026-10-26"
    candidate["config"]["Labels"].update(audit.LABELS)
    candidate["config"]["Labels"]["org.opencontainers.image.revision"] = REVISION
    candidate["rootfs"]["diff_ids"].extend("app" + str(i) for i in range(6))
    candidate["history"].extend({"created_by": "COPY " + str(i)} for i in range(6))
    candidate["history"].append({"created_by": "LABEL", "empty_layer": True})
    return original, candidate


class ApplicationLayerTests(unittest.TestCase):
    def audit_layer(self, entries, seen=None):
        return audit.audit_app_layer(io.BytesIO(tar_bytes(entries)), EXPECTED,
                                     set() if seen is None else seen)

    def test_reviewed_payload_and_structural_directories_are_accepted(self):
        seen = set()
        result = self.audit_layer([
            member("./", kind=tarfile.DIRTYPE, mode=0o755),
            member("app/", kind=tarfile.DIRTYPE, mode=0o755),
            member(APP_PATH, PAYLOAD),
        ], seen)
        self.assertEqual(result, EXPECTED)
        self.assertEqual(seen, set(EXPECTED))

    def test_source_mismatch_is_rejected(self):
        with self.assertRaisesRegex(audit.VerificationError, "differs from checkout"):
            self.audit_layer([member(APP_PATH, PAYLOAD + b"unreviewed change")])

    def test_unreviewed_files_and_directories_are_rejected(self):
        for entry in [member("app/secret.env", b"synthetic fixture"),
                      member("etc/ssl/certs", kind=tarfile.DIRTYPE),
                      member("app/.wh.recordWEB.py"),
                      member("app/.wh..wh..opq")]:
            with self.subTest(path=entry[0].name):
                with self.assertRaises(audit.VerificationError):
                    self.audit_layer([entry, member(APP_PATH, PAYLOAD)])

    def test_path_traversal_and_absolute_names_are_rejected(self):
        for name in ("../app/recordWEB.py", "app/../app/recordWEB.py", "/app/recordWEB.py"):
            with self.subTest(path=name):
                with self.assertRaisesRegex(audit.VerificationError, "Unsafe layer path"):
                    self.audit_layer([member(name, PAYLOAD)])

    def test_links_and_special_files_cannot_replace_reviewed_source(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
                     tarfile.CHRTYPE, tarfile.BLKTYPE):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(audit.VerificationError, "regular non-privileged"):
                    self.audit_layer([member(APP_PATH, kind=kind, linkname="/etc/passwd")])

    def test_duplicate_members_and_writes_across_layers_are_rejected(self):
        with self.assertRaisesRegex(audit.VerificationError, "Duplicate added layer member"):
            self.audit_layer([member(APP_PATH, PAYLOAD), member("./" + APP_PATH, PAYLOAD)])
        with self.assertRaisesRegex(audit.VerificationError, "written more than once"):
            self.audit_layer([member(APP_PATH, PAYLOAD)], {APP_PATH})

    def test_privileged_mode_and_nonroot_file_ownership_are_rejected(self):
        cases = [member(APP_PATH, PAYLOAD, mode=0o4755),
                 member("app", kind=tarfile.DIRTYPE, mode=0o2755)]
        entry = member(APP_PATH, PAYLOAD)
        entry[0].uid = 1000
        cases.append(entry)
        entry = member(APP_PATH, PAYLOAD)
        entry[0].gid = 1000
        cases.append(entry)
        for entry in cases:
            with self.subTest(mode=entry[0].mode, uid=entry[0].uid, gid=entry[0].gid):
                with self.assertRaises(audit.VerificationError):
                    self.audit_layer([entry])

    def test_empty_filesystem_layer_is_rejected(self):
        with self.assertRaisesRegex(audit.VerificationError, "no reviewed application file"):
            self.audit_layer([member("app", kind=tarfile.DIRTYPE, mode=0o755)])

    def test_reviewed_source_permissions_cannot_be_changed(self):
        for mode in (0o000, 0o600, 0o666, 0o755):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(audit.VerificationError, "mode must be 0644"):
                    self.audit_layer([member(APP_PATH, PAYLOAD, mode=mode)])

    def test_ancestor_directory_permissions_and_ownership_are_preserved(self):
        cases = [member("app", kind=tarfile.DIRTYPE, mode=0o777)]
        entry = member("app", kind=tarfile.DIRTYPE, mode=0o755)
        entry[0].uid = 1000
        cases.append(entry)
        entry = member("app", kind=tarfile.DIRTYPE, mode=0o755)
        entry[0].gid = 1000
        cases.append(entry)
        for entry in cases:
            with self.subTest(mode=entry[0].mode, uid=entry[0].uid, gid=entry[0].gid):
                with self.assertRaisesRegex(audit.VerificationError, "Structural directory"):
                    self.audit_layer([entry, member(APP_PATH, PAYLOAD)])

    def test_pax_attributes_cannot_smuggle_capabilities_or_acls(self):
        for key in ("SCHILY.xattr.security.capability", "SCHILY.acl.access"):
            with self.subTest(attribute=key):
                entry = member(APP_PATH, PAYLOAD)
                entry[0].pax_headers = {key: "synthetic fixture"}
                with self.assertRaisesRegex(audit.VerificationError, "Extended permissions"):
                    self.audit_layer([entry])


class RuntimeMetadataTests(unittest.TestCase):
    def test_only_expected_release_labels_and_copy_history_may_change(self):
        original, candidate = image_configs()
        result = audit.validate_config(original, candidate, REVISION)
        self.assertEqual(result["inherited_diffids"], 12)
        self.assertEqual(result["added_diffids"], 6)
        self.assertTrue(result["native_docker_healthcheck_preserved"])

    def test_runtime_configuration_changes_are_rejected(self):
        for key, value in {
            "WorkingDir": "/elsewhere", "User": "1000", "Env": ["TLS_VERIFY=0"],
            "Entrypoint": ["sh"], "Cmd": ["unreviewed.py"],
            "Healthcheck": {"Test": ["NONE"]}, "ExposedPorts": {"1234/tcp": {}},
        }.items():
            with self.subTest(key=key):
                original, candidate = image_configs()
                candidate["config"][key] = value
                with self.assertRaisesRegex(audit.VerificationError, "Runtime Config changed"):
                    audit.validate_config(original, candidate, REVISION)

    def test_platform_labels_history_and_rootfs_substitution_are_rejected(self):
        def replace_architecture(candidate):
            candidate["architecture"] = "arm64"

        def replace_labels(candidate):
            candidate["config"]["Labels"]["unchanged-label"] = "changed"

        def replace_revision(candidate):
            candidate["config"]["Labels"]["org.opencontainers.image.revision"] = "f" * 40

        def replace_rootfs(candidate):
            candidate["rootfs"]["diff_ids"][0] = "replaced"

        def append_layer(candidate):
            candidate["rootfs"]["diff_ids"].append("extra")

        def replace_history(candidate):
            candidate["history"][0]["created_by"] = "changed"

        def hide_copy_history(candidate):
            candidate["history"][-2]["empty_layer"] = True

        def add_top_level(candidate):
            candidate["variant"] = "unreviewed"

        for mutate in (replace_architecture, replace_labels, replace_revision,
                       replace_rootfs, append_layer, replace_history,
                       hide_copy_history, add_top_level):
            with self.subTest(mutation=mutate.__name__):
                original, candidate = image_configs()
                mutate(candidate)
                with self.assertRaises(audit.VerificationError):
                    audit.validate_config(original, candidate, REVISION)


class SavedDockerArchiveTests(unittest.TestCase):
    def fixture(self, *, corrupt_layer=False, bad_config_id=False, duplicate=False,
                compressed_layer=False):
        layer = tar_bytes([member(APP_PATH, PAYLOAD)])
        config = {"rootfs": {"type": "layers", "diff_ids": [audit.sha256(layer)]}}
        config_bytes = json.dumps(config).encode()
        inspected = {"Id": audit.sha256(config_bytes), "RootFS": {"Layers": config["rootfs"]["diff_ids"]}}
        if bad_config_id:
            inspected["Id"] = "sha256:" + "0" * 64
        record = {"Config": "blobs/sha256/config", "RepoTags": ["fixture:test"],
                  "Layers": ["blobs/sha256/layer"]}
        layer_blob = gzip.compress(layer, mtime=0) if compressed_layer else layer
        entries = [member("manifest.json", json.dumps([record]).encode()),
                   member(record["Config"], config_bytes),
                   member(record["Layers"][0], layer_blob + b"tamper" if corrupt_layer else layer_blob)]
        if duplicate:
            entries.append(member("manifest.json", b"[]"))
        return tar_bytes(entries), inspected

    def save_fixture(self, payload, inspected, output):
        def fake_docker(*arguments):
            self.assertEqual(arguments[:3], ("image", "save", "--output"))
            Path(arguments[3]).write_bytes(payload)

        with patch.object(audit, "docker", side_effect=fake_docker):
            return audit.saved_image("fixture:test", inspected, output)

    def test_valid_docker_save_layout_is_accepted_without_extraction(self):
        payload, inspected = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, config, record = self.save_fixture(payload, inspected, root / "saved.tar")
            with archive:
                self.assertEqual(config["rootfs"]["diff_ids"], inspected["RootFS"]["Layers"])
                self.assertEqual(record["Layers"], ["blobs/sha256/layer"])
            self.assertEqual(list(root.iterdir()), [root / "saved.tar"])

    def test_config_digest_layer_diffid_and_duplicate_members_are_checked(self):
        for option in ("corrupt_layer", "bad_config_id", "duplicate"):
            with self.subTest(option=option):
                payload, inspected = self.fixture(**{option: True})
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(audit.VerificationError):
                        self.save_fixture(payload, inspected, Path(directory) / "saved.tar")

    def test_containerd_gzip_blob_is_hashed_as_uncompressed_diffid(self):
        payload, inspected = self.fixture(compressed_layer=True)
        with tempfile.TemporaryDirectory() as directory:
            archive, config, _ = self.save_fixture(payload, inspected, Path(directory) / "saved.tar")
            archive.close()
            self.assertEqual(config["rootfs"]["diff_ids"], inspected["RootFS"]["Layers"])


if __name__ == "__main__":
    unittest.main()
