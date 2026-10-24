from .. import data
from .bases import AlundraTestBase


class TestGeneration(AlundraTestBase):
    def test_pool_matches_locations(self) -> None:
        self.assertEqual(len(self.multiworld.get_unfilled_locations(1)), len(self.multiworld.itempool))

    def test_not_beatable_without_items(self) -> None:
        self.assertBeatable(False)

    def test_items_gating_only_vanilla_rewards_are_not_progression(self) -> None:
        # They open Merrick's trades, Jess's chest and Lurvy's repair, which keep their vanilla items
        for name in ("Gilded Falcon", "Small Key", "Broken Armor"):
            self.assertFalse(self.world.create_item(name).advancement, name)

    def test_every_gilded_falcon_is_in_the_pool(self) -> None:
        # Merrick's last trade needs 50: 3 from the casino, the rest in the pool
        falcons = sum(item.name == "Gilded Falcon" for item in self.multiworld.itempool)
        self.assertEqual(falcons, data.ITEM_POOL["Gilded Falcon"] - 3)
