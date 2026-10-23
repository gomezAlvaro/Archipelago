"""
Builds the patched game in pure Python, without the Java randomizer.

The game files edited by the randomizer are mostly the same for every Archipelago seed: they are stored as binary
patches against the files of the original disc (see ``patch_data`` and ``scripts/ap/build_patch_data.py`` in the
randomizer repository). What depends on the seed is applied on top of them: the item of every location, and the names
of the slot and of the multiworld, which the client reads from the game.
"""
import json
import pkgutil
import struct
import zlib
from typing import Any

from . import data
from .disc import CdImage, DiscError, Iso9660, IsoRebuilder

PATCH_DATA = "patch_data"
EXPECTED_IMAGE_SIZE = 600359760

# Item the game gives for items that belong to other worlds (the game ignores it)
ITEM_PLACEHOLDER = 0x1E

MAILBOX_MAGIC = b"ALAP"
SLOT_NAME_OFFSET, SEED_NAME_OFFSET, MAILBOX_SIZE = 0x60, 0x80, 0xC0

RUNS_MAGIC = b"ARUN"

# Name the game shows for the items of other worlds. It is also what the patch data holds in the text naming the item
# of a location, which is replaced by the name of the actual item here.
PLACEHOLDER_NAME = "Archipelago item"

# Dialogues naming the item of a location: map -> {string ID: locations whose item name is in that string, in order}
ROULETTE, SHOOTING, ARENA = ("Riverside Casino: 'Wheel of Fortune' chest", "Riverside Casino: 'Hit Your Mark' chest",
                             "Riverside Casino: 'Slime Smash' chest")
ITEM_TEXTS: dict[int, dict[int, list[str]]] = {
    474: {44: [ROULETTE]},  # Casino roulette
    473: {20: [SHOOTING]},  # Casino shooting range
    472: {80: [ARENA]},  # Casino slime arena
    394: {22: [ARENA], 23: [SHOOTING], 24: [ROULETTE]},  # Riverside bar
}


def _resource(name: str) -> bytes:
    content = pkgutil.get_data(__name__, f"{PATCH_DATA}/{name}")
    if content is None:
        raise FileNotFoundError(f"{PATCH_DATA}/{name} is missing from the Alundra world")
    return content


def available() -> bool:
    """Whether this world comes with the data needed to build the game in Python"""
    try:
        _resource("manifest.json")
    except (FileNotFoundError, OSError):
        return False
    return True


def apply_runs(original: bytes, patch: bytes) -> bytearray:
    """Applies a list of ``(offset, bytes)`` replacements to a file of the same size"""
    if patch[:4] != RUNS_MAGIC:
        raise DiscError("Invalid patch data")
    result = bytearray(original)
    count, = struct.unpack_from("<I", patch, 4)
    position = 8
    for _ in range(count):
        offset, length = struct.unpack_from("<II", patch, position)
        position += 8
        result[offset:offset + length] = patch[position:position + length]
        position += length
    return result


def _ascii_field(text: str, size: int) -> bytes:
    return text.encode("ascii", "replace")[:size - 1].ljust(size, b"\0")


def _read_long(datas: bytearray, address: int) -> int:
    return struct.unpack_from("<I", datas, address)[0]


