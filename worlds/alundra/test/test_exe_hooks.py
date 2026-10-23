import struct
import unittest

from .. import exe_hooks
from ..mips import A0, RA, SP, T0, T1, ZERO, MipsCode

CODE_START = 0x8001F000
BODY_SIZE = 0x70000


def words(code: MipsCode) -> list[int]:
    return list(struct.unpack(f"<{len(code) // 4}I", code.to_bytes()))


def fake_exe() -> bytes:
    """An executable like the one the randomizer builds: its mailbox is reserved in front of the original code"""
    exe = bytearray(exe_hooks.HEADER_SIZE + BODY_SIZE)
    struct.pack_into("<IIII", exe, 0x10, 0x8008DDA0, 0, CODE_START, BODY_SIZE)
    mailbox = exe_hooks.HEADER_SIZE + exe_hooks.MAILBOX_ADDRESS - CODE_START
    exe[mailbox:mailbox + 4] = exe_hooks.MAILBOX_MAGIC
    return bytes(exe)


class TestMips(unittest.TestCase):
    def test_encodings(self) -> None:
        code = MipsCode()
        code.addiu(SP, SP, -0x20).sw(RA, SP, 0x18).lw(T1, T0, 4, False).lui(T0, 0x8002).jr(RA, False)
        self.assertEqual(words(code), [0x27BDFFE0, 0xAFBF0018, 0x8D090004, 0x3C088002, 0x03E00008])

    def test_halfword_encodings(self) -> None:
        code = MipsCode()
        code.lhu(A0, T0, 2, False).sh(A0, T0, 2)
        self.assertEqual(words(code), [0x95040002, 0xA5040002])

    def test_load_and_branch_delay_slots(self) -> None:
        code = MipsCode()
        code.lw(T1, T0, 4).beq(T1, ZERO, "end").label("end")
        self.assertEqual(words(code), [0x8D090004, 0, 0x11200001, 0])

    def test_branches_resolve_both_ways(self) -> None:
        code = MipsCode()
        code.beq(T0, T1, "forward").nop().label("forward").bne(T0, T1, "forward")
        self.assertEqual(words(code)[0] & 0xFFFF, 2)
        self.assertEqual(words(code)[3] & 0xFFFF, 0xFFFF)  # -1: back to the instruction before the delay slot

    def test_set_value(self) -> None:
        self.assertEqual(words(MipsCode().set_(A0, 0x801AC6F8))[0], 0x3C04801B)  # the lower half is negative
        self.assertEqual(words(MipsCode().set_(A0, 0x34)), [0x24040034])

    def test_undefined_label(self) -> None:
        with self.assertRaises(ValueError):
            MipsCode().beq(T0, T1, "nowhere").to_bytes()


class TestInstall(unittest.TestCase):
    def setUp(self) -> None:
        self.exe = fake_exe()
        self.result = exe_hooks.install(self.exe, "Quester", "seed", [(0x680, "Sent Hookshot to Link")])
        self.code_start, = struct.unpack_from("<I", self.result, 0x18)

    def offset(self, address: int) -> int:
        return exe_hooks.HEADER_SIZE + address - self.code_start

    def test_header_grows_by_whole_steps(self) -> None:
        code_size, = struct.unpack_from("<I", self.result, 0x1C)
        expansion = CODE_START - self.code_start
        self.assertEqual(expansion % exe_hooks.EXPANSION_STEP, 0)
        self.assertEqual(code_size, BODY_SIZE + expansion)
        self.assertEqual(len(self.result), len(self.exe) + expansion)

    def test_original_code_is_not_moved(self) -> None:
        # The original text is still at the same RAM address
        probe = exe_hooks.HEADER_SIZE + 0x80020000 - CODE_START
        self.assertEqual(self.result[self.offset(0x80020000):self.offset(0x80020000) + 16],
                         self.exe[probe:probe + 16])

    def test_mailbox_has_the_names(self) -> None:
        mailbox = self.offset(exe_hooks.MAILBOX_ADDRESS)
        self.assertEqual(self.result[mailbox:mailbox + 4], exe_hooks.MAILBOX_MAGIC)
        self.assertEqual(self.result[mailbox + 0x60:mailbox + 0x68], b"Quester\0")
        self.assertEqual(self.result[mailbox + 0x80:mailbox + 0x85], b"seed\0")
        self.assertEqual(struct.unpack_from("<I", self.result, mailbox + exe_hooks.VERSION_OFFSET)[0],
                         exe_hooks.PROTOCOL_VERSION)

    def test_a_long_seed_name_leaves_the_version(self) -> None:
        result = exe_hooks.install(self.exe, "a", "9" * 100, [])
        mailbox = self.offset(exe_hooks.MAILBOX_ADDRESS)
        self.assertEqual(struct.unpack_from("<I", result, mailbox + exe_hooks.VERSION_OFFSET)[0],
                         exe_hooks.PROTOCOL_VERSION)

    def test_hooks_call_injected_code(self) -> None:
        for call in (exe_hooks.FRAME_HOOK_CALL, exe_hooks.ENDING_HOOK_CALL, exe_hooks.GIVE_ITEM_CALL,
                     exe_hooks.ITEM_MESSAGE_CALL):
            word, = struct.unpack_from("<I", self.result, self.offset(call))
            target = (word & 0x03FFFFFF) << 2 | 0x80000000
            self.assertEqual(word >> 26, 3)  # jal
            self.assertTrue(self.code_start <= target < CODE_START, hex(target))

    def test_table_of_texts(self) -> None:
        position = self.result.index(struct.pack("<I", 0x680) + b"Sent Hookshot to Link\0")
        entry = self.result[position:position + exe_hooks.TEXT_ENTRY_SIZE]
        self.assertEqual(len(entry), exe_hooks.TEXT_ENTRY_SIZE)
        # The table ends with an empty entry
        self.assertEqual(self.result[position + exe_hooks.TEXT_ENTRY_SIZE:position + exe_hooks.TEXT_ENTRY_SIZE + 4],
                         bytes(4))

    def test_long_texts_are_cut(self) -> None:
        result = exe_hooks.install(self.exe, "a", "b", [(1, "x" * 200)])
        position = result.index(struct.pack("<I", 1) + b"x")
        text = result[position + 4:position + exe_hooks.TEXT_ENTRY_SIZE]
        self.assertEqual(text.index(0), exe_hooks.TEXT_MAX_LENGTH)

    def test_executable_without_mailbox(self) -> None:
        exe = bytearray(self.exe)
        mailbox = exe_hooks.HEADER_SIZE + exe_hooks.MAILBOX_ADDRESS - CODE_START
        exe[mailbox:mailbox + 4] = bytes(4)
        with self.assertRaises(exe_hooks.ExeError):
            exe_hooks.install(bytes(exe), "a", "b", [])
