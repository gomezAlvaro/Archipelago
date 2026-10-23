import asyncio
import struct
import unittest

from .. import core, data


class FakeMemory:
    """A tiny RAM with the layout of a patched game"""

    def __init__(self) -> None:
        self.ram = bytearray(0x200000)
        self.write_at(core.MAILBOX, core.MAILBOX_MAGIC)

    def write_at(self, address: int, data: bytes) -> None:
        self.ram[address & 0x1FFFFF:(address & 0x1FFFFF) + len(data)] = data

    def set_flag(self, flag: int) -> None:
        self.ram[(core.FLAGS_BASE & 0x1FFFFF) + (flag >> 3)] |= 1 << (flag & 7)

    async def read(self, address: int, size: int) -> bytes:
        start = address & 0x1FFFFF
        return bytes(self.ram[start:start + size])

    async def write(self, address: int, data: bytes) -> None:
        self.write_at(address, data)

    def game_processes_mailbox(self) -> None:
        """What the patched game does once per frame"""
        base = core.MAILBOX & 0x1FFFFF
        item = struct.unpack_from("<I", self.ram, base + 4)[0]
        if item:
            self.inventory.append(item)
            struct.pack_into("<I", self.ram, base + 4, 0)
        struct.pack_into("<I", self.ram, base + 12, 0)

    inventory: list[int]


def run(coroutine):
    return asyncio.run(coroutine)


FIRST_LOCATION = next(iter(data.LOCATIONS.values()))
SWORD = data.ITEMS["Sword"]


def make_sync() -> core.AlundraSync:
    return core.AlundraSync(1, lambda item_id: f"item {item_id}", lambda player: f"player {player}")


class TestSync(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = FakeMemory()
        self.memory.inventory = []
        self.memory.set_flag(core.HUD_FLAG)
        self.memory.write_at(core.INVENTORY_BASE + 4 + 2, b"\1\0")  # a started game holds its starting items
        self.sync = make_sync()
        self.sync.last_message_time = 0
        self.sync.stable_polls = core.STABLE_POLLS

    def test_waits_for_a_patched_game(self) -> None:
        memory = FakeMemory()
        memory.write_at(core.MAILBOX, b"\0\0\0\0")
        result = run(self.sync.step(memory, [], set()))
        self.assertNotEqual(result.status, "ok")

    def test_ignores_the_noise_left_while_loading(self) -> None:
        memory = FakeMemory()
        memory.write_at(core.MAILBOX, core.MAILBOX_MAGIC)
        memory.write_at(core.FLAGS_BASE, b"\xFF" * core.FLAGS_SIZE)
        memory.write_at(core.INVENTORY_BASE, bytes(range(1, 200)))
        sync = make_sync()
        for _ in range(core.STABLE_POLLS + 2):
            result = run(sync.step(memory, [], set()))
            self.assertEqual(result.status, "Waiting for a game to start")
            self.assertFalse(result.new_locations)

    def test_trusts_a_started_game_after_a_few_polls(self) -> None:
        memory = FakeMemory()
        memory.write_at(core.MAILBOX, core.MAILBOX_MAGIC)
        memory.set_flag(core.HUD_FLAG)
        memory.write_at(core.INVENTORY_BASE + 4 + 2, b"\1\0")
        sync = make_sync()
        statuses = [run(sync.step(memory, [], set())).status for _ in range(core.STABLE_POLLS)]
        self.assertEqual(statuses, ["Waiting for a game to start"] * (core.STABLE_POLLS - 1) + ["ok"])

    def test_waits_for_a_running_game(self) -> None:
        memory = FakeMemory()
        result = run(self.sync.step(memory, [], set()))
        self.assertEqual(result.status, "Waiting for a game to start")

    def test_detects_opened_chests_once(self) -> None:
        location_id, _, flag = FIRST_LOCATION
        self.assertFalse(run(self.sync.step(self.memory, [], set())).new_locations)
        self.memory.set_flag(flag)
        self.assertEqual(run(self.sync.step(self.memory, [], set())).new_locations, {location_id})
        self.assertFalse(run(self.sync.step(self.memory, [], set())).new_locations)

    def test_does_not_resend_known_locations(self) -> None:
        location_id, _, flag = FIRST_LOCATION
        self.memory.set_flag(flag)
        self.assertFalse(run(self.sync.step(self.memory, [], {location_id})).new_locations)

    def test_delivers_items_of_other_players_once(self) -> None:
        received = [core.ReceivedItem(SWORD[0], 12345, 2)]
        run(self.sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [SWORD[2]])
        self.sync.last_message_time = 0
        run(self.sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [SWORD[2]])

    def test_the_item_count_is_stored_in_the_game(self) -> None:
        received = [core.ReceivedItem(SWORD[0], 12345, 2)]
        run(self.sync.step(self.memory, received, set()))
        counter = struct.unpack("<H", run(self.memory.read(core.COUNTER_ADDRESS, 2)))[0]
        self.assertEqual(counter, 1)
        # A client that restarts doesn't give the items again
        self.memory.game_processes_mailbox()
        restarted = make_sync()
        run(restarted.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [SWORD[2]])

    def test_does_not_give_items_a_chest_already_gave(self) -> None:
        location_id, _, flag = FIRST_LOCATION
        received = [core.ReceivedItem(SWORD[0], location_id, 1)]
        run(self.sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [])

    def test_gives_items_found_by_the_server(self) -> None:
        received = [core.ReceivedItem(SWORD[0], -1, 0)]
        run(self.sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [SWORD[2]])

    def test_opening_a_chest_shows_no_extra_message(self) -> None:
        _, _, flag = FIRST_LOCATION
        self.memory.set_flag(flag)
        run(self.sync.step(self.memory, [], set()))
        self.assertEqual(run(self.memory.read(core.MAILBOX + 12, 4)), b"\0\0\0\0")

    def test_the_starting_inventory_is_not_given_twice(self) -> None:
        sync = core.AlundraSync(1, lambda item_id: "Dagger" if item_id == data.ITEMS["Dagger"][0] else "Sword",
                                lambda player: f"player {player}")
        received = [core.ReceivedItem(data.ITEMS["Dagger"][0], -2, 0)]
        run(sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [])

    def test_goal(self) -> None:
        self.assertFalse(run(self.sync.step(self.memory, [], set())).goal_reached)
        self.memory.write_at(core.MAILBOX + 16, struct.pack("<I", 1))
        self.assertTrue(run(self.sync.step(self.memory, [], set())).goal_reached)

    def test_text_is_plain_ascii(self) -> None:
        self.assertEqual(core.clean_text("Ünïcode ~ \ ok"), b"?n?code ? ? ok")
        self.assertEqual(len(core.clean_text("x" * 500)), core.MESSAGE_MAX_LENGTH)
