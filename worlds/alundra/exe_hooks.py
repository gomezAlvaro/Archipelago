"""
Adds to the executable of the game what lets it talk to the Archipelago client: a mailbox in RAM and code hooked to
the game. This is a port of the Java randomizer's Archipelago patch, which no longer has to be run for it.

Mailbox layout (little-endian words, at MAILBOX_ADDRESS):
    +0   magic "ALAP"
    +4   item ID the client wants the game to give (0 when the mailbox is free, NO_GAME_ITEM to give nothing)
    +8   value of the saved item counter once the item is given: the game gives the item and stores this value only
         when its counter is one less, so a request made for another save (one loaded meanwhile) is dropped
    +12  1 when the text at +20 must be shown in a message box (the game clears it once shown, and waits while
         another box is open)
    +16  set to 1 by the game when the ending starts
    +20  null-terminated message text, up to 75 characters
    +96  name of the Archipelago slot (UTF-8, null-terminated, up to 67 bytes), used by the client to log in
    +164 name of the multiworld seed (null-terminated, up to 23 characters)
    +188 PROTOCOL_VERSION (0 in games built before it existed)

Once per frame, unless the inventory menu is open, the game checks the mailbox, gives the requested item through its
own item pickup routine and counts it in the save, shows the message if there is one and frees the mailbox. A chest
holding an item of another world gives nothing and opens a box whose text is baked into a table of (story flag of the
chest, text).
"""
import struct
import unicodedata

from .mips import A0, A1, RA, S1, SP, T0, T1, T2, V0, ZERO, MipsCode

MAILBOX_ADDRESS = 0x8001F740
MAILBOX_SIZE = 0xC0
MAILBOX_MAGIC = b"ALAP"
# The slot name has room for 16 characters (the most Archipelago allows) of up to 4 UTF-8 bytes each. Seed names are
# 20 digits.
SLOT_NAME_OFFSET, SEED_NAME_OFFSET, VERSION_OFFSET = 0x60, 0xA4, 0xBC
# Version of the way the game and the client talk through the mailbox: a client refuses a game of another version
PROTOCOL_VERSION = 3
MESSAGE_OFFSET = 20

# Unused item ID used for items belonging to other worlds
ITEM_PLACEHOLDER = 0x1E
# Asks the game to count a received item without giving anything
NO_GAME_ITEM = 0xFFFFFFFF
# Quantity (halfword) of an unused inventory item, saved with the game: the number of items received so far
ITEM_COUNTER_ADDRESS = 0x801DD82E + 0x3A * 4 + 2

# The function called once per frame while the game is running: its call to a subroutine is hooked
FRAME_HOOK_CALL = 0x8002BFB4
FRAME_HOOK_ORIGINAL_TARGET = 0x80048FB0
# Applies the effects of an item given by a chest or a script: A0 = player data, A1 = item ID
FUN_OBTAIN_ITEM = 0x800344D4
# Where a chest or script gives an item, and where it opens its "you got this item" box (A0 = text, A1 = 1)
GIVE_ITEM_CALL = 0x8007EB4C
ITEM_MESSAGE_CALL = 0x8007EB14
FUN_SHOW_MESSAGE = 0x8004608C
# Second argument of FUN_SHOW_MESSAGE: 0 freezes the world (player and enemies) until the box is closed, like a
# dialogue; 1, used by chests, doesn't (a chest holds the player still with its own pickup animation)
MESSAGE_FREEZES_WORLD = 0
# When a chest opens it asks whether the player can carry its item (A0 = item ID): a full stack keeps it shut
CAN_CARRY_CALL = 0x8008264C
FUN_CAN_CARRY = 0x80034458
# Returns non-zero while a message box is open (the game checks it before opening its own)
FUN_MESSAGE_BUSY = 0x80045F8C
# The game's text windows: 13 entries of 0x1C bytes, whose first halfword has bit 0 set while the window is open.
# Window 6 is the inventory menu. A message box opened over it makes the game add the letters of the box twice to
# the same drawing list, which then loops: the GPU never finishes a frame and the game crawls (one frame every few
# seconds, kept by save states) until the console is reset.
TEXT_WINDOWS = 0x80107570
TEXT_WINDOW_SIZE = 0x1C
MENU_WINDOW_FLAGS = TEXT_WINDOWS + 6 * TEXT_WINDOW_SIZE
FUN_PLAY_SOUND = 0x8004A128
ITEM_JINGLE_SOUND = 0x34
# Start of the branch of the game starting its ending: its first call is hooked to flag the goal as reached
ENDING_HOOK_CALL = 0x8002C70C
ENDING_HOOK_ORIGINAL_TARGET = 0x8004AD18
PLAYER_DATA = 0x801AC6F8
# In the state of a chest giving an item (S1): the object holding it, and the offset of its story flag in it
ITEM_OBJECT_OFFSET, ITEM_OBJECT_FLAG_OFFSET = 0x280, 0x40

