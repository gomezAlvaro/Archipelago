import json
import os
import tempfile
import unittest

from ..patch import AlundraPatch


class TestPatchContainer(unittest.TestCase):
    def test_round_trip(self) -> None:
        data = json.dumps({"game": "Alundra", "locations": []}).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "AP_test_P1_Quester.apalundra")
            AlundraPatch(path, player=1, player_name="Quester", patch_data=data).write()

            loaded = AlundraPatch(path)
            loaded.read()
            self.assertEqual(loaded.patch_data, data)
            self.assertEqual((loaded.player, loaded.player_name), (1, "Quester"))
