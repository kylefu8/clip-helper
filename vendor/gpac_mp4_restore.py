from __future__ import annotations

"""Conservative ISO-BMFF helpers for lossless GPAC split outputs.

Only the temporary remux/candidate passed by the caller is ever modified. Source
files are opened read-only. Unsupported box layouts fail closed so a caller can
leave the original untouched.
"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


# Boxes whose children may contain absolute chunk offsets. Sample entries are
# deliberately excluded: their nested data is codec configuration, not boxes.
CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
UNSUPPORTED_OFFSET_BOXES = {b"saio", b"iloc", b"tfra", b"trun", b"sidx"}


def read_box_header(data: bytes | bytearray, offset: int, end: int) -> tuple[bytes, int, int]:
    if offset < 0 or offset + 8 > end:
        raise ValueError(f"short ISO-BMFF box header at {offset}")
    size32 = int.from_bytes(data[offset : offset + 4], "big")
    box_type = bytes(data[offset + 4 : offset + 8])
    if size32 == 1:
        if offset + 16 > end:
            raise ValueError(f"short extended ISO-BMFF box header at {offset}")
        size = int.from_bytes(data[offset + 8 : offset + 16], "big")
        header_size = 16
    elif size32 == 0:
        size = end - offset
        header_size = 8
    else:
        size = size32
        header_size = 8
    if size < header_size or offset + size > end:
        raise ValueError(f"invalid {box_type!r} box at {offset}: size={size}, end={end}")
    return box_type, size, header_size


def list_children(data: bytes | bytearray, box_offset: int, box_size: int) -> list[tuple[bytes, int, int, int]]:
    kind, size, header = read_box_header(data, box_offset, box_offset + box_size)
    if size != box_size:
        raise ValueError(f"{kind!r} parent box size mismatch")
    start = box_offset + header + (4 if kind == b"meta" else 0)
    end = box_offset + size
    rows: list[tuple[bytes, int, int, int]] = []
    pos = start
    while pos + 8 <= end:
        child_type, child_size, child_header = read_box_header(data, pos, end)
        rows.append((child_type, pos, child_size, child_header))
        pos += child_size
    if pos != end:
        raise ValueError(f"trailing bytes in {kind!r} children: {end - pos}")
    return rows


def top_level(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    size = path.stat().st_size
    with path.open("rb") as stream:
        pos = 0
        while pos + 8 <= size:
            stream.seek(pos)
            header = stream.read(16)
            size32 = int.from_bytes(header[:4], "big")
            box_type = header[4:8]
            if size32 == 1:
                if len(header) < 16:
                    raise ValueError(f"short extended top-level box at {pos}")
                box_size = int.from_bytes(header[8:16], "big")
                header_size = 16
            elif size32 == 0:
                box_size = size - pos
                header_size = 8
            else:
                box_size = size32
                header_size = 8
            if box_size < header_size or pos + box_size > size:
                raise ValueError(f"invalid top-level box {box_type!r} at {pos}: size={box_size}")
            result.append({"type": box_type, "offset": pos, "size": box_size, "header_size": header_size})
            pos += box_size
        if pos != size:
            raise ValueError(f"{size - pos} trailing bytes after last top-level ISO-BMFF box")
    return result


def _read_moov(path: Path) -> tuple[dict[str, Any], bytearray]:
    rows = [row for row in top_level(path) if row["type"] == b"moov"]
    if len(rows) != 1:
        raise ValueError(f"expected one moov box in {path}, found {len(rows)}")
    row = rows[0]
    with path.open("rb") as stream:
        stream.seek(row["offset"])
        raw = stream.read(row["size"])
    if len(raw) != row["size"]:
        raise OSError(f"short read of moov in {path}")
    return row, bytearray(raw)


def _direct_optional(data: bytes | bytearray, name: bytes) -> list[tuple[bytes, int, int, int]]:
    return [row for row in list_children(data, 0, len(data)) if row[0] == name]


def _box_bytes(data: bytes | bytearray, row: tuple[bytes, int, int, int]) -> bytes:
    _, offset, size, _ = row
    return bytes(data[offset : offset + size])


def _build_box(kind: bytes, payload: bytes, header_size: int = 8) -> bytes:
    if len(kind) != 4:
        raise ValueError("ISO-BMFF box type must be four bytes")
    if header_size == 8:
        size = 8 + len(payload)
        if size >= 1 << 32:
            raise ValueError("rebuilt moov requires an extended-size header, which is not supported")
        return size.to_bytes(4, "big") + kind + payload
    if header_size == 16:
        size = 16 + len(payload)
        return (1).to_bytes(4, "big") + kind + size.to_bytes(8, "big") + payload
    raise ValueError(f"unsupported original moov header size: {header_size}")


def _restore_moov_metadata(source_moov: bytearray, remux_moov: bytearray, header_size: int) -> tuple[bytearray, dict[str, Any]]:
    source_children = list_children(source_moov, 0, len(source_moov))
    remux_children = list_children(remux_moov, 0, len(remux_moov))
    names = (b"udta", b"meta")
    source_metadata: dict[bytes, bytes] = {}
    remux_metadata: dict[bytes, bytes] = {}
    for name in names:
        src_rows = [row for row in source_children if row[0] == name]
        dst_rows = [row for row in remux_children if row[0] == name]
        if len(src_rows) > 1 or len(dst_rows) > 1:
            raise ValueError(f"duplicate direct moov/{name.decode('ascii')} metadata box")
        if src_rows:
            source_metadata[name] = _box_bytes(source_moov, src_rows[0])
        if dst_rows:
            remux_metadata[name] = _box_bytes(remux_moov, dst_rows[0])

    pieces: list[bytes] = []
    inserted: set[bytes] = set()
    for box_type, offset, size, _ in remux_children:
        if box_type in names:
            if box_type in source_metadata:
                pieces.append(source_metadata[box_type])
                inserted.add(box_type)
            # Drop metadata that GPAC created if it was absent in the source.
        else:
            pieces.append(bytes(remux_moov[offset : offset + size]))
    for name in names:
        if name in source_metadata and name not in inserted:
            pieces.append(source_metadata[name])
    rebuilt = bytearray(_build_box(b"moov", b"".join(pieces), header_size))
    return rebuilt, {
        "source_moov_udta_bytes": len(source_metadata.get(b"udta", b"")),
        "remux_moov_udta_bytes": len(remux_metadata.get(b"udta", b"")),
        "source_moov_meta_bytes": len(source_metadata.get(b"meta", b"")),
        "remux_moov_meta_bytes": len(remux_metadata.get(b"meta", b"")),
        "udta_restored_exactly": hashlib.sha256(source_metadata.get(b"udta", b"")).digest()
        == hashlib.sha256(_metadata_from_bytes(rebuilt, b"udta")).digest(),
        "meta_restored_exactly": hashlib.sha256(source_metadata.get(b"meta", b"")).digest()
        == hashlib.sha256(_metadata_from_bytes(rebuilt, b"meta")).digest(),
    }


def _metadata_from_bytes(moov: bytearray, wanted: bytes) -> bytes:
    rows = [row for row in list_children(moov, 0, len(moov)) if row[0] == wanted]
    if not rows:
        return b""
    if len(rows) != 1:
        raise ValueError(f"rebuilt moov contains duplicate {wanted!r}")
    return _box_bytes(moov, rows[0])


def _patch_chunk_offsets(
    data: bytearray,
    box_offset: int,
    box_size: int,
    delta: int,
    new_mdat_start: int,
    new_mdat_end: int,
) -> int:
    box_type, size, header = read_box_header(data, box_offset, box_offset + box_size)
    if box_type in UNSUPPORTED_OFFSET_BOXES:
        raise ValueError(f"cannot safely adjust unsupported offset-bearing box {box_type.decode('latin1')}")
    if box_type in (b"stco", b"co64"):
        if header != 8 or size < 16:
            raise ValueError(f"unsupported {box_type.decode('latin1')} box layout")
        count = int.from_bytes(data[box_offset + 12 : box_offset + 16], "big")
        width = 4 if box_type == b"stco" else 8
        if 16 + count * width != size:
            raise ValueError(f"unexpected {box_type.decode('latin1')} entry table size")
        maximum = (1 << (width * 8)) - 1
        for index in range(count):
            pos = box_offset + 16 + index * width
            old = int.from_bytes(data[pos : pos + width], "big")
            new = old + delta
            if new < new_mdat_start or new >= new_mdat_end:
                raise ValueError(f"patched chunk offset {new} is outside the remux mdat")
            if new < 0 or new > maximum:
                raise ValueError(f"patched chunk offset does not fit {box_type.decode('latin1')}")
            data[pos : pos + width] = new.to_bytes(width, "big")
        return count
    if box_type not in CONTAINER_BOXES:
        return 0
    start = box_offset + header + (4 if box_type == b"meta" else 0)
    end = box_offset + size
    pos = start
    total = 0
    while pos + 8 <= end:
        child_type, child_size, _ = read_box_header(data, pos, end)
        # stsd/dref payloads have sample-entry/data-reference structures which
        # are not a sequence of ordinary child boxes.
        if child_type not in (b"stsd", b"dref"):
            total += _patch_chunk_offsets(data, pos, child_size, delta, new_mdat_start, new_mdat_end)
        pos += child_size
    if pos != end:
        raise ValueError(f"trailing bytes while scanning {box_type.decode('latin1')}")
    return total


def _hash_range(path: Path, start: int, length: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        stream.seek(start)
        remaining = length
        while remaining:
            block = stream.read(min(8 * 1024 * 1024, remaining))
            if not block:
                raise OSError("unexpected EOF while hashing media payload")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def restore_gpac_metadata(source_path: str | Path, remuxed_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Restore source ftyp/moov metadata into a GPAC split and repair offsets.

    Requires a single GPAC output mdat with moov before it. The result is written
    exclusively; existing files are never overwritten. All mdat payload bytes
    must remain identical to the GPAC remux.
    """
    source = Path(source_path)
    remuxed = Path(remuxed_path)
    output = Path(output_path)
    if source.resolve() == output.resolve() or remuxed.resolve() == output.resolve():
        raise ValueError("source/remux input cannot also be the restored output")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    source_boxes = top_level(source)
    remux_boxes = top_level(remuxed)
    source_moov_row, source_moov = _read_moov(source)
    remux_moov_row, remux_moov = _read_moov(remuxed)
    remux_mdats = [row for row in remux_boxes if row["type"] == b"mdat"]
    if len(remux_mdats) != 1:
        raise ValueError(f"expected one GPAC output mdat, found {len(remux_mdats)}")
    if remux_moov_row["header_size"] not in (8, 16):
        raise ValueError("unsupported GPAC moov box header")
    if remux_moov_row["offset"] > remux_mdats[0]["offset"]:
        raise ValueError("GPAC moov after mdat is unsupported by the safe metadata restorer")

    source_ftyp_rows = [row for row in source_boxes if row["type"] == b"ftyp"]
    remux_ftyp_rows = [row for row in remux_boxes if row["type"] == b"ftyp"]
    if len(source_ftyp_rows) > 1 or len(remux_ftyp_rows) > 1:
        raise ValueError("duplicate top-level ftyp boxes are unsupported")
    source_ftyp = b""
    remux_ftyp = b""
    if source_ftyp_rows:
        row = source_ftyp_rows[0]
        if row["offset"] > source_moov_row["offset"]:
            raise ValueError("source ftyp after moov is unsupported")
        with source.open("rb") as stream:
            stream.seek(row["offset"])
            source_ftyp = stream.read(row["size"])
        if len(source_ftyp) != row["size"]:
            raise OSError("short source ftyp read")
    if remux_ftyp_rows:
        row = remux_ftyp_rows[0]
        if row["offset"] > remux_mdats[0]["offset"]:
            raise ValueError("GPAC ftyp after mdat is unsupported")
        with remuxed.open("rb") as stream:
            stream.seek(row["offset"])
            remux_ftyp = stream.read(row["size"])
        if len(remux_ftyp) != row["size"]:
            raise OSError("short GPAC ftyp read")

    new_moov, metadata_report = _restore_moov_metadata(source_moov, remux_moov, remux_moov_row["header_size"])
    moov_delta = len(new_moov) - remux_moov_row["size"]
    if remux_ftyp_rows:
        ftyp_delta_before_mdat = (len(source_ftyp) if source_ftyp else 0) - len(remux_ftyp)
    else:
        ftyp_delta_before_mdat = len(source_ftyp)
    total_media_offset_delta = moov_delta + ftyp_delta_before_mdat
    new_mdat_start = remux_mdats[0]["offset"] + total_media_offset_delta
    new_mdat_end = new_mdat_start + remux_mdats[0]["size"]
    new_mdat_data_start = new_mdat_start + remux_mdats[0]["header_size"]
    if new_mdat_data_start < 0:
        raise ValueError("restored metadata would move mdat before start of file")

    # This helper handles a non-fragmented file with a single mdat. Refuse
    # top-level indexes that contain offsets it does not understand.
    for row in remux_boxes:
        if row["type"] in UNSUPPORTED_OFFSET_BOXES:
            raise ValueError(f"unsupported top-level offset-bearing box {row['type'].decode('latin1')}")
    patched_chunk_offsets = _patch_chunk_offsets(
        new_moov, 0, len(new_moov), total_media_offset_delta,
        new_mdat_data_start, new_mdat_end,
    )
    try:
        with remuxed.open("rb") as inp, output.open("xb") as out:
            if source_ftyp and not remux_ftyp_rows:
                out.write(source_ftyp)
            for row in remux_boxes:
                if row["type"] == b"ftyp":
                    if source_ftyp:
                        out.write(source_ftyp)
                    continue
                if row["type"] == b"moov":
                    out.write(new_moov)
                    continue
                inp.seek(row["offset"])
                remaining = row["size"]
                while remaining:
                    block = inp.read(min(8 * 1024 * 1024, remaining))
                    if not block:
                        raise OSError(f"unexpected EOF while copying {row['type']!r}")
                    out.write(block)
                    remaining -= len(block)
            out.flush()

        old_mdat_data_start = remux_mdats[0]["offset"] + remux_mdats[0]["header_size"]
        mdat_data_length = remux_mdats[0]["size"] - remux_mdats[0]["header_size"]
        before_hash = _hash_range(remuxed, old_mdat_data_start, mdat_data_length)
        after_hash = _hash_range(output, new_mdat_data_start, mdat_data_length)
        if before_hash != after_hash:
            raise ValueError("mdat payload changed while restoring metadata")
        if not all(metadata_report[key] for key in ("udta_restored_exactly", "meta_restored_exactly")):
            raise ValueError("source moov metadata boxes were not restored byte-for-byte")
        output_ftyp_rows = [row for row in top_level(output) if row["type"] == b"ftyp"]
        if source_ftyp:
            if len(output_ftyp_rows) != 1 or output_ftyp_rows[0]["size"] != len(source_ftyp):
                raise ValueError("restored output ftyp box count or size differs from source")
            with output.open("rb") as stream:
                stream.seek(output_ftyp_rows[0]["offset"])
                restored_ftyp = stream.read(output_ftyp_rows[0]["size"])
        else:
            restored_ftyp = b""
        ftyp_restored_exactly = restored_ftyp == source_ftyp and len(output_ftyp_rows) == (1 if source_ftyp else 0)
        if not ftyp_restored_exactly:
            raise ValueError("source ftyp box was not restored byte-for-byte")
        source_ftyp_sha = hashlib.sha256(source_ftyp).hexdigest() if source_ftyp else None
        restored_ftyp_sha = hashlib.sha256(restored_ftyp).hexdigest() if restored_ftyp else None
        return {
            "source": str(source.resolve()), "remuxed": str(remuxed.resolve()),
            "output": str(output.resolve()),
            "source_mdat_count": sum(row["type"] == b"mdat" for row in source_boxes),
            "remux_mdat_count": len(remux_mdats), "metadata": metadata_report,
            "source_ftyp_bytes": len(source_ftyp), "remux_ftyp_bytes": len(remux_ftyp),
            "source_ftyp_sha256": source_ftyp_sha,
            "restored_ftyp_sha256": restored_ftyp_sha,
            "ftyp_restored_exactly": ftyp_restored_exactly,
            "patched_chunk_offset_entries": patched_chunk_offsets,
            "mdat_payload_sha256_before": before_hash,
            "mdat_payload_sha256_after": after_hash,
            "mdat_payload_unchanged": True, "output_size_bytes": output.stat().st_size,
        }
    except Exception:
        # This path belongs exclusively to the current prepare call.
        output.unlink(missing_ok=True)
        raise


