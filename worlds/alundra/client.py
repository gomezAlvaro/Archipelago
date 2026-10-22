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

    async def write(self, address: int, data: bytes) -> None:
        from worlds._bizhawk import write
        await write(self.ctx.bizhawk_ctx, [(address & 0x1FFFFF, data, PSX_RAM)])


class AlundraClient(BizHawkClient):
    game = "Alundra"
    system = "PSX"
    patch_suffix = ".apalundra"

    def __init__(self) -> None:
        super().__init__()
        self.sync: core.AlundraSync | None = None
        self.slot_name = ""
        self.seed_name = ""
        self.scouted = False

    async def validate_rom(self, ctx: "BizHawkClientContext") -> bool:
        from worlds._bizhawk import RequestFailedError, read
        try:
            mailbox = (await read(ctx.bizhawk_ctx, [(core.MAILBOX & 0x1FFFFF, core.MAILBOX_SIZE, PSX_RAM)]))[0]
        except RequestFailedError:
            return False  # Should be verified again on the next pass
        if mailbox[:4] != core.MAILBOX_MAGIC:
            return False

        self.slot_name = mailbox[0x60:0x80].split(b"\0")[0].decode("ascii", "replace")
        self.seed_name = mailbox[0x80:0xC0].split(b"\0")[0].decode("ascii", "replace")
        ctx.game = self.game
        ctx.items_handling = 0b111
        ctx.want_slot_data = False
        self.sync = None
        return True

    async def set_auth(self, ctx: "BizHawkClientContext") -> None:
        ctx.auth = self.slot_name

    def on_package(self, ctx: "BizHawkClientContext", cmd: str, args: dict[str, Any]) -> None:
        if cmd == "Connected":
            if self.seed_name and ctx.seed_name and self.seed_name != ctx.seed_name:
                logger.warning("This game was built for another multiworld (%s), not for %s",
                               self.seed_name, ctx.seed_name)
            self.sync = None
            self.scouted = False
        elif cmd == "LocationInfo" and self.sync:
            for item in args["locations"]:
                if item.player != ctx.slot:
                    self.sync.scouted[item.location] = (ctx.item_names.lookup_in_slot(item.item, item.player),
                                                        ctx.player_names[item.player])

    async def game_watcher(self, ctx: "BizHawkClientContext") -> None:
        from worlds._bizhawk import ConnectionClosedError, RequestFailedError
        if ctx.slot is None:
            return

        if self.sync is None:
            self.sync = core.AlundraSync(
                ctx.slot,
                lambda item_id: ctx.item_names.lookup_in_slot(item_id, ctx.slot),
                lambda player: ctx.player_names[player])

        if not self.scouted:
            self.scouted = True
            await ctx.send_msgs([{"cmd": "LocationScouts", "locations": sorted(self.sync.location_ids),
                                  "create_as_hint": 0}])

        received = [core.ReceivedItem(item.item, item.location, item.player) for item in ctx.items_received]
        try:
            result = await self.sync.step(BizHawkMemory(ctx), received, set(ctx.checked_locations))
        except (RequestFailedError, ConnectionClosedError):
            return

        if result.new_locations:
            await ctx.send_msgs([{"cmd": "LocationChecks", "locations": sorted(result.new_locations)}])
        if result.goal_reached and not ctx.finished_game:
            ctx.finished_game = True
            await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
