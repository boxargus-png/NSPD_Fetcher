import ast
import configparser
import hashlib
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from tests.support import ROOT
from scripts.build_release import build_release, package_files, read_config


class PackagingTests(unittest.TestCase):
    def test_deploy_manifests_include_all_transitive_plugin_imports(self):
        required = {"__init__.py"}
        pending = ["__init__.py"]
        while pending:
            path = pending.pop()
            for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    names = [node.module] if node.module else [alias.name for alias in node.names]
                    for name in names:
                        filename = name.split('.')[0] + '.py'
                        if filename not in required:
                            required.add(filename)
                            pending.append(filename)
        config = configparser.ConfigParser()
        config.read(ROOT / "pb_tool.cfg", encoding="utf-8-sig")
        pb_files = set(config["files"]["python_files"].split()) | {"resources.py"}
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8-sig").replace("\\\n", " ")
        make_files = set(re.search(r"^PY_FILES\s*=([^\n]+)", makefile, re.M).group(1).split()) | {"resources.py"}
        for manifest in (pb_files, make_files):
            self.assertFalse(required - manifest, "Missing: {}".format(required - manifest))
            self.assertTrue(all((ROOT / filename).is_file() for filename in manifest))


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / "build" / "packaging-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.output = Path(self.temporary.name) / "output"
        self.root.mkdir()
        _, files = package_files(ROOT)
        for filename in files + ["pb_tool.cfg"]:
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.metadata = read_config(self.root / "metadata.txt")
        for key in ("repository", "homepage", "tracker"):
            self.metadata["general"][key] = ""
        self.write_metadata()

    def write_metadata(self):
        with (self.root / "metadata.txt").open("w", encoding="utf-8") as stream:
            self.metadata.write(stream)

    def test_archive_allowlist_integrity_and_reproducibility(self):
        for extra in ("nspd_fetcher.log", "private_extract.xml", "debug.pyc"):
            (self.root / extra).write_text("must not be included", encoding="utf-8")
        archive, report = build_release(self.root, self.output, allow_unpublished=True)
        original_bytes = archive.read_bytes()
        self.assertTrue(archive.name.endswith("-local.zip"))
        self.assertEqual(len(report["publication_blockers"]), 3)
        self.assertEqual(hashlib.sha256(original_bytes).hexdigest(), report["sha256"])
        name, files = package_files(self.root)
        with ZipFile(archive) as bundle:
            self.assertIsNone(bundle.testzip())
            self.assertEqual(bundle.namelist(), [name + "/" + path for path in files])
            for path in files:
                self.assertEqual(bundle.read(name + "/" + path), (self.root / path).read_bytes())
        self.assertIn(report["sha256"], archive.with_suffix(".zip.sha256").read_text())
        build_release(self.root, self.output, allow_unpublished=True)
        self.assertEqual(original_bytes, archive.read_bytes())

    def test_public_build_requires_real_links_but_local_build_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "public repository URL"):
            build_release(self.root, self.output)
        self.assertFalse(self.output.exists())
        for value in ("https://github.com/Ваш_GitHub_Логин/NSPD_Fetcher", "not a url"):
            self.metadata["general"]["repository"] = value
            self.write_metadata()
            with self.assertRaisesRegex(ValueError, "public repository URL"):
                build_release(self.root, self.output)
        # URL syntax only; no external request or assertion about this repository.
        for key in ("repository", "homepage", "tracker"):
            self.metadata["general"][key] = "https://github.com/test/nspd-fetcher"
        self.write_metadata()
        archive, report = build_release(self.root, self.output)
        self.assertFalse(archive.name.endswith("-local.zip"))
        self.assertEqual(report["publication_blockers"], [])

    def test_required_license_and_transitive_imports_cannot_be_omitted(self):
        config_path = self.root / "pb_tool.cfg"
        original = config_path.read_text(encoding="utf-8")
        for filename in ("LICENSE", "task_controller.py"):
            config_path.write_text(original.replace(filename, ""), encoding="utf-8")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "Missing from package"):
                build_release(self.root, self.output, allow_unpublished=True)

    def test_missing_files_and_paths_outside_source_are_rejected(self):
        config_path = self.root / "pb_tool.cfg"
        original = config_path.read_text(encoding="utf-8")
        for filename, message in (("missing.txt", "Missing package"),
                                  ("../private.txt", "Unsafe package"),
                                  ("C:/private.txt", "Unsafe package")):
            config_path.write_text(original.replace("extras:", "extras: " + filename), encoding="utf-8")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, message):
                build_release(self.root, self.output, allow_unpublished=True)
