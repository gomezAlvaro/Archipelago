"""
Talks to a running, Archipelago-patched Alundra through its RAM. This module doesn't know how the memory is accessed
(BizHawk, PCSX-Redux...): it only needs an object with ``read`` and ``guarded_write`` coroutines using PlayStation RAM
addresses (0x80000000 based).

RAM layout (see PatchArchipelago.java in the randomizer):

* the mailbox, where the client asks the game to give an item and/or show a message, and where the game tells that the
  ending started
* the story flags, saved with the game: opening a chest sets the flag whose number is stored in the chest
* an unused inventory slot, whose quantity is used to count the items already given by the client, so this count is
  saved along with the game
"""
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol

from . import data, exe_hooks

MAILBOX = 0x8001F800 - 0xC0
MAILBOX_MAGIC = b"ALAP"
MAILBOX_SIZE = 0xC0
MESSAGE_OFFSET = 20
SLOT_NAME_OFFSET = exe_hooks.SLOT_NAME_OFFSET
SEED_NAME_OFFSET = exe_hooks.SEED_NAME_OFFSET
VERSION_OFFSET = exe_hooks.VERSION_OFFSET
MESSAGE_MAX_LENGTH = 0x60 - MESSAGE_OFFSET - 1

FLAGS_BASE = 0x801DD334
FLAGS_SIZE = 0x100
HUD_FLAG = 0x67E  # set as soon as a game is running (new game or loaded save)

# Quantity of this unused item (halfword at +2 of its 4 bytes entry) holds the number of items given by the client
INVENTORY_BASE = 0x801DD82E
COUNTER_ITEM_ID = 0x3A
COUNTER_ADDRESS = INVENTORY_BASE + COUNTER_ITEM_ID * 4 + 2
INVENTORY_SIZE = COUNTER_ITEM_ID * 4
assert COUNTER_ADDRESS == exe_hooks.ITEM_COUNTER_ADDRESS
# Number of consecutive polls where the memory must look like a started game before it is trusted
STABLE_POLLS = 3

# Location ID the server uses for the items of the starting inventory
STARTING_INVENTORY_LOCATION = -2

# Time to leave a message on screen before showing the next one, in seconds
MESSAGE_DURATION = 4.5


class Memory(Protocol):
    async def read(self, address: int, size: int) -> bytes: ...

    async def guarded_write(self, writes: list[tuple[int, bytes]], guards: list[tuple[int, bytes]]) -> bool:
        """Does all the writes at once (between two frames of the game) if the memory holds all the guards"""
        ...


class ReceivedItem(NamedTuple):
    item_id: int  # Archipelago item ID
    location_id: int
    player: int


@dataclass
class SyncResult:
    status: str = "ok"
    new_locations: set[int] = field(default_factory=set)
    goal_reached: bool = False


def flag_is_set(flags: bytes, flag: int) -> bool:
    return bool(flags[flag >> 3] & (1 << (flag & 7)))


def clean_text(text: str) -> bytes:
    """The game's font only has plain ASCII, and some characters are control codes."""
    text = "".join(c if 32 <= ord(c) < 127 and c not in "\\~" else "?" for c in text)
    return text[:MESSAGE_MAX_LENGTH].encode("ascii")