def _find_direct(data: bytearray, parent_offset: int, parent_size: int, wanted: bytes) -> tuple[int, int, int]:
    rows = [row for row in list_children(data, parent_offset, parent_size) if row[0] == wanted]
    if len(rows) != 1:
        raise ValueError(f"expected one direct {wanted.decode('latin1')} box, found {len(rows)}")
    _, offset, size, header = rows[0]
    return offset, size, header


def _full_box_fields(data: bytearray, box: tuple[int, int, int]) -> tuple[int, int, int]:
    offset, size, header = box
    if size < header + 4:
        raise ValueError("short ISO-BMFF full box")
    base = offset + header
    version = data[base]
    if version not in (0, 1):
        raise ValueError(f"unsupported full-box version {version}")
    width = 4 if version == 0 else 8
    return base, width, version


def _copy_creation_modification_times(source: bytearray, target: bytearray, source_box: tuple[int, int, int], target_box: tuple[int, int, int]) -> None:
    source_base, source_width, _ = _full_box_fields(source, source_box)
    target_base, target_width, _ = _full_box_fields(target, target_box)
    for index in range(2):
        start = source_base + 4 + index * source_width
        value = int.from_bytes(source[start : start + source_width], "big")
        target_start = target_base + 4 + index * target_width
        target[target_start : target_start + target_width] = value.to_bytes(target_width, "big")


