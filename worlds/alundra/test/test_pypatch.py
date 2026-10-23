import os
import struct
import unittest

from .. import data, disc, exe_hooks, pypatch


class TestRuns(unittest.TestCase):
    def test_apply_runs(self) -> None:
        patch = (pypatch.RUNS_MAGIC + struct.pack("<I", 2) + struct.pack("<II", 1, 2) + b"ab"
                 + struct.pack("<II", 6, 1) + b"z")
        self.assertEqual(pypatch.apply_runs(b"0123456789", patch), bytearray(b"0ab345z789"))

    def test_invalid_patch(self) -> None:
        with self.assertRaises(disc.DiscError):
            pypatch.apply_runs(b"0123", b"nope")

    def test_ascii_field(self) -> None:
        self.assertEqual(exe_hooks._ascii_field("Luzbe", 8), b"Luzbe\0\0\0")
        self.assertEqual(exe_hooks._ascii_field("A" * 20, 8), b"A" * 7 + b"\0")


class TestLocationItems(unittest.TestCase):
    def other_player(self, sprite: str) -> dict:
        return {"name": "x", "item": None, "name_of_item": sprite, "recipient": "Bob", "sprite": sprite}

    def test_a_chest_shows_another_players_alundra_item(self) -> None:
        sword = data.ITEMS["Sword"][2]
        self.assertEqual(pypatch.location_item_ids(self.other_player("Sword"), False), (sword, sword))

    def test_a_chest_never_holds_a_capped_consumable_of_another_player(self) -> None:
        for name in ("Herbs", "Strength Elixyr", "Magic Elixyr", "Wonder Essence", "Strength Tonic"):
            self.assertIn(data.ITEMS[name][2], exe_hooks.CAPPED_ITEMS)
            item_id, _ = pypatch.location_item_ids(self.other_player(name), False)
            self.assertEqual(item_id, pypatch.ITEM_PLACEHOLDER, name)

    def test_own_items_are_stored_as_they_are(self) -> None:
        herbs = data.ITEMS["Herbs"][2]
        self.assertEqual(pypatch.location_item_ids({"name": "x", "item": "Herbs"}, False), (herbs, herbs))


class TestSectors(unittest.TestCase):
    def test_reusing_error_correction_matches_computing_it(self) -> None:
        data = bytes(range(256)) * 8
        computed = disc.build_form1(1234, data, 0, 2048, disc.SUBMODE_DATA)
        reused = disc.build_form1(1234, data, 0, 2048, disc.SUBMODE_DATA, bytes(computed))
        self.assertEqual(computed, reused)
        # The same data at another address only changes the header
        moved = disc.build_form1(1300, data, 0, 2048, disc.SUBMODE_DATA, bytes(computed))
        self.assertEqual(moved[16:], computed[16:])
        self.assertNotEqual(moved[12:15], computed[12:15])

    def test_error_correction_is_recomputed_for_other_data(self) -> None:
        first = disc.build_form1(5, b"\1" * 2048, 0, 2048, disc.SUBMODE_DATA)
        second = disc.build_form1(5, b"\2" * 2048, 0, 2048, disc.SUBMODE_DATA, bytes(first))
        self.assertNotEqual(first[0x818:], second[0x818:])

    def test_address(self) -> None:
        sector = bytearray(disc.SECTOR_SIZE)
        disc.set_address(sector, 0)  # LBA 0 is at 00:02:00
        self.assertEqual(bytes(sector[12:15]), b"\x00\x02\x00")


@unittest.skipUnless(os.environ.get("ALUNDRA_DISC") and pypatch.available(), "needs ALUNDRA_DISC and the patch data")
class TestBuildGame(unittest.TestCase):
    def test_build(self) -> None:
        import json
        import tempfile

        from .. import data
        names = list(data.LOCATIONS)
        patch = {"seed_name": "seed", "player_name": "Quester",
                 "locations": [{"name": n, "item": "Herbs"} if i % 2 else
                                 {"name": n, "item": None, "name_of_item": "Hookshot of the Wind Waker, long",
                                  "recipient": "Link",
                                  **({"sprite": "Herbs"} if i % 4 == 0 else {})}
                                 for i, n in enumerate(names)]}
        with tempfile.TemporaryDirectory() as directory:
            cue = pypatch.build_game(os.environ["ALUNDRA_DISC"], json.dumps(patch).encode(),
                                     os.path.join(directory, "out.bin"))
            self.assertTrue(os.path.isfile(cue))
            with disc.CdImage(os.path.join(directory, "out.bin")) as image:
                exe = disc.Iso9660(image).read_file("ALUN_CD.EXE")
            self.assertIn(b"Quester\0", exe)
