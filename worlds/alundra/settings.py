import settings


class AlundraSettings(settings.Group):
    class RomFile(settings.UserFilePath):
        """File name of your Alundra (USA) v1.1 disc image (the .bin file, not the .cue)"""
        description = "Alundra (USA) (v1.1) disc image"

    rom_file: RomFile = RomFile("Alundra (USA) (v1.1).bin")
