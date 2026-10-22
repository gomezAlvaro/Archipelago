"""Builds the patched game from the patch file written during generation (in Python, or with the Java randomizer)."""
import logging
import os
import subprocess
import tempfile
import zipfile
from typing import TYPE_CHECKING, Any

from worlds.Files import APAutoPatchInterface

from . import pypatch

if TYPE_CHECKING:
    from .settings import AlundraSettings

logger = logging.getLogger("Alundra")

PATCH_DATA_FILE = "alundra.json"


class AlundraPatch(APAutoPatchInterface):
    game = "Alundra"
    patch_file_ending = ".apalundra"
    # The randomizer writes a .bin next to it, the .cue is what the emulator opens
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
        settings = AlundraWorld.settings
        disc = settings.rom_file.resolve()
        if not os.path.isfile(disc):
            raise FileNotFoundError(f"Set alundra_settings.rom_file in your host.yaml: '{disc}' doesn't exist")
        output = os.path.splitext(target)[0] + ".bin"

        if not settings.use_java_randomizer and pypatch.available():
            pypatch.build_game(str(disc), self.patch_data, output)
        else:
            self._patch_with_java(settings, str(disc), output)
        if not os.path.isfile(target):
            raise FileNotFoundError(f"The patcher didn't write '{target}'")

    def _patch_with_java(self, settings: "AlundraSettings", disc: str, output: str) -> None:
        jar = settings.randomizer_jar.resolve()
        if not os.path.isfile(jar):
            raise FileNotFoundError(f"Set alundra_settings.randomizer_jar in your host.yaml: '{jar}' doesn't exist")

        with tempfile.TemporaryDirectory() as directory:
            patch_file = os.path.join(directory, PATCH_DATA_FILE)
            with open(patch_file, "wb") as file:
                file.write(self.patch_data)

            command = ["java", "-jar", str(jar), f"--archipelago={patch_file}", f"--input={disc}",
                       f"--outputrom={output}"]
            logger.info("Running %s", " ".join(command))
            result = subprocess.run(command, capture_output=True, text=True)
            logger.info(result.stdout)
            if result.returncode != 0:
                raise RuntimeError(f"The randomizer failed:{BS}n{result.stdout}{BS}n{result.stderr}")
