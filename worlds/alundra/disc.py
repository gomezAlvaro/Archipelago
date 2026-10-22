"""
Reads a raw CD image (2352 bytes per sector, Mode 2) holding an ISO 9660 filesystem and writes a copy of it where some
files have been replaced. This is a port of the iso package of the Java randomizer.

The original layout is preserved: every sector is copied in its original order, XA audio and video streams included.
A replaced file is written in place of its original extent. If it needs more sectors than the original one, every
following sector is shifted. Directory records, path tables and the volume descriptor are updated to the new
locations and sizes, and sector headers and error correction codes are regenerated where needed.
"""
import os
import struct
from typing import BinaryIO, NamedTuple

SECTOR_SIZE = 2352
DATA_SIZE = 2048
DATA_OFFSET = 24
PREGAP_SECTORS = 150
PVD_LBA = 16

SUBMODE_EOR = 0x01
SUBMODE_DATA = 0x08
SUBMODE_FORM2 = 0x20
SUBMODE_EOF = 0x80

BATCH_SECTORS = 512


def _build_tables() -> tuple[list[int], list[int], list[int]]:
    ecc_f, ecc_b, edc = [0] * 256, [0] * 256, [0] * 256
    for i in range(256):
        j = (i << 1) ^ (0x11D if i & 0x80 else 0)
        ecc_f[i] = j & 0xFF
        ecc_b[i ^ (j & 0xFF)] = i
        value = i
        for _ in range(8):
            value = (value >> 1) ^ (0xD8018001 if value & 1 else 0)
        edc[i] = value
    return ecc_f, ecc_b, edc


ECC_F_LUT, ECC_B_LUT, EDC_LUT = _build_tables()


class DiscError(Exception):
    pass


