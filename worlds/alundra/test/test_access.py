from .bases import AlundraTestBase


class TestGeneration(AlundraTestBase):
    def test_pool_matches_locations(self) -> None:
        self.assertEqual(len(self.multiworld.get_unfilled_locations(1)), len(self.multiworld.itempool))

    def test_not_beatable_without_items(self) -> None:
        self.assertBeatable(False)
