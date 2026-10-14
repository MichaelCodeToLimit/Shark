"""
x86-64 machine code and Windows PE32+ executables, written from scratch.

This is the bottom of Shark's native back end (spec/10-backends-targets-abi.md).
It turns instructions into bytes and bytes into an .exe with no assembler, no
linker, no C compiler and no C runtime. The only thing a finished program
imports is KERNEL32.dll, which is part of Windows itself.

Only the instructions the back end actually uses are here. Every encoding
follows the Intel SDM, volume 2: REX prefix, opcode, ModRM, optional SIB,
displacement, immediate.
"""

from __future__ import annotations

import struct

RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI = range(8)
R8, R9, R10, R11, R12, R13, R14, R15 = range(8, 16)

# Condition codes for Jcc and SETcc.
CC = {"o": 0x0, "no": 0x1, "b": 0x2, "ae": 0x3, "e": 0x4, "ne": 0x5,
      "be": 0x6, "a": 0x7, "s": 0x8, "ns": 0x9,
      "l": 0xC, "ge": 0xD, "le": 0xE, "g": 0xF}


class Asm:
    """Assembles into a byte buffer. Branches and data references are left as
    fixups and resolved once the executable's layout is known."""

    def __init__(self):
        self.buf = bytearray()
        self.labels: dict = {}
        self.fixups: list = []          # (position, section, symbol)
        self._unique = 0

    # -- bookkeeping -----------------------------------------------------

    def new_label(self, hint: str = "L") -> str:
        self._unique += 1
        return ".{}{}".format(hint, self._unique)

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError("label defined twice: " + name)
        self.labels[name] = len(self.buf)

    def _bytes(self, *values: int) -> None:
        self.buf.extend(values)

    def _disp32(self, value: int) -> None:
        self.buf.extend(struct.pack("<i", value))

    def _fixup(self, section: str, symbol: str) -> None:
        self.fixups.append((len(self.buf), section, symbol))
        self._disp32(0)

    @staticmethod
    def _rex(w: int, r: int = 0, x: int = 0, b: int = 0) -> int:
        return 0x40 | (w << 3) | ((r >> 3) << 2) | ((x >> 3) << 1) | (b >> 3)

    # -- operand forms ---------------------------------------------------

    def _reg_reg(self, opcode: tuple, reg: int, rm: int) -> None:
        """REX.W + opcode + ModRM with both operands registers."""
        self._bytes(self._rex(1, reg, 0, rm), *opcode,
                    0xC0 | ((reg & 7) << 3) | (rm & 7))

    def _reg_mem(self, opcode: tuple, reg: int, base: int, disp: int,
                 wide: bool = True) -> None:
        """Opcode with a [base + disp32] memory operand."""
        self._bytes(self._rex(1 if wide else 0, reg, 0, base), *opcode)
        rm = base & 7
        self._bytes(0x80 | ((reg & 7) << 3) | rm)
        if rm == 4:                       # RSP and R12 need a SIB byte
            self._bytes(0x24)
        self._disp32(disp)

    def _reg_rip(self, opcode: tuple, reg: int, section: str, symbol: str) -> None:
        """Opcode with a [rip + disp32] operand pointing at a symbol."""
        self._bytes(self._rex(1, reg), *opcode, ((reg & 7) << 3) | 0x05)
        self._fixup(section, symbol)

    # -- moves -----------------------------------------------------------

    def mov(self, dst: int, src: int) -> None:
        self._reg_reg((0x89,), src, dst)

    def mov_imm(self, dst: int, value: int) -> None:
        self._bytes(self._rex(1, 0, 0, dst), 0xB8 + (dst & 7))
        self.buf.extend(struct.pack("<q", value))

    def load(self, dst: int, section: str, symbol: str) -> None:
        self._reg_rip((0x8B,), dst, section, symbol)

    def store(self, section: str, symbol: str, src: int) -> None:
        self._reg_rip((0x89,), src, section, symbol)

    def lea(self, dst: int, section: str, symbol: str) -> None:
        self._reg_rip((0x8D,), dst, section, symbol)

    def load_mem(self, dst: int, base: int, disp: int) -> None:
        self._reg_mem((0x8B,), dst, base, disp)

    def store_mem(self, base: int, disp: int, src: int) -> None:
        self._reg_mem((0x89,), src, base, disp)

    def lea_mem(self, dst: int, base: int, disp: int) -> None:
        """lea dst, [base + disp]"""
        self._reg_mem((0x8D,), dst, base, disp)

    def load_byte(self, dst: int, base: int, disp: int) -> None:
        """movzx dst, byte [base + disp]"""
        self._reg_mem((0x0F, 0xB6), dst, base, disp)

    def store_byte(self, base: int, disp: int, src: int) -> None:
        """mov byte [base + disp], low byte of src"""
        self._reg_mem((0x88,), src, base, disp, wide=False)

    # -- arithmetic and logic --------------------------------------------

    def add(self, dst: int, src: int) -> None:
        self._reg_reg((0x01,), src, dst)

    def sub(self, dst: int, src: int) -> None:
        self._reg_reg((0x29,), src, dst)

    def cmp(self, a: int, b: int) -> None:
        self._reg_reg((0x39,), b, a)

    def test(self, a: int, b: int) -> None:
        self._reg_reg((0x85,), b, a)

    def and_(self, dst: int, src: int) -> None:
        self._reg_reg((0x21,), src, dst)

    def or_(self, dst: int, src: int) -> None:
        self._reg_reg((0x09,), src, dst)

    def xor(self, dst: int, src: int) -> None:
        self._reg_reg((0x31,), src, dst)

    def imul(self, dst: int, src: int) -> None:
        self._reg_reg((0x0F, 0xAF), dst, src)

    def _group1(self, digit: int, reg: int, imm: int) -> None:
        self._bytes(self._rex(1, 0, 0, reg), 0x81, 0xC0 | (digit << 3) | (reg & 7))
        self._disp32(imm)

    def add_imm(self, reg: int, imm: int) -> None:
        self._group1(0, reg, imm)

    def or_imm(self, reg: int, imm: int) -> None:
        self._group1(1, reg, imm)

    def and_imm(self, reg: int, imm: int) -> None:
        self._group1(4, reg, imm)

    def sub_imm(self, reg: int, imm: int) -> None:
        self._group1(5, reg, imm)

    def xor_imm(self, reg: int, imm: int) -> None:
        self._group1(6, reg, imm)

    def cmp_imm(self, reg: int, imm: int) -> None:
        self._group1(7, reg, imm)

    def shl_imm(self, reg: int, count: int) -> None:
        self._bytes(self._rex(1, 0, 0, reg), 0xC1, 0xE0 | (reg & 7), count)

    def _group3(self, digit: int, reg: int) -> None:
        self._bytes(self._rex(1, 0, 0, reg), 0xF7, 0xC0 | (digit << 3) | (reg & 7))

    def neg(self, reg: int) -> None:
        self._group3(3, reg)

    def div(self, reg: int) -> None:
        """Unsigned rdx:rax / reg."""
        self._group3(6, reg)

    def idiv(self, reg: int) -> None:
        """Signed rdx:rax / reg."""
        self._group3(7, reg)

    def cqo(self) -> None:
        self._bytes(0x48, 0x99)

    def set_rax(self, cc: str) -> None:
        """rax = 1 if the condition holds, else 0."""
        self._bytes(0x0F, 0x90 | CC[cc], 0xC0)        # setcc al
        self._bytes(0x48, 0x0F, 0xB6, 0xC0)           # movzx rax, al

    # -- control flow ----------------------------------------------------

    def jmp(self, label: str) -> None:
        self._bytes(0xE9)
        self._fixup("code", label)

    def jcc(self, cc: str, label: str) -> None:
        self._bytes(0x0F, 0x80 | CC[cc])
        self._fixup("code", label)

    def call(self, label: str) -> None:
        self._bytes(0xE8)
        self._fixup("code", label)

    def call_reg(self, reg: int) -> None:
        if reg >= 8:
            self._bytes(0x41)
        self._bytes(0xFF, 0xD0 | (reg & 7))

    def call_import(self, name: str) -> None:
        self._bytes(0xFF, 0x15)                        # call [rip + IAT slot]
        self._fixup("iat", name)

    def ret(self) -> None:
        self._bytes(0xC3)

    def push(self, reg: int) -> None:
        if reg >= 8:
            self._bytes(0x41)
        self._bytes(0x50 + (reg & 7))

    def pop(self, reg: int) -> None:
        if reg >= 8:
            self._bytes(0x41)
        self._bytes(0x58 + (reg & 7))


