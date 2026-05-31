"""Rebuild the game's RocksDB_v2_Backups checkpoint ZIP after a live DB write.

The game restores the live DB from this ZIP on every load, so any write to the
live DB must be reflected here or the change will be overwritten on next launch.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

_CRC32C_TABLE: list[int] = []
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ 0x82F63B78 if (_crc & 1) else _crc >> 1
    _CRC32C_TABLE.append(_crc)


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32C_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def _session_identity(data: bytes) -> str | None:
    marker = b"session.identity"
    pos = data.find(marker)
    if pos == -1:
        return None
    i = pos + len(marker)
    while i < len(data) and data[i] == 0:
        i += 1
    chars: list[str] = []
    while i < len(data) and 0x20 <= data[i] < 0x7F:
        chars.append(chr(data[i]))
        i += 1
    return "".join(chars).strip() or None


_SKIP_NAMES = {"LOCK", "IDENTITY", "LOG", "rocksdict-config.json", "rocksdict-config.bak"}


def update_checkpoint_zip(save_root: Path, db_dir: Path) -> None:
    """Rebuild the game's backup ZIP to match the current live DB state.

    Must be called immediately after a successful write to the live DB.
      save_root — versioned root, e.g. .../RocksDB_v2/0.10.0
      db_dir    — specific DB directory, e.g. .../Players/{uuid}
    """
    profile_root = save_root.parent.parent
    version = save_root.name
    db_type = db_dir.parent.name
    db_id = db_dir.name

    zip_path = (
        profile_root
        / "RocksDB_v2_Backups"
        / db_type
        / db_id
        / f"{db_id}_{version}_Latest.zip"
    )
    if not zip_path.exists():
        return

    with zipfile.ZipFile(str(zip_path)) as old_zf:
        old_meta_lines = old_zf.read("Checkpoint/meta/1").decode().strip().splitlines()
        meta_line0 = old_meta_lines[0] if old_meta_lines else "0"
        meta_line1 = old_meta_lines[1] if len(old_meta_lines) > 1 else "0"
        additional: list[tuple[str, bytes]] = [
            (info.filename, old_zf.read(info.filename))
            for info in old_zf.infolist()
            if "AdditionalRecordFiles" in info.filename
        ]

    shared: list[tuple[str, bytes, int]] = []
    private_files: list[tuple[str, bytes, int]] = []

    for f in sorted(db_dir.iterdir()):
        if f.is_dir() or f.name in _SKIP_NAMES or f.name.startswith("LOG"):
            continue
        name = f.name
        content = f.read_bytes()

        if f.suffix == ".sst":
            sid = _session_identity(content)
            size = len(content)
            renamed = f"{f.stem}_s{sid}_{size}.sst" if sid else f"{f.stem}_{size}.sst"
            shared.append((f"shared_checksum/{renamed}", content, _crc32c(content)))

        elif f.suffix == ".blob":
            crc = _crc32c(content)
            renamed = f"{f.stem}_{crc}_{len(content)}.blob"
            shared.append((f"shared_checksum/{renamed}", content, crc))

        elif (
            name.startswith("MANIFEST-")
            or name == "CURRENT"
            or name.startswith("OPTIONS-")
            or f.suffix == ".log"
        ):
            crc = _crc32c(content) if content else 0
            private_files.append((f"private/1/{name}", content, crc))

    all_entries = shared + private_files
    meta_lines = [f"{path} crc32 {crc}" for path, _, crc in all_entries]
    meta_content = f"{meta_line0}\n{meta_line1}\n{len(all_entries)}\n" + "\n".join(meta_lines) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Checkpoint/meta/1", meta_content)
        for path, content, _ in all_entries:
            zf.writestr(f"Checkpoint/{path}", content)
        for zip_name, content in additional:
            zf.writestr(zip_name, content)

    tmp = zip_path.with_suffix(".zip.tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(zip_path)