def _to_bcd(value: int) -> int:
    return ((value // 10) << 4) | (value % 10)


def set_address(sector: bytearray, lba: int) -> None:
    """Updates the MSF address inside the header of a sector"""
    address = lba + PREGAP_SECTORS
    sector[12] = _to_bcd(address // (60 * 75))
    sector[13] = _to_bcd((address // 75) % 60)
    sector[14] = _to_bcd(address % 75)


def has_sync_pattern(sector: bytes) -> bool:
    return sector[0] == 0 and sector[11] == 0 and all(b == 0xFF for b in sector[1:11])


def is_form2(sector: bytes) -> bool:
    return bool(sector[18] & SUBMODE_FORM2)


def compute_edc(buffer: bytes | bytearray, offset: int, length: int) -> int:
    edc = 0
    for i in range(offset, offset + length):
        edc = (edc >> 8) ^ EDC_LUT[(edc ^ buffer[i]) & 0xFF]
    return edc


def _compute_ecc_block(sector: bytearray, src_offset: int, major_count: int, minor_count: int,
                       major_mult: int, minor_inc: int, dest_offset: int) -> None:
    size = major_count * minor_count
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1)
        ecc_a = ecc_b = 0
        for _ in range(minor_count):
            temp = sector[src_offset + index]
            index += minor_inc
            if index >= size:
                index -= size
            ecc_a ^= temp
            ecc_b ^= temp
            ecc_a = ECC_F_LUT[ecc_a]
        ecc_a = ECC_B_LUT[ECC_F_LUT[ecc_a] ^ ecc_b]
        sector[dest_offset + major] = ecc_a
        sector[dest_offset + major + major_count] = ecc_a ^ ecc_b


def finalize_form1(sector: bytearray) -> None:
    """Recomputes EDC and ECC of a Mode 2 Form 1 sector (sync, header, subheader and data must already be set)"""
    struct.pack_into("<I", sector, 0x818, compute_edc(sector, 0x10, 0x808))
    # The ECC is computed with a zeroed header
    saved = bytes(sector[12:16])
    sector[12:16] = bytes(4)
    _compute_ecc_block(sector, 0xC, 86, 24, 2, 86, 0x81C)
    _compute_ecc_block(sector, 0xC, 52, 43, 86, 88, 0x8C8)
    sector[12:16] = saved


def build_form1(lba: int, data: bytes | bytearray, offset: int, length: int, submode: int,
                original: bytes | None = None) -> bytearray:
    """
    Builds a complete Mode 2 Form 1 sector. If the original sector at that position has the same subheader and
    data, its (already valid) error correction codes are reused, which saves a lot of time.
    """
    sector = bytearray(SECTOR_SIZE)
    sector[1:11] = b"\xFF" * 10
    set_address(sector, lba)
    sector[15] = 2
    sector[18] = sector[22] = submode
    if length > 0:
        length = min(length, DATA_SIZE)
        sector[DATA_OFFSET:DATA_OFFSET + length] = data[offset:offset + length]
    if original is not None and original[16:0x818] == sector[16:0x818]:
        sector[0x818:] = original[0x818:]
    else:
        finalize_form1(sector)
    return sector


class IsoEntry(NamedTuple):
    path: str
    lba: int
    size: int
    directory: bool

    @property
    def sector_count(self) -> int:
        return (self.size + DATA_SIZE - 1) // DATA_SIZE


def normalize(path: str) -> str:
    path = path.replace("\\", "/").lstrip("/")
    return path.split(";")[0].upper()


class CdImage:
    """Read access to a raw single-track CD image, such as a .bin file"""

    def __init__(self, path: str) -> None:
        self.path = path
        size = os.path.getsize(path)
        if size % SECTOR_SIZE:
            raise DiscError(f"'{path}' is not a raw CD image (its size is not a multiple of {SECTOR_SIZE} bytes)")
        self.sector_count = size // SECTOR_SIZE
        self.file: BinaryIO = open(path, "rb")

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> "CdImage":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def read_sectors(self, lba: int, count: int) -> bytes:
        if lba < 0 or lba + count > self.sector_count:
            raise DiscError(f"Tried to read sectors {lba} to {lba + count} outside of the disc image")
        self.file.seek(lba * SECTOR_SIZE)
        data = self.file.read(count * SECTOR_SIZE)
        if len(data) != count * SECTOR_SIZE:
            raise DiscError("Unexpected end of the disc image")
        return data

    def read_sector(self, lba: int) -> bytes:
        return self.read_sectors(lba, 1)

    def read_user_data(self, lba: int) -> bytes:
        return self.read_sector(lba)[DATA_OFFSET:DATA_OFFSET + DATA_SIZE]

    def read_file_data(self, lba: int, size: int) -> bytes:
        count = (size + DATA_SIZE - 1) // DATA_SIZE
        raw = self.read_sectors(lba, count)
        return b"".join(raw[i * SECTOR_SIZE + DATA_OFFSET:i * SECTOR_SIZE + DATA_OFFSET + DATA_SIZE]
                        for i in range(count))[:size]


class Iso9660:
    """A minimal read-only ISO 9660 filesystem parser working on top of a CdImage"""

    def __init__(self, image: CdImage) -> None:
        self.image = image
        self.pvd = image.read_user_data(PVD_LBA)
        if self.pvd[0] != 1 or self.pvd[1:6] != b"CD001":
            raise DiscError("No ISO 9660 primary volume descriptor found inside the disc image")

        self.entries: dict[str, IsoEntry] = {}
        root = self._parse_record(self.pvd, 156, "")
        self.entries[""] = root
        queue = [root]
        while queue:
            directory = queue.pop(0)
            for sector in range(directory.sector_count):
                lba = directory.lba + sector
                data = image.read_user_data(lba)
                offset = 0
                while offset < DATA_SIZE:
                    length = data[offset]
                    if length == 0:
                        break  # Records never cross sector boundaries, the rest of this sector is padding
                    if offset + length > DATA_SIZE or length < 34:
                        raise DiscError(f"Malformed directory record in sector {lba}")
                    name_length = data[offset + 32]
                    if not (name_length == 1 and data[offset + 33] in (0, 1)):
                        name = normalize(data[offset + 33:offset + 33 + name_length].decode("latin-1"))
                        path = f"{directory.path}/{name}" if directory.path else name
                        entry = self._parse_record(data, offset, path)
                        self.entries[path] = entry
                        if entry.directory:
                            queue.append(entry)
                    offset += length

    @staticmethod
    def _parse_record(data: bytes, offset: int, path: str) -> IsoEntry:
        lba, size = struct.unpack_from("<I", data, offset + 2)[0], struct.unpack_from("<I", data, offset + 10)[0]
        return IsoEntry(path, lba, size, bool(data[offset + 25] & 0x02))

    @property
    def volume_space_size(self) -> int:
        return struct.unpack_from("<I", self.pvd, 80)[0]

    @property
    def path_table_size(self) -> int:
        return struct.unpack_from("<I", self.pvd, 132)[0]

    @property
    def path_table_lbas(self) -> list[int]:
        return [struct.unpack_from("<I", self.pvd, 140)[0], struct.unpack_from("<I", self.pvd, 144)[0],
                struct.unpack_from(">I", self.pvd, 148)[0], struct.unpack_from(">I", self.pvd, 152)[0]]

    def entry(self, path: str) -> IsoEntry:
        try:
            return self.entries[normalize(path)]
        except KeyError:
            raise DiscError(f"File '{path}' could not be found inside the disc image") from None

    def read_file(self, path: str) -> bytes:
        entry = self.entry(path)
        if entry.directory:
            raise DiscError(f"'{path}' is a directory")
        return self.image.read_file_data(entry.lba, entry.size)


def _write_both(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)
    struct.pack_into(">I", data, offset + 4, value)


class _Replacement(NamedTuple):
    entry: IsoEntry
    contents: bytes
    new_count: int

    @property
    def start(self) -> int:
        return self.entry.lba

    @property
    def end(self) -> int:
        return self.entry.lba + self.entry.sector_count

    @property
    def delta(self) -> int:
        return self.new_count - self.entry.sector_count


class IsoRebuilder:
    def __init__(self, iso: Iso9660) -> None:
        self.iso = iso
        self.image = iso.image
        self.replacements: dict[str, bytes] = {}

    def replace_file(self, path: str, contents: bytes) -> None:
        entry = self.iso.entry(path)
        if entry.directory:
            raise DiscError(f"Cannot replace directory '{path}'")
        if entry.sector_count > 0 and is_form2(self.image.read_sector(entry.lba)):
            raise DiscError(f"Cannot replace '{path}' since it is not a regular data file")
        self.replacements[normalize(path)] = bytes(contents)

    def _sorted_replacements(self) -> list[_Replacement]:
        result = []
        for path, contents in self.replacements.items():
            entry = self.iso.entry(path)
            needed = (len(contents) + DATA_SIZE - 1) // DATA_SIZE
            # Never shrink extents: a smaller file is padded inside its original space to avoid moving other files
            result.append(_Replacement(entry, contents, max(needed, entry.sector_count)))
        result.sort(key=lambda r: r.start)
        for previous, current in zip(result, result[1:]):
            if current.start < previous.end:
                raise DiscError("Overlapping files cannot be replaced at the same time")
        return result

    @staticmethod
    def _map_lba(sorted_replacements: list[_Replacement], old_lba: int) -> int:
        return old_lba + sum(r.delta for r in sorted_replacements if old_lba >= r.end)

    def write(self, output_bin: str) -> str:
        """Writes the modified image to the given path along with a matching .cue file, and returns the cue path"""
        sorted_replacements = self._sorted_replacements()
        by_start = {r.start: r for r in sorted_replacements}
        metadata = self._build_metadata_sectors(sorted_replacements)
        image = self.image

        with open(output_bin, "wb") as out:
            old_lba = new_lba = 0
            while old_lba < image.sector_count:
                replacement = by_start.get(old_lba)
                if replacement is not None:
                    if new_lba != self._map_lba(sorted_replacements, old_lba):
                        raise DiscError("Sector mapping mismatch")
                    originals = image.read_sectors(old_lba, replacement.entry.sector_count)
                    contents = replacement.contents
                    for i in range(replacement.new_count):
                        offset = i * DATA_SIZE
                        length = max(0, min(DATA_SIZE, len(contents) - offset))
                        last = i == replacement.new_count - 1
                        submode = SUBMODE_DATA | (SUBMODE_EOF | SUBMODE_EOR if last else 0)
                        original = (originals[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE]
                                    if i < replacement.entry.sector_count else None)
                        out.write(build_form1(new_lba, contents, offset, length, submode, original))
                        new_lba += 1
                    old_lba = replacement.end
                    continue

                # Copy a batch of untouched sectors, stopping before the next replaced extent
                batch_end = min(image.sector_count, old_lba + BATCH_SECTORS)
                for r in sorted_replacements:
                    if old_lba < r.start < batch_end:
                        batch_end = r.start
                count = batch_end - old_lba
                raw = image.read_sectors(old_lba, count)
                shift = new_lba - old_lba
                for i in range(count):
                    sector = bytearray(raw[i * SECTOR_SIZE:(i + 1) * SECTOR_SIZE])
                    user_data = metadata.get(old_lba + i)
                    if user_data is not None:
                        sector[DATA_OFFSET:DATA_OFFSET + DATA_SIZE] = user_data
                        set_address(sector, new_lba)
                        finalize_form1(sector)
                    elif shift != 0 and has_sync_pattern(sector):
                        if sector[15] != 2:
                            raise DiscError(f"Unsupported sector mode {sector[15]} at LBA {old_lba + i}")
                        # For Mode 2 sectors, EDC and ECC don't cover the header: updating the address is enough
                        set_address(sector, new_lba)
                    out.write(sector)
                    new_lba += 1
                old_lba = batch_end

        return self._write_cue(output_bin)

    @staticmethod
    def _write_cue(output_bin: str) -> str:
        file_name = os.path.basename(output_bin)
        cue_path = os.path.splitext(output_bin)[0] + ".cue"
        cue = f'FILE "{file_name}" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n'
        with open(cue_path, "w", encoding="ascii", newline="") as file:
            file.write(cue)
        return cue_path

    # ------------------------------------------------------------------------------------------------------
    #  Filesystem metadata update
    # ------------------------------------------------------------------------------------------------------

    def _update_record(self, data: bytearray, offset: int, sorted_replacements: list[_Replacement],
                       new_sizes: dict[int, int]) -> None:
        lba = struct.unpack_from("<I", data, offset + 2)[0]
        new_size = new_sizes.get(lba)
        is_directory = bool(data[offset + 25] & 0x02)
        if new_size is not None and not is_directory:
            _write_both(data, offset + 10, new_size)
        _write_both(data, offset + 2, self._map_lba(sorted_replacements, lba))

    def _build_metadata_sectors(self, sorted_replacements: list[_Replacement]) -> dict[int, bytes]:
        """Builds the updated user data of every sector holding filesystem metadata, keyed by source address"""
        iso, image = self.iso, self.image
        sectors: dict[int, bytes] = {}
        new_sizes = {r.start: len(r.contents) for r in sorted_replacements}

        pvd = bytearray(iso.pvd)
        _write_both(pvd, 80, self._map_lba(sorted_replacements, iso.volume_space_size))
        tables = iso.path_table_lbas
        for index, (offset, endian) in enumerate(((140, "<"), (144, "<"), (148, ">"), (152, ">"))):
            if tables[index]:
                struct.pack_into(endian + "I", pvd, offset, self._map_lba(sorted_replacements, tables[index]))
        self._update_record(pvd, 156, sorted_replacements, new_sizes)
        sectors[PVD_LBA] = bytes(pvd)

        # Path tables (L tables store little-endian LBAs, M tables big-endian LBAs)
        table_sectors = (iso.path_table_size + DATA_SIZE - 1) // DATA_SIZE
        for index, lba in enumerate(tables):
            if not lba:
                continue
            endian = ">" if index >= 2 else "<"
            table = bytearray(b"".join(image.read_user_data(lba + s) for s in range(table_sectors)))
            offset = 0
            while offset < iso.path_table_size:
                name_length = table[offset]
                if name_length == 0:
                    break
                old = struct.unpack_from(endian + "I", table, offset + 2)[0]
                struct.pack_into(endian + "I", table, offset + 2, self._map_lba(sorted_replacements, old))
                offset += 8 + name_length + (name_length % 2)
            for s in range(table_sectors):
                sectors[lba + s] = bytes(table[s * DATA_SIZE:(s + 1) * DATA_SIZE])

        # Directory records
        for directory in (e for e in iso.entries.values() if e.directory):
            for s in range(directory.sector_count):
                lba = directory.lba + s
                data = bytearray(image.read_user_data(lba))
                offset = 0
                while offset < DATA_SIZE:
                    length = data[offset]
                    if length == 0:
                        break
                    self._update_record(data, offset, sorted_replacements, new_sizes)
                    offset += length
                sectors[lba] = bytes(data)

        for r in sorted_replacements:
            if any(lba in sectors for lba in range(r.start, r.end)):
                raise DiscError("A replaced file overlaps filesystem structures")
        return sectors