class AlundraSync:
    """State of the synchronization of one game with one Archipelago slot."""

    def __init__(self, slot: int,
                 item_name: Callable[[int], str], player_name: Callable[[int], str]) -> None:
        self.slot = slot
        self.item_name = item_name
        self.player_name = player_name
        self.location_flags = {location_id: flag for location_id, _, flag in data.LOCATIONS.values()}
        self.game_item_ids = {item_id: game_id for item_id, _, game_id in data.ITEMS.values()}
        self.location_ids = set(self.location_flags)
        self.checked: set[int] = set()
        self.last_message_time = 0.0
        self.stable_polls = 0

    def reset(self) -> None:
        """To call when the client (re)connects to the server"""
        self.checked.clear()
        self.stable_polls = 0

    def _is_given_by_game(self, item: ReceivedItem) -> bool:
        """
        Items of our own world found in our own locations were already given by the chest that held them,
        and the starting inventory is part of the new game.
        """
        if item.location_id == STARTING_INVENTORY_LOCATION:
            return self.item_name(item.item_id) in data.STARTING_INVENTORY
        return item.player == self.slot and item.location_id in self.location_ids

    async def step(self, memory: Memory, received: list[ReceivedItem], already_checked: set[int]) -> SyncResult:
        mailbox = await memory.read(MAILBOX, MAILBOX_SIZE)
        if mailbox[:4] != MAILBOX_MAGIC:
            self.stable_polls = 0
            return SyncResult("The game is not running or was not patched for Archipelago")

        flags = await memory.read(FLAGS_BASE, FLAGS_SIZE)
        if not flag_is_set(flags, HUD_FLAG):
            self.stable_polls = 0  # back to the title screen: the next game must be stable again before it is trusted
            return SyncResult("Waiting for a game to start")
        # While a game is loading, this memory holds other data (the flags and the inventory look like noise). A real
        # inventory has small quantities (the upper byte of each one is 0) and always holds the starting items.
        inventory = await memory.read(INVENTORY_BASE, INVENTORY_SIZE)
        if any(inventory[3::4]) or not any(inventory[2::4]):
            self.stable_polls = 0
        else:
            self.stable_polls += 1
        if self.stable_polls < STABLE_POLLS:
            return SyncResult("Waiting for a game to start")

        result = SyncResult()
        result.goal_reached = struct.unpack_from("<I", mailbox, 16)[0] == 1

        for location_id, flag in self.location_flags.items():
            if location_id not in self.checked and flag_is_set(flags, flag):
                self.checked.add(location_id)
                if location_id not in already_checked:
                    result.new_locations.add(location_id)

        await self._deliver(memory, mailbox, received)
        return result

    async def _deliver(self, memory: Memory, mailbox: bytes, received: list[ReceivedItem]) -> None:
        pending_item, _, pending_message = struct.unpack_from("<III", mailbox, 4)
        if pending_item or pending_message:
            return  # the game didn't process the previous request yet

        counter = struct.unpack("<H", await memory.read(COUNTER_ADDRESS, 2))[0]
        # Every write below is guarded by the counter it was computed from and by the free mailbox, so nothing is
        # written if a save was loaded or the game took a request meanwhile
        free_mailbox = [(MAILBOX + 4, bytes(4)), (MAILBOX + 12, bytes(4))]
        skipped = counter
        while skipped < len(received) and self._is_given_by_game(received[skipped]):
            skipped += 1
        if skipped != counter:
            if not await memory.guarded_write([(COUNTER_ADDRESS, struct.pack("<H", skipped))],
                                              [(COUNTER_ADDRESS, struct.pack("<H", counter)), *free_mailbox]):
                return
            counter = skipped

        if counter >= len(received) or time.monotonic() - self.last_message_time < MESSAGE_DURATION:
            return

        item = received[counter]
        game_item = self.game_item_ids.get(item.item_id) or exe_hooks.NO_GAME_ITEM
        sender = "Archipelago" if item.player == 0 else self.player_name(item.player)
        text = clean_text(f"Received {self.item_name(item.item_id)} from {sender}")

        # The game gives the item and counts it in the save in the same frame, and shows the message once it can.
        # The item is written last: it is what tells the game that the request is complete.
        if await memory.guarded_write(
                [(MAILBOX + MESSAGE_OFFSET, text + b"\0"),
                 (MAILBOX + 8, struct.pack("<I", counter + 1)),
                 (MAILBOX + 12, struct.pack("<I", 1)),
                 (MAILBOX + 4, struct.pack("<I", game_item))],
                [(COUNTER_ADDRESS, struct.pack("<H", counter)), *free_mailbox]):
            self.last_message_time = time.monotonic()
