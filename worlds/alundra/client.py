import logging
from typing import TYPE_CHECKING, Any

from NetUtils import ClientStatus
from worlds._bizhawk.client import BizHawkClient

from . import core

if TYPE_CHECKING:
    from worlds._bizhawk.context import BizHawkClientContext

logger = logging.getLogger("Client")

PSX_RAM = "MainRAM"


class BizHawkMemory:
    """RAM of a PlayStation game running in BizHawk"""

    def __init__(self, ctx: "BizHawkClientContext") -> None:
        self.ctx = ctx

    async def read(self, address: int, size: int) -> bytes:
        from worlds._bizhawk import read
        return (await read(self.ctx.bizhawk_ctx, [(address & 0x1FFFFF, size, PSX_RAM)]))[0]

    async def guarded_write(self, writes: list[tuple[int, bytes]], guards: list[tuple[int, bytes]]) -> bool:
        from worlds._bizhawk import guarded_write
        return await guarded_write(self.ctx.bizhawk_ctx,
                                   [(address & 0x1FFFFF, data, PSX_RAM) for address, data in writes],
                                   [(address & 0x1FFFFF, data, PSX_RAM) for address, data in guards])


class AlundraClient(BizHawkClient):
    game = "Alundra"
    system = "PSX"
    patch_suffix = ".apalundra"

    def __init__(self) -> None:
        super().__init__()
        self.sync: core.AlundraSync | None = None
        self.slot_name = ""
        self.seed_name = ""
        self.version_error_shown = False
        self.goal_sent = False

    async def validate_rom(self, ctx: "BizHawkClientContext") -> bool:
        from worlds._bizhawk import RequestFailedError, read
        try:
            mailbox = (await read(ctx.bizhawk_ctx, [(core.MAILBOX & 0x1FFFFF, core.MAILBOX_SIZE, PSX_RAM)]))[0]
        except RequestFailedError:
            return False  # Should be verified again on the next pass
        if mailbox[:4] != core.MAILBOX_MAGIC:
            return False
        version = int.from_bytes(mailbox[core.VERSION_OFFSET:core.VERSION_OFFSET + 4], "little")
        if version != core.exe_hooks.PROTOCOL_VERSION:
            if not self.version_error_shown:
                # An older game counts received items differently: it would be given the same items again and again
                logger.error("This Alundra game was patched by another version of the Alundra world (protocol %d, "
                             "this client needs %d). Patch the game again from its .apalundra file.",
                             version, core.exe_hooks.PROTOCOL_VERSION)
                self.version_error_shown = True
            return False

        self.slot_name = mailbox[core.SLOT_NAME_OFFSET:core.SEED_NAME_OFFSET].split(b"\0")[0].decode("utf-8", "replace")
        self.seed_name = mailbox[core.SEED_NAME_OFFSET:core.VERSION_OFFSET].split(b"\0")[0].decode("ascii", "replace")
        ctx.game = self.game
        ctx.items_handling = 0b111
        ctx.want_slot_data = False
        self.sync = None
        return True

    async def set_auth(self, ctx: "BizHawkClientContext") -> None:
        ctx.auth = self.slot_name

    def on_package(self, ctx: "BizHawkClientContext", cmd: str, args: dict[str, Any]) -> None:
        if cmd == "Connected":
            self.sync = None
            self.goal_sent = False

    def _built_for_another_multiworld(self, ctx: "BizHawkClientContext") -> bool:
        # The game holds as much of the seed name as fits in its mailbox
        return bool(self.seed_name and ctx.seed_name
                    and self.seed_name != ctx.seed_name[:core.VERSION_OFFSET - core.SEED_NAME_OFFSET - 1])

    async def game_watcher(self, ctx: "BizHawkClientContext") -> None:
        from worlds._bizhawk import RequestFailedError
        if ctx.slot is None:
            return
        if self._built_for_another_multiworld(ctx):
            # Its opened chests would be sent as checks of this multiworld, and its items counted against them
            logger.error("This game was built for another multiworld (%s), not for the room you joined (%s). "
                         "Load the game patched for this room, then connect again.", self.seed_name, ctx.seed_name)
            await ctx.disconnect()
            return

        if self.sync is None:
            self.sync = core.AlundraSync(
                ctx.slot,
                lambda item_id: ctx.item_names.lookup_in_slot(item_id, ctx.slot),
                lambda player: ctx.player_names[player])

        received = [core.ReceivedItem(item.item, item.location, item.player) for item in ctx.items_received]
        try:
            result = await self.sync.step(BizHawkMemory(ctx), received, set(ctx.checked_locations))
        except RequestFailedError:
            return

        if result.new_locations:
            await ctx.send_msgs([{"cmd": "LocationChecks", "locations": sorted(result.new_locations)}])
        if result.goal_reached and not self.goal_sent:
            # Sent again after every (re)connection: harmless if the server already has it, and not lost if the
            # connection dropped while sending it
            self.goal_sent = True
            ctx.finished_game = True
            await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