# Entries of the text table: a 32-bit story flag (0 ends the table) followed by a null-terminated text
TEXT_ENTRY_SIZE = 80
TEXT_MAX_LENGTH = TEXT_ENTRY_SIZE - 4 - 1

HEADER_SIZE = 0x800
# The executable grows by multiples of this size, in front of its original code
EXPANSION_STEP = 0x800
# Lowest address the injected code can reach (below it the BIOS keeps its data)
LOWEST_ADDRESS = 0x80010000


class ExeError(Exception):
    pass


def _ascii_field(text: str, size: int) -> bytes:
    return text.encode("ascii", "replace")[:size - 1].ljust(size, b"\0")


def _utf8_field(text: str, size: int) -> bytes:
    encoded = text.encode("utf-8")
    if len(encoded) >= size:
        # Cut on a character boundary
        encoded = encoded[:size - 1].decode("utf-8", "ignore").encode("utf-8")
    return encoded.ljust(size, b"\0")


def clean_text(text: str) -> str:
    """
    Text the game can show: its font only has plain ASCII, and "~" and "\\" are control codes. Accented letters lose
    their accent, other characters become "?".
    """
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return "".join(c if 32 <= ord(c) < 127 and c not in "\\~" else "?" for c in text)


def text_entry(flag: int, text: str) -> bytes:
    return struct.pack("<I", flag) + _ascii_field(clean_text(text), TEXT_ENTRY_SIZE - 4)


def _search_table(code: MipsCode, table_address: int, not_found: str, found: str, s1_is_chest: bool = False) -> None:
    """
    Looks for the story flag of the chest being opened in the text table, and branches to one of the labels with T1
    pointing at its entry when found. S1 is the object of the item the chest gives, or the chest itself.
    """
    if s1_is_chest:
        code.lw(T0, S1, ITEM_OBJECT_FLAG_OFFSET)
    else:
        code.lw(T0, S1, ITEM_OBJECT_OFFSET)
        code.lw(T0, T0, ITEM_OBJECT_FLAG_OFFSET)
    code.set_(T1, table_address)
    code.label("search_" + not_found)
    code.lw(T2, T1, 0)
    code.beq(T2, ZERO, not_found)
    code.beq(T2, T0, found)
    code.addiu(T1, T1, TEXT_ENTRY_SIZE)
    code.bra("search_" + not_found)


