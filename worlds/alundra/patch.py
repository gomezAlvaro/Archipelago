"""Builds the patched game from the patch file written during generation, using the Alundra randomizer program."""
import logging
import os
import subprocess

import Utils
from worlds.LauncherComponents import Component, SuffixIdentifier, Type, components, launch

logger = logging.getLogger("Alundra")


def build_game(patch_file: str) -> str:
    """Patches the player's disc image, returns the path of the new disc image."""
    from . import AlundraWorld

    settings = AlundraWorld.settings
    disc, jar = settings.rom_file.resolve(), settings.randomizer_jar.resolve()
    for path, what in ((disc, "rom_file"), (jar, "randomizer_jar")):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Set alundra_settings.{what} in your host.yaml: '{path}' doesn't exist")

    output = os.path.splitext(patch_file)[0] + ".bin"
    command = ["java", "-jar", str(jar), f"--archipelago={patch_file}", f"--input={disc}", f"--outputrom={output}"]
    logger.info("Running %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    logger.info(result.stdout)
    if result.returncode != 0:
        raise RuntimeError(f"The randomizer failed:\n{result.stdout}\n{result.stderr}")
    return output


def patch_game(*args: str) -> None:
    Utils.init_logging("AlundraPatcher")
    if not args:
        raise SystemExit("Open an .apalundra file with this tool to build your game.")
    output = build_game(args[0])
    logger.info("Your game is ready: %s (open the .cue file next to it in your emulator)", output)


def launch_patcher(*args: str) -> None:
    launch(patch_game, name="Alundra Patcher", args=args)


components.append(Component("Alundra Patcher", func=launch_patcher, component_type=Type.MISC,
                            file_identifier=SuffixIdentifier(".apalundra"),
                            description="Builds the Alundra game from an Archipelago patch file."))
