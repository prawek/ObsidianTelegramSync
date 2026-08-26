"""
Obsidian vault file-system parser.

Accepts either:
  - a directory path (local vault)
  - a ZIP file path (uploaded archive)

Returns a structured list of VaultNote objects ready for sync.
"""
import io
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from markdown_parser import parse_note, ParsedNote


SUPPORTED_EXTENSIONS = {".md"}
IGNORED_DIRS = {".obsidian", ".trash", ".git", "__MACOSX"}


@dataclass
class VaultNote:
    """A single Obsidian note ready to be synced."""
    parsed: ParsedNote
    raw_content: str
    relative_path: str          # e.g. "Projects/2024/Ideas.md"
    folder_path: str            # e.g. "Projects/2024"  (empty str = root)
    stem: str                   # filename without extension


@dataclass
class VaultStructure:
    vault_name: str
    notes: list[VaultNote] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)   # unique, sorted

    @property
    def note_count(self) -> int:
        return len(self.notes)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_vault_directory(vault_dir: str | Path) -> VaultStructure:
    """Parse an Obsidian vault from a local directory."""
    root = Path(vault_dir).resolve()
    vault_name = root.name
    notes: list[VaultNote] = []
    folders: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden / system folders
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIRS and not d.startswith(".")
        ]

        for fname in sorted(filenames):
            suffix = Path(fname).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                continue

            full_path = Path(dirpath) / fname
            rel_path = full_path.relative_to(root)
            relative_str = str(rel_path).replace(os.sep, "/")
            folder_str = str(rel_path.parent).replace(os.sep, "/")
            if folder_str == ".":
                folder_str = ""

            stem = rel_path.stem
            raw = full_path.read_text(encoding="utf-8", errors="replace")
            parsed = parse_note(stem, raw)

            note = VaultNote(
                parsed=parsed,
                raw_content=raw,
                relative_path=relative_str,
                folder_path=folder_str,
                stem=stem,
            )
            notes.append(note)
            if folder_str:
                # Add all ancestor folders
                parts = folder_str.split("/")
                for depth in range(1, len(parts) + 1):
                    folders.add("/".join(parts[:depth]))

    return VaultStructure(
        vault_name=vault_name,
        notes=notes,
        folders=sorted(folders),
    )


def parse_vault_zip(
    zip_source: str | Path | io.BytesIO,
    archive_name: str | None = None,
) -> VaultStructure:
    """Parse an Obsidian vault from a ZIP path or an in-memory archive."""
    if isinstance(zip_source, (str, Path)):
        source = Path(zip_source)
        vault_name = archive_name or source.stem
    else:
        source = zip_source
        vault_name = archive_name or "vault"

    notes: list[VaultNote] = []
    folders: set[str] = set()

    with zipfile.ZipFile(source, "r") as zf:
        # Detect common prefix (e.g. "MyVault/") to strip
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = _common_prefix(zf.namelist())
        if prefix:
            vault_name = prefix.strip("/").split("/")[0] or vault_name

        for name in sorted(names):
            # Skip system / hidden
            parts = name.replace("\\", "/").split("/")
            if any(p in IGNORED_DIRS or p.startswith(".") for p in parts):
                continue

            rel = name[len(prefix):]  # strip common prefix
            if not rel:
                continue

            rel_path = Path(rel)
            if rel_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            folder_str = str(rel_path.parent).replace("\\", "/")
            if folder_str == ".":
                folder_str = ""

            stem = rel_path.stem
            raw = zf.read(name).decode("utf-8", errors="replace")
            parsed = parse_note(stem, raw)

            note = VaultNote(
                parsed=parsed,
                raw_content=raw,
                relative_path=rel.replace("\\", "/"),
                folder_path=folder_str,
                stem=stem,
            )
            notes.append(note)
            if folder_str:
                parts2 = folder_str.split("/")
                for depth in range(1, len(parts2) + 1):
                    folders.add("/".join(parts2[:depth]))

    return VaultStructure(
        vault_name=vault_name,
        notes=notes,
        folders=sorted(folders),
    )


def parse_vault_zip_bytes(data: bytes, archive_name: str = "vault") -> VaultStructure:
    """Parse from raw ZIP bytes (Telegram file download)."""
    return parse_vault_zip(io.BytesIO(data), archive_name=archive_name)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _common_prefix(names: list[str]) -> str:
    """Return one enclosing vault directory, if the archive has one.

    ``os.path.commonprefix`` works character-by-character.  With an archive
    containing a single file such as ``Vault/Ideas/Plan.md`` it used to return
    the whole ``Vault/Ideas/`` path, silently dropping the ``Ideas`` folder.
    A ZIP upload conventionally has at most one wrapper directory, so only
    strip its first path component when every file has the same component.
    """
    files = [n.replace("\\", "/").strip("/") for n in names if not n.endswith("/")]
    if not files or any("/" not in name for name in files):
        return ""

    roots = {name.split("/", 1)[0] for name in files}
    if len(roots) != 1:
        return ""
    return roots.pop() + "/"


def build_folder_hierarchy(folders: list[str]) -> dict[str, list[str]]:
    """
    Given a flat sorted list of folder paths, return a dict mapping
    each parent path → list of immediate children paths.

    Root children are listed under key "".
    """
    tree: dict[str, list[str]] = {"": []}
    for folder in folders:
        tree.setdefault(folder, [])
        parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
        tree.setdefault(parent, [])
        if folder not in tree[parent]:
            tree[parent].append(folder)
    return tree