def apply_item_texts(datas: bytearray, names: dict[str, str]) -> None:
    """
    Replaces the placeholder with the name of the actual item in the dialogues naming the item of a location. The
    strings of a room are stored at the end of its data as a table of 16-bit offsets terminated by 0xFFFF followed by
    null-terminated strings, and are written back in the same space.
    """
    for map_id, edits in ITEM_TEXTS.items():
        room = _read_long(datas, 0x28 + map_id * 4)
        next_room = _read_long(datas, 0x28 + map_id * 4 + 4) or len(datas)
        strings_size = next_room - room - _read_long(datas, room + 0x18)
        start = room + _read_long(datas, room + 0x18)

        strings = []
        address = start
        while (offset := struct.unpack_from("<H", datas, address)[0]) != 0xFFFF:
            end = datas.index(0, start + offset)
            strings.append(datas[start + offset:end].decode("latin-1"))
            address += 2

        for string_id, locations in edits.items():
            text = strings[string_id]
            for location in locations:
                if PLACEHOLDER_NAME not in text:
                    raise DiscError("The patch data of the Alundra world is corrupted (item text not found)")
                text = text.replace(PLACEHOLDER_NAME, names[location], 1)
            strings[string_id] = text

        encoded = bytearray(b"\xFF" * ((len(strings) + 1) * 2))
        for index, text in enumerate(strings):
            struct.pack_into("<H", encoded, index * 2, len(encoded) & 0xFFFF)
            encoded += text.encode("latin-1") + b"\0"
        if len(encoded) > strings_size:
            raise DiscError("String data is bigger than in the original room")
        datas[start:start + strings_size] = encoded.ljust(strings_size, b"\0")


def apply_seed(files: dict[str, bytearray], manifest: dict[str, Any], patch: dict[str, Any]) -> None:
    """Writes what depends on the seed inside the files patched with the data of the world"""
    addresses = json.loads(_resource("seed_sources.json"))
    datas = files[manifest["datas_file"]]
    names = {}
    for location in patch["locations"]:
        source = addresses[location["name"]]
        # The dialogues have room for the placeholder's length only
        names[location["name"]] = (location["item"] or location.get("name_of_item") or PLACEHOLDER_NAME) \
            .encode("ascii", "replace").decode("ascii")[:len(PLACEHOLDER_NAME)]
        item_id = ITEM_PLACEHOLDER if location["item"] is None else data.ITEMS[location["item"]][2]
        # An item of another player's Alundra world keeps its own sprite
        sprite_id = data.ITEMS[location["sprite"]][2] if location.get("sprite") else item_id
        for address in source["addresses"]:
            datas[address] = item_id
        for address in source["sprites"]:
            datas[address] = (sprite_id + 0x1E) & 0xFF
    apply_item_texts(datas, names)

    exe = files[manifest["exe_file"]]
    mailbox = manifest["mailbox_offset"]
    if exe[mailbox:mailbox + 4] != MAILBOX_MAGIC:
        raise DiscError("The patch data of the Alundra world is corrupted (mailbox not found)")
    exe[mailbox + SLOT_NAME_OFFSET:mailbox + SEED_NAME_OFFSET] = _ascii_field(
        patch["player_name"], SEED_NAME_OFFSET - SLOT_NAME_OFFSET)
    exe[mailbox + SEED_NAME_OFFSET:mailbox + MAILBOX_SIZE] = _ascii_field(
        patch["seed_name"], MAILBOX_SIZE - SEED_NAME_OFFSET)


def build_game(input_bin: str, patch_json: bytes, output_bin: str) -> str:
    """Writes the patched disc image (a .bin and its .cue), and returns the path of the .cue file"""
    import bsdiff4

    manifest = json.loads(_resource("manifest.json"))
    patch = json.loads(patch_json)

    with CdImage(input_bin) as image:
        if image.sector_count * 2352 != EXPECTED_IMAGE_SIZE:
            raise DiscError(f"'{input_bin}' is not an Alundra (USA) v1.1 disc image (unexpected size)")
        iso = Iso9660(image)

        files: dict[str, bytearray] = {}
        for name, info in manifest["files"].items():
            original = iso.read_file(name)
            if len(original) != info["original_size"] or zlib.crc32(original) != info["original_crc"]:
                raise DiscError(f"{name} doesn't match the one of an Alundra (USA) v1.1 disc image")
            patch_file = _resource(info["patch"])
            if info["format"] == "runs":
                files[name] = apply_runs(original, patch_file)
            else:
                files[name] = bytearray(bsdiff4.patch(original, patch_file))

        apply_seed(files, manifest, patch)

        rebuilder = IsoRebuilder(iso)
        for name, contents in files.items():
            rebuilder.replace_file(name, contents)
        return rebuilder.write(output_bin)
