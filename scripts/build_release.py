"""Build an installable QGIS ZIP from an explicit manifest, using only stdlib."""
import argparse
import ast
import configparser
import hashlib
import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def read_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(path.read_text(encoding="utf-8-sig"))
    return config


def package_files(root):
    """Use pb_tool's allowlist; never walk the checkout or installed profile."""
    config = read_config(root / "pb_tool.cfg")
    name = config["plugin"]["name"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Plugin directory must be an ASCII Python package name")
    files = config["files"]
    if files.get("extra_dirs", "").strip() or files.get("locales", "").strip():
        raise ValueError("List additional files explicitly before packaging directories/locales")
    paths = set()
    for key in ("python_files", "main_dialog", "extras", "resource_files"):
        paths.update(files.get(key, "").split())
    for key in ("compiled_ui_files", "resource_files"):
        paths.update(str(PurePosixPath(p).with_suffix(".py"))
                     for p in files.get(key, "").split())
    for path in paths:
        relative = PurePosixPath(path)
        target = root / path
        if (relative.is_absolute() or ".." in relative.parts or "\\" in path
                or ":" in path or target.is_symlink()
                or root.resolve() not in target.resolve().parents):
            raise ValueError("Unsafe package path: " + path)
        if not target.is_file():
            raise ValueError("Missing package file: " + path)
        if target.suffix.lower() in {".log", ".pyc", ".zip", ".exe", ".dll"}:
            raise ValueError("Unexpected runtime file: " + path)
    required = {"__init__.py", "metadata.txt", "LICENSE", "README.md", "CHANGELOG.md"}
    for path in paths:
        if path.endswith(".py"):
            tree = ast.parse((root / path).read_text(encoding="utf-8-sig"), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    modules = [node.module] if node.module else [a.name for a in node.names]
                    required.update(m.split(".")[0] + ".py" for m in modules)
    missing = required - paths
    if missing:
        raise ValueError("Missing from package manifest: " + ", ".join(sorted(missing)))
    return name, sorted(paths)


def release_blockers(metadata):
    blockers = []
    for key in ("repository", "homepage", "tracker"):
        value = metadata.get(key, "").strip()
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.hostname in {"example.com", "localhost"}
                or "Ваш_GitHub" in value or any(c.isspace() for c in value)):
            blockers.append("Set a real public {} URL in metadata.txt".format(key))
    return blockers


def build_release(root=ROOT, output_dir=None, allow_unpublished=False):
    root = Path(root).resolve()
    name, paths = package_files(root)
    metadata = read_config(root / "metadata.txt")["general"]
    for key in ("name", "description", "about", "version", "author", "email",
                "qgisMinimumVersion", "qgisMaximumVersion", "license", "changelog"):
        if not metadata.get(key, "").strip():
            raise ValueError("Missing metadata: " + key)
    version = metadata["version"]
    if not re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
        raise ValueError("Version must use dotted numbers, e.g. 0.1.0")
    if metadata.get("icon") not in paths:
        raise ValueError("Metadata icon is missing from the manifest")
    blockers = release_blockers(metadata)
    if blockers and not allow_unpublished:
        raise ValueError("\n".join(blockers) + "\nUse --allow-unpublished only for a local preview ZIP.")
    output_dir = Path(output_dir) if output_dir else root / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-local" if allow_unpublished else ""
    archive = output_dir / "{}-{}{}.zip".format(name, version, suffix)
    hashes = {}
    # Fixed timestamps, order and permissions make repeated builds reproducible
    # in the same Python/zlib environment; file mtimes never affect the archive.
    with tempfile.TemporaryDirectory(prefix="package-", dir=output_dir) as staging:
        temporary = Path(staging) / archive.name
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
            for path in paths:
                data = (root / path).read_bytes()
                entry = ZipInfo(name + "/" + path, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                bundle.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=9)
                hashes[path] = hashlib.sha256(data).hexdigest()
        with ZipFile(temporary) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("ZIP integrity check failed")
        if temporary.stat().st_size > 25 * 1024 * 1024:
            raise ValueError("Plugin ZIP exceeds 25 MiB")
        temporary.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        "{}  {}\n".format(digest, archive.name), encoding="ascii")
    report = {
        "archive": archive.name, "sha256": digest, "version": version,
        "qgis_minimum": metadata["qgisMinimumVersion"],
        "qgis_maximum": metadata["qgisMaximumVersion"],
        "local_preview": allow_unpublished, "publication_blockers": blockers,
        "files": hashes,
    }
    archive.with_suffix(".manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return archive, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-unpublished", action="store_true",
                        help="Build a local preview even without public repository URLs")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        archive, report = build_release(output_dir=args.output_dir,
                                        allow_unpublished=args.allow_unpublished)
    except (ValueError, OSError, configparser.Error, SyntaxError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(archive)
    print("{} files; SHA256 {}".format(len(report["files"]), report["sha256"]))
    for blocker in report["publication_blockers"]:
        print("Publication pending: " + blocker)


if __name__ == "__main__":
    main()
