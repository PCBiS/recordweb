"""Offline tests for exact DER filtering and Debian CA source configuration."""

import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ca_policy


def pem(der, *, multiline=False):
    encoded = base64.b64encode(der)
    if multiline:
        encoded = b"\n".join(encoded[index:index + 8]
                             for index in range(0, len(encoded), 8))
    return b"-----BEGIN CERTIFICATE-----\n" + encoded + b"\n-----END CERTIFICATE-----"


def sha(der):
    return hashlib.sha256(der).hexdigest().upper()


class CertificateFilteringTests(unittest.TestCase):
    def test_exact_der_target_removal_preserves_other_bytes_and_certificates(self):
        bad = b"target DER fixture"
        similar = b"target DER fixture, different key"
        good = b"unrelated DER fixture"
        payload = (b"# Keep explanatory comments\n" + pem(good) + b"\n\n"
                   + pem(bad, multiline=True) + b"\n# Keep this too\n" + pem(similar))
        result = ca_policy.filter_certificates(payload, {sha(bad)})
        self.assertEqual(result, payload.replace(pem(bad, multiline=True), b""))
        self.assertEqual(ca_policy.certificate_hashes(result), {sha(good), sha(similar)})

    def test_target_encoding_whitespace_and_duplicates_do_not_bypass_filter(self):
        bad = b"one public certificate in two PEM encodings"
        good = b"keep me"
        payload = pem(bad) + b"\n" + pem(good) + b"\n" + pem(bad, multiline=True)
        once = ca_policy.filter_certificates(payload, {sha(bad)})
        twice = ca_policy.filter_certificates(once, {sha(bad)})
        self.assertEqual(once, twice)
        self.assertEqual(ca_policy.certificate_hashes(once), {sha(good)})

    def test_malformed_base64_fails_closed(self):
        with self.assertRaises(ca_policy.PolicyError):
            ca_policy.filter_certificates(
                b"-----BEGIN CERTIFICATE-----\n!bad!\n-----END CERTIFICATE-----")

    def test_delta_detects_unrelated_root_loss_and_new_roots(self):
        before = {"bundle": {"roots": frozenset({"keep", "drop"})}}
        with patch.object(ca_policy, "TARGETS", frozenset({"drop"})):
            ca_policy.assert_exact_delta(before, {"bundle": {"roots": frozenset({"keep"})}})
            with self.assertRaises(ca_policy.PolicyError):
                ca_policy.assert_exact_delta(before, {})
            with self.assertRaises(ca_policy.PolicyError):
                ca_policy.assert_exact_delta(before, {
                    "bundle": {"roots": frozenset({"keep"})},
                    "new-alias": {"roots": frozenset({"new"})},
                })


class DebianConfigurationTests(unittest.TestCase):
    def test_only_exact_enabled_sources_are_disabled(self):
        source = "mozilla/target.crt"
        original = (b"# mozilla/target.crt\nmozilla/target.crt\n"
                    b"!mozilla/target.crt\nmozilla/target.crt.backup\n"
                    b"mozilla/unrelated.crt\n  mozilla/target.crt\r\n")
        expected = (b"# mozilla/target.crt\n!mozilla/target.crt\n"
                    b"!mozilla/target.crt\nmozilla/target.crt.backup\n"
                    b"mozilla/unrelated.crt\n  !mozilla/target.crt\r\n")
        actual = ca_policy.disable_sources(original, {source})
        self.assertEqual(actual, expected)
        self.assertEqual(ca_policy.disable_sources(actual, {source}), expected)

    def test_patch_removes_target_source_and_capath_alias_without_disabling_mixed_bundle(self):
        bad, good = b"bad fixture", b"good fixture"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "share"
            sources.mkdir()
            target = sources / "target.crt"
            target.write_bytes(pem(bad))
            mixed = sources / "mixed.crt"
            mixed.write_bytes(pem(bad) + b"\n" + pem(good))
            mixed.chmod(0o644)
            capath = root / "certs"
            capath.mkdir()
            alias = capath / "01234567.0"
            alias.symlink_to(target)
            configuration = root / "ca-certificates.conf"
            configuration.write_bytes(b"target.crt\nmixed.crt\n")
            before = ca_policy.snapshot({sources, capath}, set())
            with patch.object(ca_policy, "TARGETS", frozenset({sha(bad)})), \
                    patch.object(ca_policy, "SOURCE_DIRECTORY", sources), \
                    patch.object(ca_policy, "CONFIGURATION", configuration):
                ca_policy.patch_files(before)
                self.assertFalse(target.exists())
                self.assertFalse(alias.is_symlink())
                self.assertEqual(mixed.read_bytes(), b"\n" + pem(good))
                self.assertEqual(mixed.stat().st_mode & 0o777, 0o644)
                self.assertEqual(configuration.read_bytes(), b"!target.crt\nmixed.crt\n")
                # A second application is harmless even after the target source is gone.
                ca_policy.patch_files(ca_policy.snapshot({sources, capath}, set()))
                self.assertEqual(configuration.read_bytes(), b"!target.crt\nmixed.crt\n")

    def test_snapshot_includes_hash_aliases_for_active_capath_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            capath = Path(directory)
            target = capath / "target.crt"
            target.write_bytes(pem(b"bad"))
            (capath / "01234567.0").symlink_to(target)
            files = ca_policy.snapshot({capath}, set())
            self.assertEqual(len(files), 2)
            with patch.object(ca_policy, "TARGETS", frozenset({sha(b"bad")})):
                with self.assertRaises(ca_policy.PolicyError):
                    ca_policy.assert_targets_absent(files)


if __name__ == "__main__":
    unittest.main()
