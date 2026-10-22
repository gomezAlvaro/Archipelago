from collections.abc import Mapping
from typing import Any

from BaseClasses import Entrance, Item, ItemClassification, Location, Region, Tutorial
from worlds.AutoWorld import WebWorld, World

from . import data
from .options import AlundraOptions

CLASSIFICATIONS = {
    "progression": ItemClassification.progression,
    "useful": ItemClassification.useful,
    "filler": ItemClassification.filler,
}


class AlundraItem(Item):
    game = "Alundra"


class AlundraLocation(Location):
    game = "Alundra"


class AlundraWeb(WebWorld):
    tutorials = [Tutorial(
        "Multiworld Setup Guide",
        "A guide to playing Alundra with Archipelago.",
        "English",
        "setup_en.md",
        "setup/en",
        ["Luzbe"],
    )]


class AlundraWorld(World):
    """
    Alundra is an action RPG for the PlayStation. Dreamwalker Alundra arrives in the village of Inoa,
    where the villagers are haunted by nightmares, and explores dungeons, caves and the dreams of its people.
    """

    game = "Alundra"
    web = AlundraWeb()

    options_dataclass = AlundraOptions
    options: AlundraOptions

    item_name_to_id = {name: item_id for name, (item_id, _) in data.ITEMS.items()}
    location_name_to_id = {name: location_id for name, (location_id, _) in data.LOCATIONS.items()}
    item_name_groups = {"Crests": set(data.CRESTS)}

    origin_region_name = data.START_REGION

    def generate_early(self) -> None:
        self.entrance_names: dict[str, int] = {}

    def create_item(self, name: str) -> AlundraItem:
        return AlundraItem(name, CLASSIFICATIONS[data.ITEMS[name][1]], data.ITEMS[name][0], self.player)

    def create_regions(self) -> None:
        regions = {name: Region(name, self.player, self.multiworld) for name in data.REGIONS}

        for name, (_, region) in data.LOCATIONS.items():
            location = AlundraLocation(self.player, name, self.location_name_to_id[name], regions[region])
            if name in data.NO_PROGRESSION_LOCATIONS:
                location.item_rule = lambda item: not item.advancement
            regions[region].locations.append(location)

        for source, target, two_way, items, required_regions in data.PATHS:
            self._connect(regions, source, target, items, required_regions)
            if two_way:
                self._connect(regions, target, source, items, required_regions)

        victory = AlundraLocation(self.player, "Defeat Melzas", None, regions[data.GOAL_REGION])
        victory.place_locked_item(AlundraItem("Victory", ItemClassification.progression, None, self.player))
        regions[data.GOAL_REGION].locations.append(victory)

        # Regions are registered last so that the location cache sees every location
        self.multiworld.regions.extend(regions.values())

    def _connect(self, regions: dict[str, Region], source: str, target: str,
                 items: dict[str, int], required_regions: list[str]) -> None:
        player = self.player
        name = f"{source} -> {target}"
        # Several paths between the same nodes are alternatives to each other
        duplicates = self.entrance_names[name] = self.entrance_names.get(name, 0) + 1
        entrance = regions[source].connect(regions[target], name if duplicates == 1 else f"{name} ({duplicates})")
        if items or required_regions:
            entrance.access_rule = lambda state: (
                all(state.has(item, player, count) for item, count in items.items())
                and all(state.can_reach_region(region, player) for region in required_regions)
            )
        for region in required_regions:
            self.multiworld.register_indirect_condition(regions[region], entrance)

    def create_items(self) -> None:
        for name in data.STARTING_INVENTORY:
            self.multiworld.push_precollected(self.create_item(name))

        pool = dict(data.ITEM_POOL)
        for location_name, item_name in data.FIXED_LOCATIONS.items():
            self.get_location(location_name).place_locked_item(self.create_item(item_name))
            pool[item_name] -= 1
        for crest in data.CRESTS:
            pool[crest] = 1

        itempool = [self.create_item(name) for name, count in pool.items() for _ in range(count)]
        unfilled = len(self.multiworld.get_unfilled_locations(self.player)) - len(itempool)
        itempool += [self.create_filler() for _ in range(unfilled)]
        self.multiworld.itempool += itempool

    def set_rules(self) -> None:
        self.multiworld.completion_condition[self.player] = lambda state: state.has("Victory", self.player)

    def get_filler_item_name(self) -> str:
        return self.random.choice(["Herbs", "10 Gilder", "30 Gilder"])

    def fill_slot_data(self) -> Mapping[str, Any]:
        return {}