def install(exe: bytes, slot_name: str, seed_name: str, texts: list[tuple[int, str]]) -> bytearray:
    """
    Returns the executable with the Archipelago code added.

    :param exe: the executable, as built by the randomizer with its Archipelago mailbox but without its hooks
    :param texts: (story flag of a chest, text of its box) for the chests holding an item of another world
    """
    exe = bytearray(exe)
    code_start, = struct.unpack_from("<I", exe, 0x18)

    def offset(address: int) -> int:
        return HEADER_SIZE + address - code_start

    # The randomizer reserves the mailbox: it is the first block it injects, right below its free RAM
    mailbox_offset = offset(MAILBOX_ADDRESS)
    if mailbox_offset < HEADER_SIZE or exe[mailbox_offset:mailbox_offset + 4] != MAILBOX_MAGIC:
        raise ExeError("The executable has no Archipelago mailbox")
    exe[mailbox_offset + SLOT_NAME_OFFSET:mailbox_offset + SEED_NAME_OFFSET] = _utf8_field(
        slot_name, SEED_NAME_OFFSET - SLOT_NAME_OFFSET)
    exe[mailbox_offset + SEED_NAME_OFFSET:mailbox_offset + VERSION_OFFSET] = _ascii_field(
        seed_name, VERSION_OFFSET - SEED_NAME_OFFSET)
    struct.pack_into("<I", exe, mailbox_offset + VERSION_OFFSET, PROTOCOL_VERSION)

    # Blocks are injected in front of the code, each one below the previous ones
    blocks: list[bytes] = []
    size = 0

    def inject(block: bytes) -> int:
        nonlocal size
        blocks.append(block)
        size += len(block)
        return code_start - size

    def hook(call_address: int, original_target: int, target: int) -> None:
        site = slice(offset(call_address), offset(call_address) + 4)
        if exe[site] != MipsCode().jal(original_target, False).to_bytes():
            raise ExeError(f"Unexpected code at {call_address:08X}")
        exe[site] = MipsCode().jal(target, False).to_bytes()

    tick = MipsCode()
    tick.addiu(SP, SP, -0x20)
    tick.sw(RA, SP, 0x18)

    # Nothing is given or shown while the inventory menu is open: the request waits in the mailbox
    tick.set_(T0, MENU_WINDOW_FLAGS)
    tick.lhu(T1, T0, 0)
    tick.andi(T1, T1, 1)
    tick.bne(T1, ZERO, "done")

    tick.set_(T0, MAILBOX_ADDRESS)
    tick.lw(T1, T0, 4)
    tick.beq(T1, ZERO, "no_item")
    tick.sw(ZERO, T0, 4)

    # The request is for this save only if its counter is one less than the requested value
    tick.lw(T2, T0, 8)
    tick.set_(A0, ITEM_COUNTER_ADDRESS)
    tick.lhu(A1, A0, 0)
    tick.addiu(A1, A1, 1)
    tick.bne(A1, T2, "stale")
    tick.sh(A1, A0, 0)

    # The game obtains the item exactly like when picking it up
    tick.beq_(T1, NO_GAME_ITEM, "no_item")
    tick.set_(A0, PLAYER_DATA)
    tick.move_(A1, T1)
    tick.jal(FUN_OBTAIN_ITEM)
    tick.bra("no_item")

    # Its message is dropped with it
    tick.label("stale")
    tick.sw(ZERO, T0, 12)

    tick.label("no_item")
    tick.set_(T0, MAILBOX_ADDRESS)
    tick.lw(T1, T0, 12)
    tick.beq(T1, ZERO, "done")
    tick.jal(FUN_MESSAGE_BUSY)
    tick.bne(V0, ZERO, "done")
    tick.set_(T0, MAILBOX_ADDRESS)
    tick.sw(ZERO, T0, 12)
    tick.addiu(A0, T0, MESSAGE_OFFSET)
    tick.addiu(A1, ZERO, MESSAGE_FREEZES_WORLD)
    tick.jal(FUN_SHOW_MESSAGE)
    tick.addiu(A0, ZERO, ITEM_JINGLE_SOUND)
    tick.jal(FUN_PLAY_SOUND)

    tick.label("done")
    tick.lw(RA, SP, 0x18)
    tick.addiu(SP, SP, 0x20)
    # Tail call: the hooked function returns directly to the original caller
    tick.j(FRAME_HOOK_ORIGINAL_TARGET)
    hook(FRAME_HOOK_CALL, FRAME_HOOK_ORIGINAL_TARGET, inject(tick.to_bytes()))

    # The goal is reached when the ending starts
    goal = MipsCode()
    goal.set_(T0, MAILBOX_ADDRESS)
    goal.addiu(T1, ZERO, 1)
    goal.sw(T1, T0, 16)
    goal.j(ENDING_HOOK_ORIGINAL_TARGET)
    hook(ENDING_HOOK_CALL, ENDING_HOOK_ORIGINAL_TARGET, inject(goal.to_bytes()))

    # Items of other worlds must not be added to the inventory, and their box tells what they are. They are the chests
    # of the table. A chest holds either the placeholder or an item of Alundra whose sprite is shown for it. A chest
    # of another world without an entry still gives nothing and shows the game's own box.
    table = b"".join(text_entry(flag, text) for flag, text in texts) + bytes(4)
    table_address = inject(table)

    # The player can always "carry" what a chest of the table holds, since nothing is given: a full stack of the item
    # shown in it must not keep it shut
    can_carry = MipsCode()
    _search_table(can_carry, table_address, "check", "yes", s1_is_chest=True)
    can_carry.label("yes")
    can_carry.addiu(V0, ZERO, 1)
    can_carry.jr(RA)
    can_carry.label("check")
    can_carry.j(FUN_CAN_CARRY)
    hook(CAN_CARRY_CALL, FUN_CAN_CARRY, inject(can_carry.to_bytes()))

    give = MipsCode()
    give.beq_(A1, ITEM_PLACEHOLDER, "skip")
    _search_table(give, table_address, "give", "skip")
    give.label("skip")
    give.jr(RA)
    give.label("give")
    give.j(FUN_OBTAIN_ITEM)
    hook(GIVE_ITEM_CALL, FUN_OBTAIN_ITEM, inject(give.to_bytes()))

    message = MipsCode()
    _search_table(message, table_address, "show", "found")
    message.label("found")
    message.addiu(A0, T1, 4)
    message.label("show")
    message.j(FUN_SHOW_MESSAGE)
    hook(ITEM_MESSAGE_CALL, FUN_SHOW_MESSAGE, inject(message.to_bytes()))

    # Prepend the blocks, the last one injected being the lowest, and update the header
    expansion = -(-size // EXPANSION_STEP) * EXPANSION_STEP
    if code_start - expansion < LOWEST_ADDRESS:
        raise ExeError("Not enough free RAM for the Archipelago code")
    prepended = bytes([0xFF]) * (expansion - size) + b"".join(reversed(blocks))
    exe[HEADER_SIZE:HEADER_SIZE] = prepended
    struct.pack_into("<I", exe, 0x18, code_start - expansion)
    code_size, = struct.unpack_from("<I", exe, 0x1C)
    struct.pack_into("<I", exe, 0x1C, code_size + expansion)
    return exe
