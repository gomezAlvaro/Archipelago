import json
import os
from collections.abc import Mapping
from typing import Any, ClassVar

from BaseClasses import Item, ItemClassification, Location, Region, Tutorial
from worlds.AutoWorld import WebWorld, World

from . import data
from .options import AlundraOptions
from .settings import AlundraSettings

from . import client as client  # registers the client in the launcher
from .patch import AlundraPatch

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
    settings_key = "alundra_settings"
    settings: ClassVar[AlundraSettings]

    item_name_to_id = {name: item_id for name, (item_id, _, _) in data.ITEMS.items()}
    location_name_to_id = {name: location_id for name, (location_id, _, _) in data.LOCATIONS.items()}
    item_name_groups = {"Crests": set(data.CRESTS)}

    origin_region_name = data.START_REGION

    def generate_early(self) -> None:
        self.entrance_names: dict[str, int] = {}

    def create_item(self, name: str) -> AlundraItem:
        item_id, classification, _ = data.ITEMS[name]
        return AlundraItem(name, CLASSIFICATIONS[classification], item_id, self.player)

    def create_regions(self) -> None:
        regions = {name: Region(name, self.player, self.multiworld) for name in data.REGIONS}

        for name, (location_id, region, _) in data.LOCATIONS.items():
            regions[region].locations.append(AlundraLocation(self.player, name, location_id, regions[region]))

        # Item sources the client cannot track keep their vanilla item
        for name, (region, item_name) in data.LOCKED_LOCATIONS.items():
            location = AlundraLocation(self.player, name, None, regions[region])
            item_id, classification, _ = data.ITEMS[item_name]
            location.place_locked_item(AlundraItem(item_name, CLASSIFICATIONS[classification], None, self.player))
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
        for crest in data.CRESTS:
            pool[crest] = pool.get(crest, 0) + 1
        # Items already placed in the locations that keep their vanilla item are not part of the pool
        for _, item_name in data.LOCKED_LOCATIONS.values():
            if pool.get(item_name, 0) > 0:
                pool[item_name] -= 1

        itempool = [self.create_item(name) for name, count in pool.items() for _ in range(count)]
        unfilled = len(self.multiworld.get_unfilled_locations(self.player)) - len(itempool)
        if unfilled < 0:
            # Too many items: drop filler
            filler = [item for item in itempool if item.classification == ItemClassification.filler]
            for item in filler[:-unfilled]:
                itempool.remove(item)
        else:
            itempool += [self.create_filler() for _ in range(unfilled)]
        self.multiworld.itempool += itempool

    def set_rules(self) -> None:
        self.multiworld.completion_condition[self.player] = lambda state: state.has("Victory", self.player)

    def get_filler_item_name(self) -> str:
        return self.random.choice(["Herbs", "10 Gilder", "30 Gilder"])

    def generate_output(self, output_directory: str) -> None:
        """Writes the file the player gives to the patcher in order to build their game."""
        locations = []
        for name in data.LOCATIONS:
            item = self.get_location(name).item
            if item.player == self.player:
                locations.append({"name": name, "item": item.name})
                continue
            # Items of other worlds are a placeholder which gives nothing, with the real name for the dialogues
            # naming it, and the sprite of the item when it is one of Alundra
            location = {"name": name, "item": None, "name_of_item": item.name}
            if item.game == self.game and item.name in data.ITEMS:
                location["sprite"] = item.name
            locations.append(location)

        patch_data = {
            "game": self.game,
            "format": 1,
            "seed_name": self.multiworld.seed_name,
            "player": self.player,
            "player_name": self.player_name,
            "locations": locations,
        }
        patch = AlundraPatch(os.path.join(output_directory, self.multiworld.get_out_file_name_base(self.player)
                                          + AlundraPatch.patch_file_ending),
                             player=self.player, player_name=self.player_name,
                             patch_data=json.dumps(patch_data).encode("utf-8"))
        patch.write()

    def fill_slot_data(self) -> Mapping[str, Any]:
        return {"version": 1}
