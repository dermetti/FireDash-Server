"""Root Linux acceptance tests using only disposable, synthetic credentials.

Run directly with the project's Python. Unsupported platforms fail rather than
skip, so the mandatory CI acceptance step cannot silently lose this coverage.
"""

import builtins
import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "installer_configuration", Path(__file__).resolve().parents[1] / "lib/configuration.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load installer configuration module")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CredentialMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("Native credential metadata acceptance requires Linux root")

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="firedash-credential-metadata-")
        self.addCleanup(scratch.cleanup)
        self.config = Path(scratch.name)
        self.credentials = self.config / "credentials"
        self.credentials.mkdir(mode=0o700)
        for name in c.CREDENTIAL_FILES:
            self.write_placeholder(self.credentials / name)
        self.staging = self.credentials / "publication-signing-key-staging"
        self.staging.mkdir(mode=0o700)
        self.version = self.add_version("2")
        self.nodes = (
            self.credentials,
            self.credentials / "publication-signing-key",
            self.staging,
            self.version,
            self.version / "private",
            self.version / "public",
        )

    def write_placeholder(self, path):
        # These bytes are test data, never cryptographic key material.
        path.write_bytes(b"synthetic-placeholder-not-a-key\n")
        path.chmod(0o600)

    def add_version(self, name):
        version = self.staging / name
        version.mkdir(mode=0o700)
        for name in ("private", "public"):
            self.write_placeholder(version / name)
        return version

    def snapshot(self):
        result = {}
        for path in (self.config, *sorted(self.config.rglob("*"))):
            info = path.lstat()
            # Ignore access time: the test itself reads synthetic placeholders.
            metadata = (
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            target = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
            result[path.relative_to(self.config)] = (metadata, content, target)
        return result

    def validate(self, *, rejected=False):
        before = self.snapshot()
        # Guard every Python file-content opening API during validation. Real
        # lstat/iterdir still run; even the staged private placeholder is unread.
        with (
            patch.object(builtins, "open", side_effect=AssertionError("content read")),
            patch.object(io, "open", side_effect=AssertionError("content read")),
            patch.object(os, "open", side_effect=AssertionError("content read")),
        ):
            if rejected:
                with self.assertRaises(c.ConfigurationError) as error:
                    c.validate_files(self.config)
                self.assertNotIn("synthetic-placeholder", str(error.exception))
            else:
                c.validate_files(self.config)
        self.assertEqual(before, self.snapshot(), "validation changed credential state")

    def test_valid_retained_versions(self):
        self.add_version("1")
        self.add_version("9" * 64)
        for node in self.nodes:
            info = node.lstat()
            self.assertEqual((info.st_uid, info.st_gid), (0, 0))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700 if node.is_dir() else 0o600)
        self.validate()

    def test_without_staging(self):
        for path in self.version.iterdir():
            path.unlink()
        self.version.rmdir()
        self.staging.rmdir()
        self.validate()

    def test_empty_staging(self):
        for path in self.version.iterdir():
            path.unlink()
        self.version.rmdir()
        self.validate()

    def test_symlinks_including_broken(self):
        for node in self.nodes:
            for broken in (False, True):
                with self.subTest(node=node.relative_to(self.config), broken=broken):
                    target = self.config / "saved-target"
                    node.rename(target)
                    node.symlink_to(target if not broken else self.config / "missing")
                    try:
                        self.validate(rejected=True)
                    finally:
                        node.unlink()
                        target.rename(node)

    def test_incorrect_owner_and_group(self):
        for node in self.nodes:
            for uid, gid in ((65534, 0), (0, 65534)):
                with self.subTest(node=node.relative_to(self.config), uid=uid, gid=gid):
                    os.chown(node, uid, gid)
                    try:
                        self.validate(rejected=True)
                    finally:
                        os.chown(node, 0, 0)

    def test_incorrect_permissions(self):
        for node in self.nodes:
            expected = 0o700 if node.is_dir() else 0o600
            for mode in (expected | 0o040, expected | 0o002, expected | stat.S_ISUID):
                with self.subTest(node=node.relative_to(self.config), mode=oct(mode)):
                    node.chmod(mode)
                    try:
                        self.validate(rejected=True)
                    finally:
                        node.chmod(expected)

    def test_incorrect_file_types(self):
        for node in self.nodes:
            for kind in ("fifo", "opposite-type"):
                with self.subTest(node=node.relative_to(self.config), kind=kind):
                    target = self.config / "saved-target"
                    directory = node.is_dir()
                    node.rename(target)
                    if kind == "fifo":
                        os.mkfifo(node, 0o600)
                    elif directory:
                        self.write_placeholder(node)
                    else:
                        node.mkdir(mode=0o700)
                    try:
                        self.validate(rejected=True)
                    finally:
                        if node.is_dir():
                            node.rmdir()
                        else:
                            node.unlink()
                        target.rename(node)

    def test_unknown_entries(self):
        for parent in (self.credentials, self.staging, self.version):
            for directory in (False, True):
                with self.subTest(parent=parent.relative_to(self.config), directory=directory):
                    unknown = parent / "unexpected"
                    if directory:
                        unknown.mkdir(mode=0o700)
                    else:
                        self.write_placeholder(unknown)
                    try:
                        self.validate(rejected=True)
                    finally:
                        if directory:
                            unknown.rmdir()
                        else:
                            unknown.unlink()

    def test_incomplete_staging(self):
        for name in ("private", "public"):
            with self.subTest(missing=name):
                path = self.version / name
                path.unlink()
                try:
                    self.validate(rejected=True)
                finally:
                    self.write_placeholder(path)

    def test_invalid_versions(self):
        for name in ("0", "01", "-1", "1.0", "9" * 65, ".prepare.interrupted"):
            with self.subTest(version=name):
                invalid = self.staging / name
                self.version.rename(invalid)
                try:
                    self.validate(rejected=True)
                finally:
                    invalid.rename(self.version)


if __name__ == "__main__":
    unittest.main(verbosity=2)