def _track_headers(moov: bytearray) -> dict[int, dict[str, tuple[int, int, int]]]:
    result: dict[int, dict[str, tuple[int, int, int]]] = {}
    for kind, offset, size, _ in list_children(moov, 0, len(moov)):
        if kind != b"trak":
            continue
        tkhd = _find_direct(moov, offset, size, b"tkhd")
        base, width, version = _full_box_fields(moov, tkhd)
        track_id_start = base + 4 + 2 * width
        track_id = int.from_bytes(moov[track_id_start : track_id_start + 4], "big")
        if track_id in result:
            raise ValueError(f"duplicate MP4 track id {track_id}")
        mdia = _find_direct(moov, offset, size, b"mdia")
        mdhd = _find_direct(moov, mdia[0], mdia[1], b"mdhd")
        matrix_start = base + (40 if version == 0 else 52)
        if matrix_start + 36 > tkhd[0] + tkhd[1]:
            raise ValueError(f"track {track_id} tkhd matrix is truncated")
        result[track_id] = {"tkhd": tkhd, "mdhd": mdhd, "matrix_start": (matrix_start, 36, 0)}
    return result


def restore_movie_header_fields(source_path: str | Path, candidate_path: str | Path) -> dict[str, Any]:
    """Restore movie/track creation times and the exact source display matrices."""
    source_path = Path(source_path)
    candidate_path = Path(candidate_path)
    if source_path.resolve() == candidate_path.resolve():
        raise ValueError("refusing to modify the source file")
    source_row, source_moov = _read_moov(source_path)
    candidate_row, candidate_moov = _read_moov(candidate_path)
    original_candidate_moov_length = len(candidate_moov)

    _copy_creation_modification_times(
        source_moov,
        candidate_moov,
        _find_direct(source_moov, 0, len(source_moov), b"mvhd"),
        _find_direct(candidate_moov, 0, len(candidate_moov), b"mvhd"),
    )
    source_tracks = _track_headers(source_moov)
    candidate_tracks = _track_headers(candidate_moov)
    if set(source_tracks) != set(candidate_tracks):
        raise ValueError("source and candidate track IDs differ; cannot safely restore headers")

    for track_id, target in candidate_tracks.items():
        original = source_tracks[track_id]
        _copy_creation_modification_times(source_moov, candidate_moov, original["tkhd"], target["tkhd"])
        _copy_creation_modification_times(source_moov, candidate_moov, original["mdhd"], target["mdhd"])
        src_matrix_start, _, _ = original["matrix_start"]
        dst_matrix_start, _, _ = target["matrix_start"]
        candidate_moov[dst_matrix_start : dst_matrix_start + 36] = source_moov[src_matrix_start : src_matrix_start + 36]

    if len(candidate_moov) != original_candidate_moov_length:
        raise ValueError("candidate moov length changed while restoring header fields")
    with candidate_path.open("r+b") as stream:
        stream.seek(candidate_row["offset"])
        stream.write(candidate_moov)
        stream.flush()

    return {
        "source": str(source_path.resolve()),
        "candidate": str(candidate_path.resolve()),
        "source_track_ids": sorted(source_tracks),
        "creation_modification_times_restored": True,
        "display_matrices_restored_exactly": True,
    }


__all__ = ["restore_gpac_metadata", "restore_movie_header_fields", "top_level", "list_children"]
