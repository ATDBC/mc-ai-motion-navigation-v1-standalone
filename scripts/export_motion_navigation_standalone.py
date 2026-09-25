"""Build and verify the source-only motion/navigation standalone snapshot."""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config/motion-navigation/standalone-export-v1.json"
HASH_FILE = "SHA256SUMS.txt"
METADATA_FILE = "EXPORT-METADATA.json"
_LOCAL_IMPORT_ROOTS = frozenset({"mc2p", "scripts", "tests", "tools"})
_FORBIDDEN_PARTS = frozenset({
    ".git", ".gradle", ".venv", "__pycache__", "artifacts", "build",
    "logs", "output", "run",
})
_FORBIDDEN_SUFFIXES = frozenset({
    ".class", ".jar", ".log", ".pyc", ".pyo",
})


class ExportViolation(ValueError):
    """The export input or output does not satisfy the frozen contract."""


@dataclass(frozen=True, slots=True)
class ExportManifest:
    path: Path
    template_mappings: tuple[tuple[PurePosixPath, PurePosixPath], ...]
    include_globs: tuple[str, ...]
    python_entry_globs: tuple[str, ...]
    required_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True, slots=True)
class VerificationReport:
    file_count: int
    missing_files: tuple[str, ...]
    extra_files: tuple[str, ...]
    changed_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JavaTools:
    java: Path
    javac: Path
    major_version: int


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value:
        raise ExportViolation(f"{label} must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ExportViolation(f"{label} escapes the source tree: {value!r}")
    return path


def load_manifest(path: Path | None = None) -> ExportManifest:
    source = (path or DEFAULT_MANIFEST).resolve()
    try:
        document = json.loads(source.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExportViolation(f"standalone export manifest is unavailable: {source}") from error
    if (type(document) is not dict
            or document.get("schema_version")
                != "mc2p.motion-navigation-standalone-export.v1"):
        raise ExportViolation("unsupported standalone export manifest")
    mappings = document.get("template_mappings")
    include = document.get("include_globs")
    entries = document.get("python_entry_globs")
    required = document.get("required_paths")
    if (type(mappings) is not dict or not mappings
            or type(include) is not list or not include
            or type(entries) is not list or not entries
            or type(required) is not list or not required):
        raise ExportViolation("standalone export manifest sections are incomplete")
    parsed_mappings = tuple(
        (_safe_relative(src, "template source"),
         _safe_relative(dst, "template destination"))
        for src, dst in sorted(mappings.items())
    )
    patterns = []
    for label, values in (("include glob", include), ("python entry glob", entries)):
        for value in values:
            if type(value) is not str or not value or "\\" in value:
                raise ExportViolation(f"{label} must use a non-empty POSIX pattern")
            if value.startswith("/") or ".." in PurePosixPath(value).parts:
                raise ExportViolation(f"{label} escapes the source tree: {value!r}")
            patterns.append(value)
    return ExportManifest(
        source,
        parsed_mappings,
        tuple(include),
        tuple(entries),
        tuple(_safe_relative(item, "required path") for item in required),
    )


def _allowed_source(path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(ROOT.resolve())
    except (OSError, ValueError):
        return False
    if not path.is_file():
        return False
    parts = set(relative.parts)
    return (not parts.intersection(_FORBIDDEN_PARTS)
            and path.suffix.lower() not in _FORBIDDEN_SUFFIXES)


def _glob_files(patterns: Iterable[str]) -> set[Path]:
    result: set[Path] = set()
    for pattern in patterns:
        matches = tuple(path for path in ROOT.glob(pattern) if _allowed_source(path))
        if not matches:
            raise ExportViolation(f"standalone export pattern matched no files: {pattern}")
        result.update(matches)
    return result


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_module(name: str) -> Path | None:
    if not name or name.partition(".")[0] not in _LOCAL_IMPORT_ROOTS:
        return None
    relative = Path(*name.split("."))
    candidates = (ROOT / relative.with_suffix(".py"), ROOT / relative / "__init__.py")
    return next((path for path in candidates if _allowed_source(path)), None)


def _imported_modules(path: Path) -> tuple[str, ...]:
    try:
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise ExportViolation(f"cannot inspect Python imports in {path}") from error
    current = _module_name(path)
    package = current if path.name == "__init__.py" else current.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = node.module or ""
        if node.level:
            try:
                base = importlib.util.resolve_name(
                    "." * node.level + base, package,
                )
            except (ImportError, ValueError):
                continue
        if base:
            names.add(base)
        for alias in node.names:
            if alias.name != "*" and base:
                names.add(base + "." + alias.name)
    return tuple(sorted(names))


def _python_closure(entries: Iterable[Path]) -> set[Path]:
    pending = list(entries)
    result: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in result:
            continue
        result.add(path)
        relative = path.relative_to(ROOT)
        for parent in relative.parents:
            if parent == Path("."):
                continue
            initializer = ROOT / parent / "__init__.py"
            if _allowed_source(initializer) and initializer not in result:
                pending.append(initializer)
        for name in _imported_modules(path):
            imported = _resolve_module(name)
            if imported is not None and imported not in result:
                pending.append(imported)
    return result


def collect_export_files(manifest: ExportManifest) -> dict[PurePosixPath, Path]:
    files: dict[PurePosixPath, Path] = {}
    for source, destination in manifest.template_mappings:
        path = ROOT / Path(source)
        if not _allowed_source(path):
            raise ExportViolation(f"template source is missing: {source}")
        files[destination] = path
    included = _glob_files(manifest.include_globs)
    entries = _glob_files(manifest.python_entry_globs)
    included.update(_python_closure(entries))
    for path in included:
        relative = PurePosixPath(path.relative_to(ROOT).as_posix())
        existing = files.get(relative)
        if existing is not None and existing != path:
            raise ExportViolation(f"two sources target {relative}")
        files[relative] = path
    for required in manifest.required_paths:
        if required not in files and required not in {PurePosixPath(HASH_FILE),
                                                       PurePosixPath(METADATA_FILE)}:
            raise ExportViolation(f"required export path is not selected: {required}")
    return dict(sorted(files.items(), key=lambda item: item[0].as_posix()))


def _guard_export_target(root: Path) -> Path:
    target = root.resolve()
    safe_parent = (ROOT / ".tmp").resolve()
    try:
        relative = target.relative_to(safe_parent)
    except ValueError as error:
        raise ExportViolation("export target must be inside the workspace .tmp directory") from error
    if not relative.parts:
        raise ExportViolation("export target cannot be the .tmp directory itself")
    return target


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}\n?", result.stdout) is None:
        raise ExportViolation("cannot identify the source commit")
    return result.stdout.strip()


def _source_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise ExportViolation("cannot inspect the source worktree")
    return bool(result.stdout.strip())


def _write_hashes(root: Path) -> None:
    lines = []
    for path in sorted(
            (path for path in root.rglob("*")
             if path.is_file() and path.name != HASH_FILE),
            key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
    (root / HASH_FILE).write_text("".join(lines), encoding="utf-8", newline="\n")


def export_tree(root: Path, *, clean: bool,
                manifest: ExportManifest | None = None) -> Path:
    selected = manifest or load_manifest()
    target = _guard_export_target(Path(root))
    if target.exists():
        if not clean:
            raise ExportViolation(f"export target already exists: {target}")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    files = collect_export_files(selected)
    for relative, source in files.items():
        destination = target / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    metadata = {
        "schema_version": "mc2p.motion-navigation-standalone-metadata.v1",
        "source_commit": _source_commit(),
        "source_worktree_dirty": _source_dirty(),
        "export_manifest_sha256": hashlib.sha256(selected.path.read_bytes()).hexdigest(),
        "selected_source_file_count": len(files),
    }
    (target / METADATA_FILE).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    _write_hashes(target)
    return target


def _parse_hashes(path: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError as error:
        raise ExportViolation(f"missing {HASH_FILE}") from error
    for line in lines:
        digest, separator, name = line.partition("  ")
        if (not separator or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not name or "\\" in name):
            raise ExportViolation(f"invalid {HASH_FILE} entry")
        relative = _safe_relative(name, "hash path").as_posix()
        if relative in expected:
            raise ExportViolation(f"duplicate hash path: {relative}")
        expected[relative] = digest
    if not expected:
        raise ExportViolation(f"empty {HASH_FILE}")
    return expected


def verify_tree(root: Path, *,
                manifest: ExportManifest | None = None) -> VerificationReport:
    selected = manifest or load_manifest()
    target = Path(root).resolve()
    if not target.is_dir():
        raise ExportViolation(f"standalone tree is missing: {target}")
    expected = _parse_hashes(target / HASH_FILE)
    actual_paths = {}
    for path in target.rglob("*"):
        if not path.is_file() or path.name == HASH_FILE:
            continue
        relative = path.relative_to(target)
        if (set(relative.parts).intersection(_FORBIDDEN_PARTS)
                or path.suffix.lower() in _FORBIDDEN_SUFFIXES):
            continue
        actual_paths[relative.as_posix()] = path
    missing = tuple(sorted(set(expected) - set(actual_paths)))
    extra = tuple(sorted(set(actual_paths) - set(expected)))
    changed = tuple(sorted(
        name for name in set(expected).intersection(actual_paths)
        if hashlib.sha256(actual_paths[name].read_bytes()).hexdigest() != expected[name]
    ))
    required_missing = tuple(sorted(
        path.as_posix() for path in selected.required_paths
        if not (target / Path(path)).is_file()
    ))
    missing = tuple(sorted(set(missing).union(required_missing)))
    report = VerificationReport(len(actual_paths), missing, extra, changed)
    if missing or extra or changed:
        raise ExportViolation(
            "standalone tree differs: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    requirements = (target / "requirements.txt").read_text("utf-8")
    if re.search(r"(?m)^psutil(?:==|>=|~=)", requirements) is None:
        raise ExportViolation("standalone requirements do not declare psutil")
    return report


def discover_java_tools(environment: Mapping[str, str] | None = None) -> JavaTools:
    values = os.environ if environment is None else environment
    suffix = ".exe" if os.name == "nt" else ""
    homes = []
    java_home = values.get("JAVA_HOME")
    if java_home:
        homes.append(Path(java_home) / "bin")
    candidates = []
    for directory in homes:
        candidates.append((directory / f"java{suffix}", directory / f"javac{suffix}"))
    java_path = shutil.which("java", path=values.get("PATH"))
    javac_path = shutil.which("javac", path=values.get("PATH"))
    if java_path and javac_path:
        candidates.append((Path(java_path), Path(javac_path)))
    for java, javac in candidates:
        if not java.is_file() or not javac.is_file():
            continue
        result = subprocess.run(
            [str(javac), "-version"], capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout + result.stderr).strip()
        match = re.search(r"javac\s+(\d+)(?:\.|$)", output)
        if result.returncode == 0 and match and int(match.group(1)) == 21:
            return JavaTools(java.resolve(), javac.resolve(), 21)
    raise ExportViolation(
        "OpenJDK 21 was not found through JAVA_HOME or PATH"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    export = subcommands.add_parser("export")
    export.add_argument("--root", type=Path, required=True)
    export.add_argument("--clean", action="store_true")
    verify = subcommands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    subcommands.add_parser("check-java")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "export":
            root = export_tree(arguments.root, clean=arguments.clean)
            print(f"STANDALONE_EXPORT_ROOT={root}")
        elif arguments.command == "verify":
            report = verify_tree(arguments.root)
            print(f"STANDALONE_EXPORT_OK files={report.file_count}")
        else:
            tools = discover_java_tools()
            print(json.dumps({
                "java": str(tools.java),
                "javac": str(tools.javac),
                "major_version": tools.major_version,
            }, ensure_ascii=False))
    except ExportViolation as error:
        print(f"STANDALONE_EXPORT_ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
