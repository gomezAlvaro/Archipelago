import asyncio
import struct
import unittest

from .. import core, data, exe_hooks


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

    async def guarded_write(self, writes: list[tuple[int, bytes]], guards: list[tuple[int, bytes]]) -> bool:
        if any(run_sync(self.read(address, len(expected))) != expected for address, expected in guards):
            return False
        for address, value in writes:
            self.write_at(address, value)
        return True

    def counter(self) -> int:
        return struct.unpack_from("<H", self.ram, core.COUNTER_ADDRESS & 0x1FFFFF)[0]

    def game_processes_mailbox(self, message_box_open: bool = False) -> None:
        """What the patched game does once per frame"""
        base = core.MAILBOX & 0x1FFFFF
        item, target = struct.unpack_from("<II", self.ram, base + 4)
        if item:
            struct.pack_into("<I", self.ram, base + 4, 0)
            if self.counter() + 1 == target:
                struct.pack_into("<H", self.ram, core.COUNTER_ADDRESS & 0x1FFFFF, target)
                if item != exe_hooks.NO_GAME_ITEM:
                    self.inventory.append(item)
            else:
                struct.pack_into("<I", self.ram, base + 12, 0)
        if not message_box_open and struct.unpack_from("<I", self.ram, base + 12)[0]:
            struct.pack_into("<I", self.ram, base + 12, 0)
            self.messages.append(bytes(self.ram[base + 20:base + 96]).split(b"\0")[0].decode())

    inventory: list[int]
    messages: list[str]


def run_sync(coroutine):
    """Runs a coroutine that never waits, from inside a running event loop"""
    try:
        coroutine.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("The coroutine waited")


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
        self.memory.messages = []
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
        # The game counts the item when it gives it, not the client when it asks for it
        self.assertEqual(self.memory.counter(), 0)
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.counter(), 1)
        # A client that restarts doesn't give the items again
        restarted = make_sync()
        run(restarted.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [SWORD[2]])

    def test_a_request_for_another_save_is_dropped(self) -> None:
        received = [core.ReceivedItem(SWORD[0], 12345, 2)]
        run(self.sync.step(self.memory, received, set()))
        # An older save that already received the item is loaded before the game takes the request
        self.memory.write_at(core.COUNTER_ADDRESS, b"\1\0")
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [])
        self.assertEqual(self.memory.messages, [])
        self.assertEqual(self.memory.counter(), 1)

    def test_nothing_is_written_if_a_save_is_loaded_meanwhile(self) -> None:
        received = [core.ReceivedItem(SWORD[0], 12345, 2)]
        memory = self.memory

        class LoadsASave(FakeMemory):
            async def read(self, address: int, size: int) -> bytes:
                data = await memory.read(address, size)
                if address == core.COUNTER_ADDRESS:
                    memory.write_at(core.COUNTER_ADDRESS, b"\5\0")
                return data

        loading = LoadsASave()
        loading.ram = memory.ram
        run(self.sync.step(loading, received, set()))
        self.assertEqual(run(memory.read(core.MAILBOX + 4, 12)), bytes(12))
        self.assertEqual(memory.counter(), 5)

    def test_waits_for_the_open_message_box(self) -> None:
        received = [core.ReceivedItem(SWORD[0], 12345, 2), core.ReceivedItem(SWORD[0], 12346, 2)]
        run(self.sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox(message_box_open=True)
        self.assertEqual(self.memory.inventory, [SWORD[2]])
        self.assertEqual(self.memory.messages, [])
        # The next item waits for the message
        self.sync.last_message_time = 0
        run(self.sync.step(self.memory, received, set()))
        self.assertEqual(run(self.memory.read(core.MAILBOX + 4, 4)), bytes(4))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.messages, [f"Received item {SWORD[0]} from player 2"])

    def test_items_without_a_game_item_are_still_counted(self) -> None:
        received = [core.ReceivedItem(123, 12345, 2)]
        run(self.sync.step(self.memory, received, set()))
        self.memory.game_processes_mailbox()
        self.assertEqual(self.memory.inventory, [])
        self.assertEqual(self.memory.counter(), 1)
        self.assertEqual(len(self.memory.messages), 1)

    def test_back_to_the_title_screen_the_next_game_must_be_stable_again(self) -> None:
        self.memory.ram[(core.FLAGS_BASE & 0x1FFFFF) + (core.HUD_FLAG >> 3)] = 0
        run(self.sync.step(self.memory, [], set()))
        self.memory.set_flag(core.HUD_FLAG)
        self.assertEqual(run(self.sync.step(self.memory, [], set())).status, "Waiting for a game to start")

    def test_does_not_give_items_a_chest_already_gave(self) -> None:
        location_id, _, _ = FIRST_LOCATION
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

    def test_goal_is_seen_after_the_game_left_its_ending(self) -> None:
        # A client connecting during the credits or at the title screen after them
        self.memory.write_at(core.MAILBOX + 16, struct.pack("<I", 1))
        self.memory.ram[(core.FLAGS_BASE & 0x1FFFFF) + (core.HUD_FLAG >> 3)] = 0
        self.assertTrue(run(make_sync().step(self.memory, [], set())).goal_reached)
        self.memory.set_flag(core.HUD_FLAG)
        self.assertTrue(run(make_sync().step(self.memory, [], set())).goal_reached)

    def test_text_is_plain_ascii(self) -> None:
        self.assertEqual(core.clean_text(r"Ünïcode ~ \ ok 🎮"), b"Unicode ? ? ok ?")
        self.assertEqual(len(core.clean_text("x" * 500)), core.MESSAGE_MAX_LENGTH)
