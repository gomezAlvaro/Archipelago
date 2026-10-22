import settings


class AlundraSettings(settings.Group):
    class RomFile(settings.UserFilePath):
        """File name of your Alundra (USA) v1.1 disc image (the .bin file, not the .cue)"""
        description = "Alundra (USA) (v1.1) disc image"
        required = False

    class RandomizerJar(settings.UserFilePath):
        """File name of the Alundra randomizer program, which builds your patched game"""
        description = "Alundra randomizer (alundra-randomizer.jar)"
        required = False

    class UseJavaRandomizer(settings.Bool):
        """Build the game with the Java randomizer (needs Java and the jar below) instead of with the built-in Python patcher"""

    rom_file: RomFile = RomFile("Alundra (USA) (v1.1).bin")
    randomizer_jar: RandomizerJar = RandomizerJar("alundra-randomizer.jar")
    use_java_randomizer: UseJavaRandomizer | bool = False
