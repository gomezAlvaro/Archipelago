"""A tiny MIPS assembler producing little-endian R3000 machine code, enough to write the patches of the executable."""
import struct

REGISTERS = {name: code for code, name in enumerate(
    ["zero", "at", "v0", "v1", "a0", "a1", "a2", "a3", "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
     "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra"])}
ZERO, A0, A1, T0, T1, T2, T9, S1, SP, RA = (REGISTERS[name] for name in
                                            ("zero", "a0", "a1", "t0", "t1", "t2", "t9", "s1", "sp", "ra"))


class MipsCode:
    """
    Instructions are appended in order. Branches and jumps are followed by a no-op in their delay slot, and so are
    loads when asked to (the R3000 doesn't use a loaded register in the next instruction). Branches take the name of a
    label defined before or after them with `label`. Pseudo-instructions ending with an underscore may use `t9`.
    """

    def __init__(self) -> None:
        self.words: list[int] = []
        self.labels: dict[str, int] = {}
        self.pending: list[tuple[int, str]] = []

    def __len__(self) -> int:
        return len(self.words) * 4

    def to_bytes(self) -> bytes:
        if self.pending:
            raise ValueError(f"Undefined labels: {sorted({label for _, label in self.pending})}")
        return struct.pack(f"<{len(self.words)}I", *self.words)

    def _op(self, word: int) -> "MipsCode":
        self.words.append(word & 0xFFFFFFFF)
        return self

    def nop(self) -> "MipsCode":
        return self._op(0)

    def add(self, to: int, a: int, b: int) -> "MipsCode":
        return self._op(0x20 | (a << 21) | (b << 16) | (to << 11))

    def addiu(self, to: int, source: int, value: int) -> "MipsCode":
        return self._op(0x24000000 | (source << 21) | (to << 16) | (value & 0xFFFF))

    def lui(self, register: int, value: int) -> "MipsCode":
        return self._op(0x3C000000 | (register << 16) | (value & 0xFFFF))

    def lw(self, to: int, base: int, offset: int = 0, load_delay: bool = True) -> "MipsCode":
        self._op(0x8C000000 | (base << 21) | (to << 16) | (offset & 0xFFFF))
        return self.nop() if load_delay else self

    def sw(self, source: int, base: int, offset: int = 0) -> "MipsCode":
        return self._op(0xAC000000 | (base << 21) | (source << 16) | (offset & 0xFFFF))

    def _branch(self, opcode: int, label: str) -> "MipsCode":
        self.pending.append((len(self.words), label))
        self._op(opcode)
        self._resolve()
        return self.nop()

    def beq(self, a: int, b: int, label: str) -> "MipsCode":
        return self._branch(0x10000000 | (a << 21) | (b << 16), label)

    def bne(self, a: int, b: int, label: str) -> "MipsCode":
        return self._branch(0x14000000 | (a << 21) | (b << 16), label)

    def bra(self, label: str) -> "MipsCode":
        """Unconditional relative branch"""
        return self._branch(0x04010000, label)  # bgez zero

    def j(self, address: int, delay_nop: bool = True) -> "MipsCode":
        self._op(0x08000000 | ((address // 4) & 0x00FFFFFF))
        return self.nop() if delay_nop else self

    def jal(self, address: int, delay_nop: bool = True) -> "MipsCode":
        self._op(0x0C000000 | ((address // 4) & 0x00FFFFFF))
        return self.nop() if delay_nop else self

    def jr(self, register: int, delay_nop: bool = True) -> "MipsCode":
        self._op(0x08 | (register << 21))
        return self.nop() if delay_nop else self

    def label(self, name: str) -> "MipsCode":
        self.labels[name] = len(self.words)
        self._resolve()
        return self

    def _resolve(self) -> None:
        remaining = []
        for index, name in self.pending:
            if name in self.labels:
                offset = self.labels[name] - (index + 1)
                self.words[index] = (self.words[index] & 0xFFFF0000) | (offset & 0xFFFF)
            else:
                remaining.append((index, name))
        self.pending = remaining

    def set_(self, to: int, value: int) -> "MipsCode":
        """Sets a 32-bit value in a register (lui + addiu, or a single addiu if possible)"""
        upper, lower = (value >> 16) & 0xFFFF, value & 0xFFFF
        if lower & 0x8000:
            upper = (upper + 1) & 0xFFFF
        if upper:
            self.lui(to, upper)
            return self.addiu(to, to, lower)
        return self.addiu(to, ZERO, lower)

    def move_(self, to: int, source: int) -> "MipsCode":
        return self.add(to, source, ZERO)

    def bne_(self, register: int, value: int, label: str) -> "MipsCode":
        return self.set_(T9, value).bne(register, T9, label)

    def beq_(self, register: int, value: int, label: str) -> "MipsCode":
        return self.set_(T9, value).beq(register, T9, label)
