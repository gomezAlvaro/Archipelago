from dataclasses import dataclass

from Options import PerGameCommonOptions, Toggle


class VanillaCrests(Toggle):
    """
    Every crest stays in its original location: the boss rewards of Lars Crypt, Reptile Lair, Nirude's Lair,
    Torla Mountain and the Tower of the Great Tree, and the item revealed with Zolist's Stone in Nava's Keep.
    The crests are then never found in another world.
    """

    display_name = "Vanilla Crests"


@dataclass
class AlundraOptions(PerGameCommonOptions):
    vanilla_crests: VanillaCrests
