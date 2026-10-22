"""Builds the patched game from the patch file written during generation."""
import os
import zipfile
from typing import Any

from worlds.Files import APAutoPatchInterface

from . import pypatch

PATCH_DATA_FILE = "alundra.json"


class AlundraPatch(APAutoPatchInterface):
    game = "Alundra"
    patch_file_ending = ".apalundra"
    # The patcher writes a .bin next to it, the .cue is what the emulator opens
    result_file_ending = ".cue"

    patch_data: bytes = b""

    def __init__(self, *args: Any, patch_data: bytes = b"", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.patch_data = patch_data

    def write_contents(self, opened_zipfile: zipfile.ZipFile) -> None:
        super().write_contents(opened_zipfile)
        opened_zipfile.writestr(PATCH_DATA_FILE, self.patch_data)

    def read_contents(self, opened_zipfile: zipfile.ZipFile) -> dict[str, Any]:
        manifest = super().read_contents(opened_zipfile)
        self.patch_data = opened_zipfile.read(PATCH_DATA_FILE)
        return manifest

    def patch(self, target: str) -> None:
        """Patches the player's disc image and writes the result as `target` (a .cue file next to a .bin file)"""
        from . import AlundraWorld

        self.read()
        disc = AlundraWorld.settings.rom_file.resolve()
        if not os.path.isfile(disc):
            raise FileNotFoundError(f"Set alundra_settings.rom_file in your host.yaml: '{disc}' doesn't exist")
        pypatch.build_game(str(disc), self.patch_data, os.path.splitext(target)[0] + ".bin")
        if not os.path.isfile(target):
            raise FileNotFoundError(f"The patcher didn't write '{target}'")