# --------------------------------------------------------------------------
# PE32+ executable
# --------------------------------------------------------------------------

FILE_ALIGN = 0x200
SECTION_ALIGN = 0x1000
IMAGE_BASE = 0x140000000
HEADERS_SIZE = 0x400


def _align(value: int, to: int) -> int:
    return (value + to - 1) // to * to


def build_pe(asm: Asm, entry: str, rdata: dict, bss: dict,
             imports: list, dll: str = "KERNEL32.dll") -> bytes:
    """Lay out .text, .rdata (constants and the import table) and .bss,
    resolve every fixup, and return the bytes of a console executable.

    `rdata` maps symbol -> bytes, `bss` maps symbol -> size in bytes (zeroed
    by the loader), `imports` lists functions taken from `dll`.
    The output is deterministic: no timestamps, no paths, no randomness."""
    text_rva = SECTION_ALIGN
    code = asm.buf

    # .rdata: constants, then the import directory.
    rdata_rva = _align(text_rva + len(code), SECTION_ALIGN)
    blob = bytearray()
    rdata_syms = {}
    for symbol, value in rdata.items():
        rdata_syms[symbol] = len(blob)
        blob.extend(value)
    while len(blob) % 8:
        blob.append(0)

    count = len(imports)
    descriptors = len(blob)
    blob.extend(bytes(40))                       # one descriptor + terminator
    lookup = len(blob)
    blob.extend(bytes(8 * (count + 1)))
    address_table = len(blob)
    blob.extend(bytes(8 * (count + 1)))
    hints = []
    for name in imports:
        hints.append(len(blob))
        blob.extend(b"\0\0" + name.encode("ascii") + b"\0")
        if len(blob) % 2:
            blob.append(0)
    dll_name = len(blob)
    blob.extend(dll.encode("ascii") + b"\0")

    struct.pack_into("<IIIII", blob, descriptors,
                     rdata_rva + lookup, 0, 0,
                     rdata_rva + dll_name, rdata_rva + address_table)
    iat_syms = {}
    for index, name in enumerate(imports):
        struct.pack_into("<Q", blob, lookup + 8 * index, rdata_rva + hints[index])
        struct.pack_into("<Q", blob, address_table + 8 * index, rdata_rva + hints[index])
        iat_syms[name] = address_table + 8 * index

    # .bss: zero-initialised globals.
    bss_rva = _align(rdata_rva + len(blob), SECTION_ALIGN)
    bss_syms = {}
    bss_size = 0
    for symbol, size in bss.items():
        bss_size = _align(bss_size, 16)
        bss_syms[symbol] = bss_size
        bss_size += size
    bss_size = max(bss_size, 16)
    image_size = _align(bss_rva + bss_size, SECTION_ALIGN)

    # Resolve fixups: every reference is RIP-relative, so no relocations.
    bases = {"rdata": (rdata_rva, rdata_syms), "bss": (bss_rva, bss_syms),
             "iat": (rdata_rva, iat_syms)}
    for position, section, symbol in asm.fixups:
        if section == "code":
            if symbol not in asm.labels:
                raise ValueError("undefined label: " + symbol)
            target = text_rva + asm.labels[symbol]
        else:
            base, table = bases[section]
            if symbol not in table:
                raise ValueError("undefined {} symbol: {}".format(section, symbol))
            target = base + table[symbol]
        struct.pack_into("<i", code, position, target - (text_rva + position + 4))

    text_raw = _align(len(code), FILE_ALIGN)
    rdata_raw = _align(len(blob), FILE_ALIGN)
    text_ptr = HEADERS_SIZE
    rdata_ptr = text_ptr + text_raw

    header = bytearray(HEADERS_SIZE)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)          # e_lfanew
    offset = 0x40
    header[offset:offset + 4] = b"PE\0\0"
    offset += 4
    struct.pack_into("<HHIIIHH", header, offset,
                     0x8664,          # Machine: x86-64
                     3,               # NumberOfSections
                     0, 0, 0,         # TimeDateStamp, symbol table, symbols
                     0xF0,            # SizeOfOptionalHeader
                     0x0023)          # RELOCS_STRIPPED | EXECUTABLE | LARGE_ADDRESS_AWARE
    offset += 20

    optional = struct.pack(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        0x20B, 14, 0,                         # PE32+, linker version
        text_raw, rdata_raw, _align(bss_size, FILE_ALIGN),
        text_rva + asm.labels[entry], text_rva,
        IMAGE_BASE, SECTION_ALIGN, FILE_ALIGN,
        6, 0, 0, 0, 6, 0,                     # OS, image, subsystem versions
        0,                                    # Win32VersionValue
        image_size, HEADERS_SIZE, 0,          # SizeOfImage, SizeOfHeaders, CheckSum
        3,                                    # Subsystem: console
        0x8100,                               # NX_COMPAT | TERMINAL_SERVER_AWARE
        0x100000, 0x1000, 0x100000, 0x1000,   # stack and heap reserve/commit
        0, 16)                                # LoaderFlags, NumberOfRvaAndSizes
    directories = bytearray(16 * 8)
    struct.pack_into("<II", directories, 1 * 8, rdata_rva + descriptors, 40)
    struct.pack_into("<II", directories, 12 * 8, rdata_rva + address_table,
                     8 * (count + 1))
    header[offset:offset + len(optional)] = optional
    offset += len(optional)
    header[offset:offset + len(directories)] = directories
    offset += len(directories)

    sections = [
        (b".text", len(code), text_rva, text_raw, text_ptr, 0x60000020),
        (b".rdata", len(blob), rdata_rva, rdata_raw, rdata_ptr, 0x40000040),
        (b".bss", bss_size, bss_rva, 0, 0, 0xC0000080),
    ]
    for name, vsize, rva, raw, ptr, flags in sections:
        struct.pack_into("<8sIIIIIIHHI", header, offset,
                         name, vsize, rva, raw, ptr, 0, 0, 0, 0, flags)
        offset += 40

    image = bytearray(header)
    image.extend(code)
    image.extend(bytes(text_raw - len(code)))
    image.extend(blob)
    image.extend(bytes(rdata_raw - len(blob)))
    return bytes(image)


def read_imports(image: bytes) -> list:
    """The DLL names a PE32+ executable imports, for checking what a built
    program depends on."""
    lfanew = struct.unpack_from("<I", image, 0x3C)[0]
    sections = struct.unpack_from("<H", image, lfanew + 6)[0]
    optional_size = struct.unpack_from("<H", image, lfanew + 20)[0]
    optional = lfanew + 24
    import_rva = struct.unpack_from("<I", image, optional + 112 + 8)[0]
    table = optional + optional_size

    def to_offset(rva):
        for index in range(sections):
            base = table + 40 * index
            vsize, vaddr, raw, ptr = struct.unpack_from("<IIII", image, base + 8)
            if vaddr <= rva < vaddr + max(vsize, raw):
                return rva - vaddr + ptr
        raise ValueError("rva outside every section")

    names = []
    cursor = to_offset(import_rva)
    while True:
        name_rva = struct.unpack_from("<I", image, cursor + 12)[0]
        if name_rva == 0:
            return names
        start = to_offset(name_rva)
        names.append(image[start:image.index(b"\0", start)].decode("ascii"))
        cursor += 20
