#!/usr/bin/env python3
"""Build and optionally install the Tsukihime Chinese patch BNTX/BC7 fix.

The v3.0 Chinese patch was built with an older BNTX texture replacer.  In a
modified BNTX archive, translated textures were saved with flags 0x07 and
layout 0x30..0x34, while untouched textures in the same archive retained
flags 0x09 and layout 0x40..0x44.  The upstream compatibility fix rewrites
the complete modified BNTX, so both groups must use flags 0x01 and retain
only the block-height bits (layout & 0x07).

This tool starts from the verified official Chinese v3.0 mod, changes only
BRTI flags/layout metadata in the 124 affected BNTX archives, proves that all
569 compressed texture payloads are unchanged, writes a separate repaired mod, and
can atomically install CHS.mrg/CHS.hed/CHS.nam after making a rollback backup.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SECTOR_SIZE = 0x800
HED_RECORD = struct.Struct("<IHH")
NXX_HEADER = struct.Struct("<4sIII")
NAME_RECORD_SIZE = 32

EXPECTED_SOURCE_HASHES = {
    "CHS.hed": "b4b1c10a7683ad14f56dadac7faa590708965222cdd91ce2551e04d22eab5c1e",
    "CHS.mrg": "e1b262c2f62c7ec868d975959890f1a192f848f750268181574d55677a8cbb32",
    "CHS.nam": "262c7b564fad8b1ead3fadae1e277f3dc4736163f44a900cf2a75a575d8b22dd",
}
EXPECTED_SOURCE_FLAGS = Counter({0x01: 19, 0x07: 166, 0x09: 384})
EXPECTED_OUTPUT_FLAGS = Counter({0x01: 455, 0x09: 114})
EXPECTED_ARCHIVE_ENTRIES = 225
EXPECTED_BNTX_ARCHIVES = 216
EXPECTED_TEXTURES = 569
EXPECTED_AFFECTED_ARCHIVES = 124
EXPECTED_LEGACY_TEXTURES = 166
EXPECTED_COPACK_TEXTURES = 270
EXPECTED_NORMALIZED_TEXTURES = 436
EXPECTED_CHANGED_METADATA_BYTES = 872
EXPECTED_PURE_BNTX_ARCHIVES = 92
TITLE_ARCHIVE_INDICES = (207, 208)
TITLE_ARCHIVE_NAME = "TITLE_PARTS_JA.NXGZ"
TITLE_TEXTURE_COUNT = 15


class RepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextureRecord:
    index: int
    flags_position: int
    layout_position: int
    endian: str
    flags: int
    layout: int
    name: str
    format: int
    width: int
    height: int
    tile_mode: int
    swizzle: int
    mip_count: int
    image_size: int
    payload_start: int
    name_pointer: int

    def payload_signature(self, data: bytes) -> dict[str, object]:
        payload = data[self.payload_start : self.payload_start + self.image_size]
        return {
            "name": self.name,
            "name_pointer": self.name_pointer,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "tile_mode": self.tile_mode,
            "swizzle": self.swizzle,
            "mip_count": self.mip_count,
            "image_size": self.image_size,
            "sha256": sha256_bytes(payload),
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_hed(hed: bytes, mrg_size: int) -> list[tuple[int, int, int]]:
    if len(hed) % HED_RECORD.size:
        raise RepairError("CHS.hed size is not divisible by 8")

    entries: list[tuple[int, int, int]] = []
    eof_records = 0
    for position in range(0, len(hed), HED_RECORD.size):
        offset, size_sectors, uncompressed_sectors = HED_RECORD.unpack_from(hed, position)
        if offset == 0xFFFFFFFF:
            eof_records += 1
            continue
        if eof_records:
            raise RepairError("CHS.hed contains data after its EOF marker")
        start = offset * SECTOR_SIZE
        end = start + size_sectors * SECTOR_SIZE
        if end > mrg_size:
            raise RepairError(f"CHS.hed entry {len(entries)} points outside CHS.mrg")
        entries.append((offset, size_sectors, uncompressed_sectors))

    if eof_records != 2:
        raise RepairError(f"CHS.hed should contain 2 EOF records, found {eof_records}")
    return entries


def parse_names(nam: bytes, entry_count: int) -> list[str]:
    if len(nam) % NAME_RECORD_SIZE:
        raise RepairError("CHS.nam size is not divisible by 32")
    record_count = len(nam) // NAME_RECORD_SIZE
    if record_count != entry_count + 1:
        raise RepairError(
            f"CHS.nam should contain {entry_count + 1} records, found {record_count}"
        )

    names: list[str] = []
    for index in range(entry_count):
        raw = nam[index * NAME_RECORD_SIZE : (index + 1) * NAME_RECORD_SIZE]
        raw = raw.split(b"\0", 1)[0]
        for encoding in ("ascii", "shift_jis", "utf-8"):
            try:
                names.append(raw.decode(encoding))
                break
            except UnicodeDecodeError:
                continue
        else:
            raise RepairError(f"CHS.nam record {index} has an unsupported encoding")
    return names


def decode_nxx(raw: bytes) -> tuple[bytes, bytes] | None:
    if len(raw) < NXX_HEADER.size or raw[:4] not in (b"NXGX", b"NXCX"):
        return None
    magic, expected_size, compressed_size, _padding = NXX_HEADER.unpack_from(raw)
    end = NXX_HEADER.size + compressed_size
    if end > len(raw):
        raise RepairError("NXX compressed payload extends beyond its MRG entry")
    payload = raw[NXX_HEADER.size:end]
    if magic == b"NXGX":
        data = gzip.decompress(payload)
    else:
        data = zlib.decompress(payload)
    if len(data) != expected_size:
        raise RepairError(
            f"NXX size mismatch: header={expected_size}, decompressed={len(data)}"
        )
    return magic, data


def encode_nxx(magic: bytes, data: bytes) -> bytes:
    if magic == b"NXGX":
        payload = gzip.compress(data, compresslevel=9, mtime=0)
    elif magic == b"NXCX":
        payload = zlib.compress(data, level=9)
    else:
        raise RepairError(f"Unsupported NXX type: {magic!r}")
    return NXX_HEADER.pack(magic, len(data), len(payload), 0) + payload


def decode_texture_name(data: bytes, position: int, endian: str) -> str:
    if position < 0 or position >= len(data):
        raise RepairError("BNTX texture name pointer is outside the file")

    def decode_candidate(raw: bytes) -> str | None:
        if not raw or b"\0" in raw:
            return None
        for encoding in ("utf-8", "shift_jis"):
            try:
                value = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            if all(ord(character) >= 0x20 for character in value):
                return value
        return None

    # Tool-rewritten records point at the two-byte string-table length.
    if position + 3 <= len(data):
        length = struct.unpack_from(endian + "H", data, position)[0]
        start = position + 2
        end = start + length
        if length <= 4096 and end < len(data) and data[end] == 0:
            decoded = decode_candidate(data[start:end])
            if decoded is not None:
                return decoded

    # Untouched records in the same old BNTX can point directly at the first
    # character instead.  Their preceding 16-bit value is not a byte length,
    # so validate them as bounded NUL-terminated names.
    terminator = data.find(b"\0", position, min(len(data), position + 4097))
    if terminator != -1:
        decoded = decode_candidate(data[position:terminator])
        if decoded is not None:
            return decoded
    # Some untouched records in the old mixed packs point inside a shared
    # string-table entry rather than to a standalone decodable name.  Preserve
    # and compare the numeric pointer instead of consuming unrelated bytes.
    return f"<name-pointer-0x{position:X}>"


def bntx_texture_records(data: bytes | bytearray) -> list[TextureRecord]:
    if len(data) < 88 or bytes(data[:8]) != b"BNTX\0\0\0\0":
        return []
    bom = bytes(data[12:14])
    if bom == b"\xff\xfe":
        endian = "<"
    elif bom == b"\xfe\xff":
        endian = ">"
    else:
        raise RepairError("BNTX has an invalid byte-order marker")

    container = struct.Struct(endian + "4sI5qI4x")
    target, count, info_pointers, *_rest = container.unpack_from(data, 32)
    if target not in (b"NX  ", b"Gen "):
        raise RepairError(f"BNTX has an invalid target: {target!r}")
    if count > 100000:
        raise RepairError(f"BNTX texture count is implausible: {count}")

    pointer_struct = struct.Struct(endian + "q")
    texture_struct = struct.Struct(endian + "2B4H2x2I3i3I20x3IB3x8q")
    records: list[TextureRecord] = []
    for texture_index in range(count):
        pointer_position = info_pointers + texture_index * pointer_struct.size
        if pointer_position < 0 or pointer_position + pointer_struct.size > len(data):
            raise RepairError("BNTX texture pointer is outside the file")
        brti = pointer_struct.unpack_from(data, pointer_position)[0]
        info_position = brti + 16
        if brti < 0 or info_position + texture_struct.size > len(data):
            raise RepairError("BNTX BRTI pointer is outside the file")
        if bytes(data[brti : brti + 4]) != b"BRTI":
            raise RepairError("BNTX texture pointer does not reference BRTI")

        values = texture_struct.unpack_from(data, info_position)
        flags = values[0]
        tile_mode = values[2]
        swizzle = values[3]
        mip_count = values[4]
        format_value = values[6]
        width = values[8]
        height = values[9]
        layout = values[12]
        image_size = values[14]
        name_pointer = values[18]
        mip_pointers = values[20]
        if mip_pointers < 0 or mip_pointers + pointer_struct.size > len(data):
            raise RepairError("BNTX mip pointer table is outside the file")
        payload_start = pointer_struct.unpack_from(data, mip_pointers)[0]
        if payload_start < 0 or payload_start + image_size > len(data):
            raise RepairError("BNTX image payload extends beyond the file")

        records.append(
            TextureRecord(
                index=texture_index,
                flags_position=info_position,
                layout_position=info_position + 36,
                endian=endian,
                flags=flags,
                layout=layout,
                name=decode_texture_name(bytes(data), name_pointer, endian),
                format=format_value,
                width=width,
                height=height,
                tile_mode=tile_mode,
                swizzle=swizzle,
                mip_count=mip_count,
                image_size=image_size,
                payload_start=payload_start,
                name_pointer=name_pointer,
            )
        )
    return records


def read_archive(mod_root: Path) -> dict[str, object]:
    romfs = mod_root / "romfs"
    paths = {name: romfs / name for name in ("CHS.hed", "CHS.mrg", "CHS.nam")}
    for path in paths.values():
        if not path.is_file():
            raise RepairError(f"Missing required patch file: {path}")
    hed = paths["CHS.hed"].read_bytes()
    mrg = paths["CHS.mrg"].read_bytes()
    nam = paths["CHS.nam"].read_bytes()
    entries = parse_hed(hed, len(mrg))
    names = parse_names(nam, len(entries))
    return {
        "romfs": romfs,
        "paths": paths,
        "hed": hed,
        "mrg": mrg,
        "nam": nam,
        "entries": entries,
        "names": names,
    }


def entry_bytes(mrg: bytes, entry: tuple[int, int, int]) -> bytes:
    offset, size_sectors, _uncompressed_sectors = entry
    return mrg[offset * SECTOR_SIZE : (offset + size_sectors) * SECTOR_SIZE]


def counter_to_json(counter: Counter) -> dict[str, int]:
    return {str(key): value for key, value in sorted(counter.items()) if value}


def layout_counter_to_json(counter: Counter) -> dict[str, int]:
    return {f"0x{key:02X}": value for key, value in sorted(counter.items()) if value}


def scan_archive(mod_root: Path) -> dict[str, object]:
    archive = read_archive(mod_root)
    mrg = archive["mrg"]
    entries = archive["entries"]
    names = archive["names"]
    assert isinstance(mrg, bytes)
    assert isinstance(entries, list)
    assert isinstance(names, list)

    flags = Counter()
    layouts = Counter()
    bntx_archives = 0
    textures = 0
    payloads: dict[str, dict[str, object]] = {}
    records_by_archive: dict[int, list[TextureRecord]] = {}
    bntx_hashes: dict[int, str] = {}
    raw_entry_hashes: dict[int, str] = {}

    for archive_index, entry in enumerate(entries):
        raw = entry_bytes(mrg, entry)
        raw_entry_hashes[archive_index] = sha256_bytes(raw)
        decoded = decode_nxx(raw)
        if decoded is None:
            continue
        _magic, data = decoded
        if data[:8] != b"BNTX\0\0\0\0":
            continue
        records = bntx_texture_records(data)
        bntx_archives += 1
        textures += len(records)
        records_by_archive[archive_index] = records
        bntx_hashes[archive_index] = sha256_bytes(data)
        for record in records:
            flags[record.flags] += 1
            layouts[(record.flags, record.layout)] += 1
            payloads[f"{archive_index}:{record.index}"] = {
                "archive_name": names[archive_index],
                **record.payload_signature(data),
            }

    payload_digest = sha256_bytes(
        json.dumps(payloads, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    return {
        "archive": archive,
        "flags": flags,
        "layouts": layouts,
        "bntx_archives": bntx_archives,
        "textures": textures,
        "payloads": payloads,
        "payload_digest": payload_digest,
        "records_by_archive": records_by_archive,
        "bntx_hashes": bntx_hashes,
        "raw_entry_hashes": raw_entry_hashes,
    }


def verify_source(scan: dict[str, object]) -> None:
    archive = scan["archive"]
    assert isinstance(archive, dict)
    paths = archive["paths"]
    entries = archive["entries"]
    assert isinstance(paths, dict)
    assert isinstance(entries, list)
    actual_hashes = {name: sha256_file(path) for name, path in paths.items()}
    if actual_hashes != EXPECTED_SOURCE_HASHES:
        raise RepairError(
            "Source CHS files do not match the verified official Chinese v3.0 build: "
            + json.dumps(actual_hashes, sort_keys=True)
        )
    if len(entries) != EXPECTED_ARCHIVE_ENTRIES:
        raise RepairError(
            f"Expected {EXPECTED_ARCHIVE_ENTRIES} archive entries, found {len(entries)}"
        )
    if scan["bntx_archives"] != EXPECTED_BNTX_ARCHIVES:
        raise RepairError(
            f"Expected {EXPECTED_BNTX_ARCHIVES} BNTX archives, found {scan['bntx_archives']}"
        )
    if scan["textures"] != EXPECTED_TEXTURES:
        raise RepairError(f"Expected {EXPECTED_TEXTURES} textures, found {scan['textures']}")
    if scan["flags"] != EXPECTED_SOURCE_FLAGS:
        raise RepairError(
            f"Unexpected source texture flags: {dict(scan['flags'])}; "
            f"expected {dict(EXPECTED_SOURCE_FLAGS)}"
        )


def file_inventory(root: Path, excluded_relative_paths: set[str]) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=str):
        relative = path.relative_to(root).as_posix()
        if relative in excluded_relative_paths:
            continue
        inventory[relative] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return inventory


def validate_title_archives(scan: dict[str, object]) -> dict[str, object]:
    archive = scan["archive"]
    records_by_archive = scan["records_by_archive"]
    raw_hashes = scan["raw_entry_hashes"]
    bntx_hashes = scan["bntx_hashes"]
    assert isinstance(archive, dict)
    assert isinstance(records_by_archive, dict)
    assert isinstance(raw_hashes, dict)
    assert isinstance(bntx_hashes, dict)
    names = archive["names"]
    assert isinstance(names, list)

    title_payload: dict[str, object] | None = None
    for archive_index in TITLE_ARCHIVE_INDICES:
        if names[archive_index] != TITLE_ARCHIVE_NAME:
            raise RepairError(
                f"Archive {archive_index} should be {TITLE_ARCHIVE_NAME}, found {names[archive_index]}"
            )
        records = records_by_archive.get(archive_index)
        if records is None or len(records) != TITLE_TEXTURE_COUNT:
            found = 0 if records is None else len(records)
            raise RepairError(
                f"Title archive {archive_index} should contain {TITLE_TEXTURE_COUNT} textures, found {found}"
            )
        for record in records:
            if record.flags != 0x01 or not 0 <= record.layout <= 0x04:
                raise RepairError(
                    f"Title archive {archive_index} texture {record.index} was not normalized"
                )
            if record.name == "titlemenu":
                if (
                    record.width != 968
                    or record.height != 472
                    or record.format != 0x2006
                    or record.layout != 0x04
                ):
                    raise RepairError("titlemenu regression check failed")
                title_payload = {
                    "name": record.name,
                    "width": record.width,
                    "height": record.height,
                    "format": f"0x{record.format:X}",
                    "layout": f"0x{record.layout:X}",
                    "flags": f"0x{record.flags:X}",
                }

    if raw_hashes[TITLE_ARCHIVE_INDICES[0]] != raw_hashes[TITLE_ARCHIVE_INDICES[1]]:
        raise RepairError("The duplicate title archive entries no longer match")
    if bntx_hashes[TITLE_ARCHIVE_INDICES[0]] != bntx_hashes[TITLE_ARCHIVE_INDICES[1]]:
        raise RepairError("The duplicate decompressed title BNTX archives no longer match")
    if title_payload is None:
        raise RepairError("titlemenu texture was not found in the title archives")
    return {
        "archive_indices": list(TITLE_ARCHIVE_INDICES),
        "archive_name": TITLE_ARCHIVE_NAME,
        "textures_per_archive": TITLE_TEXTURE_COUNT,
        "duplicates_identical": True,
        "titlemenu": title_payload,
    }


def build_repaired_archive(source_mod: Path, output_mod: Path) -> dict[str, object]:
    if output_mod.exists():
        raise RepairError(f"Output directory already exists: {output_mod}")
    if source_mod.resolve() == output_mod.resolve():
        raise RepairError("Source and output directories must be different")

    source_scan = scan_archive(source_mod)
    verify_source(source_scan)
    source_archive = source_scan["archive"]
    assert isinstance(source_archive, dict)
    source_mrg = source_archive["mrg"]
    source_entries = source_archive["entries"]
    source_names = source_archive["names"]
    assert isinstance(source_mrg, bytes)
    assert isinstance(source_entries, list)
    assert isinstance(source_names, list)

    output_mod.parent.mkdir(parents=True, exist_ok=True)
    staging = output_mod.parent / f".{output_mod.name}.building-{uuid.uuid4().hex}"
    if staging.exists():
        raise RepairError(f"Unexpected staging directory already exists: {staging}")

    changed_archives: list[dict[str, object]] = []
    affected_indices: set[int] = set()
    legacy_textures = 0
    copack_textures = 0
    normalized_textures = 0
    changed_metadata_bytes = 0

    try:
        shutil.copytree(source_mod, staging, copy_function=shutil.copy2)
        output_mrg = bytearray()
        output_hed = bytearray()

        for archive_index, entry in enumerate(source_entries):
            raw = entry_bytes(source_mrg, entry)
            output_entry = raw
            decoded = decode_nxx(raw)
            if decoded is not None:
                magic, data = decoded
                if data[:8] == b"BNTX\0\0\0\0":
                    records = bntx_texture_records(data)
                    if any(record.flags == 0x07 for record in records):
                        patched = bytearray(data)
                        original_flag_counts = Counter(record.flags for record in records)
                        original_layout_counts = Counter(record.layout for record in records)
                        if set(original_flag_counts) - {0x07, 0x09}:
                            raise RepairError(
                                f"Affected archive {archive_index} has unexpected flags "
                                f"{dict(original_flag_counts)}"
                            )

                        local_legacy = 0
                        local_copack = 0
                        for record in records:
                            if record.flags == 0x07:
                                if record.layout not in range(0x30, 0x35):
                                    raise RepairError(
                                        f"Archive {archive_index} texture {record.index}: flags 0x07 "
                                        f"has unexpected layout 0x{record.layout:X}"
                                    )
                                local_legacy += 1
                            elif record.flags == 0x09:
                                if record.layout not in range(0x40, 0x45):
                                    raise RepairError(
                                        f"Archive {archive_index} texture {record.index}: flags 0x09 "
                                        f"has unexpected layout 0x{record.layout:X}"
                                    )
                                local_copack += 1

                            patched[record.flags_position] = 0x01
                            struct.pack_into(
                                record.endian + "I",
                                patched,
                                record.layout_position,
                                record.layout & 0x07,
                            )

                        byte_changes = sum(
                            left != right for left, right in zip(data, patched, strict=True)
                        )
                        local_normalized = len(records)
                        if byte_changes != local_normalized * 2:
                            raise RepairError(
                                f"Archive {archive_index}: expected {local_normalized * 2} "
                                f"metadata byte changes, found {byte_changes}"
                            )
                        patched_records = bntx_texture_records(patched)
                        if any(
                            record.flags != 0x01 or record.layout != (records[i].layout & 0x07)
                            for i, record in enumerate(patched_records)
                        ):
                            raise RepairError(f"Archive {archive_index} failed in-memory validation")

                        output_entry = encode_nxx(magic, bytes(patched))
                        affected_indices.add(archive_index)
                        legacy_textures += local_legacy
                        copack_textures += local_copack
                        normalized_textures += local_normalized
                        changed_metadata_bytes += byte_changes
                        changed_archives.append(
                            {
                                "archive_index": archive_index,
                                "archive_name": source_names[archive_index],
                                "container": magic.decode("ascii"),
                                "legacy_0x07_textures": local_legacy,
                                "copack_0x09_textures": local_copack,
                                "normalized_textures": local_normalized,
                                "changed_metadata_bytes": byte_changes,
                                "source_flag_counts": counter_to_json(original_flag_counts),
                                "source_layout_counts": layout_counter_to_json(
                                    original_layout_counts
                                ),
                                "source_bntx_sha256": sha256_bytes(data),
                                "output_bntx_sha256": sha256_bytes(bytes(patched)),
                                "old_entry_bytes": NXX_HEADER.size + NXX_HEADER.unpack_from(raw)[2],
                                "new_entry_bytes": len(output_entry),
                            }
                        )

            output_offset = len(output_mrg) // SECTOR_SIZE
            output_sectors = (len(output_entry) + SECTOR_SIZE - 1) // SECTOR_SIZE
            output_hed += HED_RECORD.pack(output_offset, output_sectors, entry[2])
            output_mrg += output_entry
            output_mrg += b"\0" * (output_sectors * SECTOR_SIZE - len(output_entry))

        output_hed += b"\xff" * (HED_RECORD.size * 2)

        observed = {
            "affected_archives": len(affected_indices),
            "legacy_textures": legacy_textures,
            "copack_textures": copack_textures,
            "normalized_textures": normalized_textures,
            "changed_metadata_bytes": changed_metadata_bytes,
        }
        expected = {
            "affected_archives": EXPECTED_AFFECTED_ARCHIVES,
            "legacy_textures": EXPECTED_LEGACY_TEXTURES,
            "copack_textures": EXPECTED_COPACK_TEXTURES,
            "normalized_textures": EXPECTED_NORMALIZED_TEXTURES,
            "changed_metadata_bytes": EXPECTED_CHANGED_METADATA_BYTES,
        }
        if observed != expected:
            raise RepairError(f"Second-generation repair count mismatch: {observed}; expected {expected}")

        output_romfs = staging / "romfs"
        atomic_write(output_romfs / "CHS.mrg", bytes(output_mrg))
        atomic_write(output_romfs / "CHS.hed", bytes(output_hed))

        output_scan = scan_archive(staging)
        if output_scan["flags"] != EXPECTED_OUTPUT_FLAGS:
            raise RepairError(
                f"Unexpected output flags: {dict(output_scan['flags'])}; "
                f"expected {dict(EXPECTED_OUTPUT_FLAGS)}"
            )
        if output_scan["bntx_archives"] != EXPECTED_BNTX_ARCHIVES:
            raise RepairError("Output BNTX archive count changed")
        if output_scan["textures"] != EXPECTED_TEXTURES:
            raise RepairError("Output texture count changed")
        if source_scan["payloads"] != output_scan["payloads"]:
            raise RepairError("One or more texture payloads or immutable texture fields changed")
        if source_scan["payload_digest"] != output_scan["payload_digest"]:
            raise RepairError("Texture payload aggregate digest changed")

        source_raw_hashes = source_scan["raw_entry_hashes"]
        output_raw_hashes = output_scan["raw_entry_hashes"]
        assert isinstance(source_raw_hashes, dict)
        assert isinstance(output_raw_hashes, dict)
        pure_bntx_indices = set(source_scan["records_by_archive"]) - affected_indices
        if len(pure_bntx_indices) != EXPECTED_PURE_BNTX_ARCHIVES:
            raise RepairError(
                f"Expected {EXPECTED_PURE_BNTX_ARCHIVES} pure BNTX archives, "
                f"found {len(pure_bntx_indices)}"
            )
        for archive_index in pure_bntx_indices:
            if source_raw_hashes[archive_index] != output_raw_hashes[archive_index]:
                raise RepairError(f"Pure BNTX archive {archive_index} changed unexpectedly")

        source_files = file_inventory(
            source_mod, {"romfs/CHS.hed", "romfs/CHS.mrg"}
        )
        output_files = file_inventory(staging, {"romfs/CHS.hed", "romfs/CHS.mrg"})
        if source_files != output_files:
            raise RepairError("A copied non-CHS file or CHS.nam changed unexpectedly")

        title_check = validate_title_archives(output_scan)
        output_hashes = {
            name: sha256_file(output_romfs / name)
            for name in ("CHS.hed", "CHS.mrg", "CHS.nam")
        }
        source_hashes = {
            name: sha256_file(source_mod / "romfs" / name)
            for name in ("CHS.hed", "CHS.mrg", "CHS.nam")
        }
        manifest: dict[str, object] = {
            "repair": "Tsukihime Chinese v3.0 BNTX/BC7 compatibility fix",
            "manifest_schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_mod": str(source_mod.resolve()),
            "output_mod": str(output_mod.resolve()),
            "source_hashes_sha256": source_hashes,
            "output_hashes_sha256": output_hashes,
            "source_flag_counts": counter_to_json(source_scan["flags"]),
            "output_flag_counts": counter_to_json(output_scan["flags"]),
            "archive_entries": len(source_entries),
            "bntx_archives": output_scan["bntx_archives"],
            "texture_records": output_scan["textures"],
            "affected_bntx_archives": len(affected_indices),
            "pure_bntx_archives_byte_identical": len(pure_bntx_indices),
            "legacy_0x07_textures": legacy_textures,
            "copack_0x09_textures": copack_textures,
            "normalized_textures": normalized_textures,
            "changed_decompressed_metadata_bytes": changed_metadata_bytes,
            "texture_payloads_reencoded": False,
            "all_texture_payload_hashes_match": True,
            "texture_payload_aggregate_sha256": output_scan["payload_digest"],
            "title_archive_check": title_check,
            "changes": changed_archives,
        }
        atomic_write(
            staging / "BNTX_FIX_MANIFEST.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        note = (
            "Tsukihime Chinese patch v3.0 - BNTX/BC7 compatibility repair\n\n"
            "The old toolchain produced mixed BNTX header semantics inside modified packs.\n"
            "Every flags=0x07/0x30..0x34 record and every co-resident\n"
            "flags=0x09/0x40..0x44 record in the 124 affected archives now uses\n"
            "flags=0x01 and layout=(old_layout & 0x07). Pure original BNTX archives\n"
            "were preserved byte-for-byte. BC7 payloads were not decoded or re-encoded.\n\n"
            f"Affected BNTX archives: {len(affected_indices)}\n"
            f"Textures normalized: {normalized_textures}\n"
            f"Decompressed metadata bytes changed: {changed_metadata_bytes}\n"
            f"Texture payloads verified unchanged: {output_scan['textures']}\n"
            "See BNTX_FIX_MANIFEST.json for hashes and the complete audit trail.\n"
        )
        atomic_write(staging / "README_BNTX_FIX.txt", note.encode("utf-8"))

        os.replace(staging, output_mod)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def ensure_ryujinx_stopped() -> None:
    if os.name != "nt":
        return
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RepairError("Could not verify whether Ryujinx is running")
    rows = csv.reader(io.StringIO(result.stdout))
    running = [row[0] for row in rows if row and "ryujinx" in row[0].lower()]
    if running:
        raise RepairError("Close Ryujinx before installing the repaired patch: " + ", ".join(running))


def install_repaired_triplet(
    output_mod: Path, install_mod: Path, backup_root: Path
) -> tuple[Path, dict[str, object]]:
    ensure_ryujinx_stopped()
    fixed_romfs = output_mod / "romfs"
    install_romfs = install_mod / "romfs"
    names = ("CHS.mrg", "CHS.hed", "CHS.nam")
    for name in names:
        if not (fixed_romfs / name).is_file():
            raise RepairError(f"Fixed patch is missing {name}")
        if not (install_romfs / name).is_file():
            raise RepairError(f"Installed patch is missing {name}")

    output_scan = scan_archive(output_mod)
    if output_scan["flags"] != EXPECTED_OUTPUT_FLAGS:
        raise RepairError("Refusing to install an output with unexpected texture flags")
    if output_scan["bntx_archives"] != EXPECTED_BNTX_ARCHIVES:
        raise RepairError("Refusing to install an output with an unexpected BNTX count")
    if output_scan["textures"] != EXPECTED_TEXTURES:
        raise RepairError("Refusing to install an output with an unexpected texture count")
    validate_title_archives(output_scan)

    excluded_triplet = {f"romfs/{name}" for name in names}
    preinstall_non_chs = file_inventory(install_mod, excluded_triplet)
    preinstall_hashes = {name: sha256_file(install_romfs / name) for name in names}
    preinstall_sizes = {name: (install_romfs / name).stat().st_size for name in names}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = backup_root / f"Backup_before_BNTX_fix_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for name in names:
        shutil.copy2(install_romfs / name, backup_dir / name)
    for name in names:
        if sha256_file(backup_dir / name) != preinstall_hashes[name]:
            raise RepairError(f"Backup verification failed for {name}")

    backup_manifest: dict[str, object] = {
        "created_local": datetime.now().astimezone().isoformat(),
        "install_mod": str(install_mod.resolve()),
        "files": {
            name: {"sha256": preinstall_hashes[name], "bytes": preinstall_sizes[name]}
            for name in names
        },
        "non_chs_inventory": preinstall_non_chs,
    }
    atomic_write(
        backup_dir / "BACKUP_MANIFEST.json",
        json.dumps(backup_manifest, ensure_ascii=False, indent=2).encode("utf-8"),
    )

    rollback_attempted = False
    try:
        for name in names:
            atomic_copy(fixed_romfs / name, install_romfs / name)
        installed_hashes = {name: sha256_file(install_romfs / name) for name in names}
        output_hashes = {name: sha256_file(fixed_romfs / name) for name in names}
        if installed_hashes != output_hashes:
            raise RepairError("Installed CHS hashes do not match the repaired output")

        installed_scan = scan_archive(install_mod)
        if installed_scan["flags"] != EXPECTED_OUTPUT_FLAGS:
            raise RepairError("Installed archive has unexpected texture flags")
        if installed_scan["bntx_archives"] != EXPECTED_BNTX_ARCHIVES:
            raise RepairError("Installed archive has an unexpected BNTX count")
        if installed_scan["textures"] != EXPECTED_TEXTURES:
            raise RepairError("Installed archive has an unexpected texture count")
        if installed_scan["payload_digest"] != output_scan["payload_digest"]:
            raise RepairError("Installed texture payload digest does not match the repaired output")
        title_check = validate_title_archives(installed_scan)

        postinstall_non_chs = file_inventory(install_mod, excluded_triplet)
        if postinstall_non_chs != preinstall_non_chs:
            raise RepairError("A non-CHS mod file changed during installation")

        result: dict[str, object] = {
            "installed_local": datetime.now().astimezone().isoformat(),
            "install_mod": str(install_mod.resolve()),
            "backup_dir": str(backup_dir.resolve()),
            "installed_hashes_sha256": installed_hashes,
            "non_chs_files_unchanged": True,
            "texture_payload_aggregate_sha256": installed_scan["payload_digest"],
            "title_archive_check": title_check,
        }
        atomic_write(
            output_mod / "INSTALL_RESULT.json",
            json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return backup_dir, result
    except Exception:
        rollback_attempted = True
        for name in names:
            atomic_copy(backup_dir / name, install_romfs / name)
        restored_hashes = {name: sha256_file(install_romfs / name) for name in names}
        if restored_hashes != preinstall_hashes:
            raise RepairError("Installation failed and rollback hash verification also failed")
        raise
    finally:
        if rollback_attempted:
            ensure_ryujinx_stopped()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Official v3.0 mod directory")
    parser.add_argument("--output", required=True, type=Path, help="New repaired mod directory")
    parser.add_argument("--install-to", type=Path, help="Optional active Ryujinx mod directory")
    parser.add_argument(
        "--backup-root",
        type=Path,
        help="Backup parent; defaults to the repaired mod's parent directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    print(f"Source:  {source}")
    print(f"Output:  {output}")
    manifest = build_repaired_archive(source, output)
    print(
        f"Built BNTX/BC7 fix: {manifest['normalized_textures']} textures in "
        f"{manifest['affected_bntx_archives']} archives; "
        f"{manifest['changed_decompressed_metadata_bytes']} metadata bytes changed."
    )
    print(
        f"Verified unchanged texture payloads: {manifest['texture_records']} "
        f"(digest {manifest['texture_payload_aggregate_sha256']})"
    )

    if args.install_to:
        install_to = args.install_to.resolve()
        backup_root = (args.backup_root or output.parent).resolve()
        backup_dir, _result = install_repaired_triplet(output, install_to, backup_root)
        print(f"Installed to: {install_to}")
        print(f"Backup:      {backup_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepairError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
