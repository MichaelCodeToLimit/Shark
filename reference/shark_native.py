"""
Shark native back end: canonical SIR -> x86-64 machine code -> Windows .exe.

The back end reads only the canonical intermediate representation that the
front end produces (spec/08-sir.md). It never sees source words, so a program
written in English, Hebrew or Japanese compiles to the same bytes: identical
SIR in, identical executable out (spec/08 section 8.7, requirement 2).

What a built program is
-----------------------
A standalone Windows x86-64 console executable. It imports only KERNEL32.dll,
which ships with Windows. There is no C runtime, no interpreter, no bytecode,
no garbage collector, and no Python, JavaScript or HTML anywhere in it.

How pages run in a console
--------------------------
Shark programs are made of pages, text and buttons. This first back end draws
them as text: a page prints its heading, its text and a numbered list of its
buttons, then waits for a number. Pressing a button runs its `when ... is
pressed` blocks and follows its `to page`. A page with no buttons ends the
program, as does 0 or the end of input.

Supported now: modules (`use` and `share`), pages, show text with value
splices, show button (with `to page`, `named`, and `when ... is pressed`),
show spacer, show divider, the layout blocks, let / keep / set with whole
numbers and yes-or-no values, open page, messages with run-time language
selection, and instructions declared with `to`. Anything else is a compile
error that names what is missing - never a silent skip.
"""

from __future__ import annotations

import unicodedata
from typing import Optional

from x64 import (Asm, build_pe, RAX, RCX, RDX, RSP, RBP, R8, R9, R10, R11)

IMPORTS = [
    "ExitProcess", "GetStdHandle", "WriteFile", "ReadFile",
    "SetConsoleOutputCP", "SetConsoleCP",
    "GetEnvironmentVariableA", "GetUserDefaultUILanguage",
]

# Languages a message can be written for: tag -> (English name, Windows
# primary language id). The id is how the user's own language is detected.
KNOWN_LOCALES = {
    "en": ("english", 0x09), "he": ("hebrew", 0x0D), "ja": ("japanese", 0x11),
    "ar": ("arabic", 0x01), "es": ("spanish", 0x0A), "fr": ("french", 0x0C),
    "de": ("german", 0x07), "pt": ("portuguese", 0x16), "it": ("italian", 0x10),
    "ru": ("russian", 0x19), "zh": ("chinese", 0x04), "ko": ("korean", 0x12),
    "hi": ("hindi", 0x39),
}
LOCALE_WORDS = {name: tag for tag, (name, _id) in KNOWN_LOCALES.items()}
LOCALE_WORDS.update({tag: tag for tag in KNOWN_LOCALES})
LOCALE_WORDS.update({"עברית": "he", "日本語": "ja", "العربية": "ar",
                     "español": "es", "français": "fr", "deutsch": "de"})

NUMBER, TRUTH, TEXT = "a whole number", "yes or no", "text"
SLOT_TYPES = {"core.type.whole_number": NUMBER, "core.type.boolean": TRUTH}
ARITHMETIC = {"plus", "minus", "times", "over", "remainder of"}
ORDERING = {"is more than": "g", "is less than": "l",
            "is at least": "ge", "is at most": "le"}
EQUALITY = {"is": "e", "is not": "ne"}

LAYOUTS = {"core.layout.row", "core.layout.column", "core.layout.grid",
           "core.layout.centred", "core.layout.scrolling"}
BINDINGS = {"core.decl.let", "core.decl.keep", "core.stmt.set"}
DECLARATIONS = {"core.decl.page", "core.decl.message", "core.decl.handler",
                "core.decl.action", "core.decl.use", "core.decl.share"}
FRIENDLY = {
    "core.stmt.show_picture": "`show picture`",
    "core.stmt.show_field": "`show field`",
    "core.stmt.create": "`create`",
    "core.stmt.add_to": "`add ... to`",
    "core.stmt.remove_from": "`remove ... from`",
    "core.stmt.during": "`during` blocks",
    "core.stmt.run_on": "`run on the ...` blocks",
    "core.stmt.fetch_together": "`fetch these at the same time`",
    "core.stmt.wait_for": "`wait for`",
    "core.stmt.wait_at_most": "`wait at most`",
    "core.stmt.go_back": "`go back`",
    "core.stmt.replace_page": "`replace page with`",
}

# Trap reason codes, spec/10 section 10.8. The process exits with the code.
OVERFLOW, DIVIDE_BY_ZERO, ALLOCATION = 1, 2, 9

MAX_BUTTONS = 64
BUTTON_SIZE = 24                     # label pointer, label length, action
TEXT_SPACE = 65536                   # where text with values is assembled


class CompileError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__("{}  {}".format(code, message))
        self.code = code
        self.message = message


def fold(text: str) -> str:
    """Names compare case-insensitively (spec/01 section 1.4.3)."""
    return unicodedata.normalize("NFKC", text).casefold()


def walk(nodes: list):
    for node in nodes:
        yield node
        yield from walk(node.get("body", []))


class Place:
    """Where a value lives. `global` is a symbol in .bss, `frame` is at
    rbp + offset, `indirect` is a pointer at rbp + offset (a name passed to
    a declared instruction). Text is a (pointer, length) pair."""

    def __init__(self, how: str, where, type_: str):
        self.how = how
        self.where = where
        self.type = type_


class Variable:
    def __init__(self, module: "Module", node: dict, symbol: str):
        self.module = module
        self.node = node
        self.place = Place("global", symbol, None)


class Action:
    """An instruction declared with `to`."""

    def __init__(self, module: "Module", node: dict):
        self.module = module
        self.node = node
        shape = node["args"]["shape"]
        self.cid = shape["action"]
        self.head = " ".join(shape["parts"][0]["head"])
        self.params = [part for part in shape["parts"] if "slot" in part]


class Module:
    def __init__(self, name: str, sir: dict, index: int):
        self.name = name
        self.sir = sir
        self.pool = sir["data"]
        self.prefix = "m{}_".format(index)
        self.uses: list = []
        self.shares: set = set()
        self.pages: dict = {}          # folded -> (name, label, node)
        self.page_order: list = []
        self.messages: dict = {}       # folded -> (name, wordings)
        self.variables: dict = {}      # folded -> Variable
        self.actions: dict = {}        # cid -> Action
        self.handlers: list = []
        self.init: list = []
        self.opening: list = []

    # -- reading this module's SIR ---------------------------------------

    def pieces(self, arg: dict) -> list:
        for key in ("const text", "name"):
            if key in arg:
                return self.pool[arg[key]]
        raise CompileError("E-1001", "expected words here")

    def words(self, arg: dict) -> str:
        return " ".join(p for p in self.pieces(arg) if isinstance(p, str))

    def ref(self, arg: dict) -> str:
        if "ref" not in arg:
            raise CompileError("E-1001", "expected a name here")
        return self.pool[arg["ref"]]


class Frame:
    def __init__(self):
        self.params: dict = {}         # folded -> Place
        self.locals: dict = {}         # folded -> Place


class Env:
    """What the code being emitted can see and do."""

    def __init__(self, module: Module, kind: str, where: str,
                 frame: Optional[Frame] = None):
        self.module = module
        self.kind = kind               # render, action, init or body
        self.where = where
        self.frame = frame
        self.buttons: list = []


class NativeCompiler:
    def __init__(self, sir: dict, modules: Optional[dict] = None):
        self.asm = Asm()
        self.rdata: dict = {}
        self._strings: dict = {}
        self.bss = {
            "g_stdout": 8, "g_stdin": 8, "g_stderr": 8, "g_written": 8,
            "g_inlen": 8, "g_inpos": 8, "g_inbuf": 4096,
            "g_numbuf": 32, "g_envbuf": 64, "g_envlen": 8, "g_locale": 8,
            "g_page": 8, "g_next": 8, "g_nbuttons": 8,
            "g_buttons": BUTTON_SIZE * MAX_BUTTONS,
            "g_acc": 8, "g_ndig": 8, "g_tptr": 8, "g_tlen": 8, "g_tact": 8,
            "g_reason": 8, "g_sink": 8, "g_textstart": 8, "g_texttop": 8,
            "g_text": TEXT_SPACE,
        }
        self.sources = dict(modules or {})
        self.modules: dict = {}
        self.order: list = []          # modules, each after those it uses
        self.main = self.load("app main", sir)
        self.locales: list = []
        self.traps: dict = {}
        self.specs: dict = {}
        self.queue: list = []
        self._types: dict = {}
        self._typing: set = set()
        self._labels = 0

    # -- modules ---------------------------------------------------------

    def load(self, name: str, sir: dict) -> Module:
        if name in self.modules:
            return self.modules[name]
        module = Module(name, sir, len(self.modules))
        self.modules[name] = module
        for node in sir["items"]:
            if node["op"] == "core.decl.use":
                used = module.words(node["args"]["module"])
                if used not in self.sources:
                    raise CompileError("E-1201", "module `{}` was not provided".format(used))
                module.uses.append(self.load(used, self.sources[used]))
            elif node["op"] == "core.decl.share":
                module.shares.add(fold(module.words(node["args"]["name"])))
        self.order.append(module)
        return module

    def visible(self, module: Module, table: str, key: str):
        """Own declarations first, then what used modules share."""
        own = getattr(module, table).get(key)
        if own is not None:
            return own
        for used in module.uses:
            entry = getattr(used, table).get(key)
            if entry is None:
                continue
            name = entry.head if isinstance(entry, Action) else key
            if fold(name) in used.shares:
                return entry
        return None

    def label(self, hint: str) -> str:
        self._labels += 1
        return "{}{}".format(hint, self._labels)

    def string(self, text: str) -> tuple:
        data = text.encode("utf-8")
        symbol = self._strings.get(data)
        if symbol is None:
            symbol = "s{}".format(len(self._strings))
            self._strings[data] = symbol
            self.rdata[symbol] = data
        return symbol, len(data)

    # -- collecting declarations -----------------------------------------

    def collect(self, module: Module) -> None:
        for node in module.sir["items"]:
            op = node["op"]
            if op == "core.decl.page":
                name = module.words(node["args"]["name"])
                if fold(name) in module.pages:
                    raise CompileError("E-0502", "page `{}` is declared twice".format(name))
                module.pages[fold(name)] = (name, module.prefix + "page{}".format(
                    len(module.pages)), node)
                module.page_order.append(fold(name))
            elif op == "core.decl.message":
                self.collect_message(module, node)
            elif op == "core.decl.handler":
                module.handlers.append(node)
            elif op == "core.decl.action":
                action = Action(module, node)
                module.actions[action.cid] = action
            elif op in BINDINGS:
                module.init.append(node)
            elif op not in DECLARATIONS:
                module.opening.append(node)

        for node in module.sir["items"]:
            if node["op"] == "core.decl.action":
                continue                   # its bindings live in its frame
            for inner in walk([node]):
                op = inner["op"]
                if op in ("core.decl.use", "core.decl.share") and inner is not node:
                    raise CompileError("E-0571", "`use` and `share` go at the top level")
                if op in ("core.decl.let", "core.decl.keep"):
                    folded = fold(module.words(inner["args"]["name"]))
                    if folded in module.messages:
                        raise CompileError(
                            "E-0502", "`{}` is already the name of a message".format(
                                module.words(inner["args"]["name"])))
                    if folded not in module.variables:
                        symbol = module.prefix + "v{}".format(len(module.variables))
                        module.variables[folded] = Variable(module, inner, symbol)
                        self.bss[symbol] = 8
                if op == "core.decl.handler":
                    event = inner["args"]["event"]
                    if event.get("event") != "core.event.is_pressed":
                        raise CompileError(
                            "E-1001", "only `when ... is pressed` events run natively so far")
        for name in module.shares:
            if not (name in module.pages or name in module.messages
                    or name in module.variables
                    or any(fold(a.head) == name for a in module.actions.values())):
                raise CompileError("E-0503", "`{}` is shared but never declared".format(name))

    def collect_message(self, module: Module, node: dict) -> None:
        name = module.words(node["args"]["name"])
        if fold(name) in module.messages:
            raise CompileError("E-0502", "message `{}` is declared twice".format(name))
        wordings: dict = {}
        for child in node.get("body", []):
            if child["op"] != "core.stmt.in_locale":
                raise CompileError("E-0571", "a message holds only `in <language>` lines")
            word = module.ref(child["args"]["locale"])
            tag = LOCALE_WORDS.get(fold(word))
            if tag is None:
                raise CompileError("E-0416", "`{}` is not a language Shark knows".format(word))
            if tag in wordings:
                raise CompileError("E-0417", "`{}` has two {} wordings".format(name, word))
            wordings[tag] = module.pieces(child["args"]["content"])
            if tag not in self.locales:
                self.locales.append(tag)
        if not wordings:
            raise CompileError("E-0411", "`{}` has no wordings".format(name))
        spliced = {tag: tuple(sorted({fold(p["splice"]) for p in pieces
                                      if isinstance(p, dict)}))
                   for tag, pieces in wordings.items()}
        if len(set(spliced.values())) > 1:
            raise CompileError(
                "E-0413", "the wordings of `{}` do not use the same values".format(name))
        module.messages[fold(name)] = (name, wordings)

    def check_message_cycles(self, module: Module) -> None:
        """A message may include another message, but never itself, directly
        or through others; that would never finish."""
        state: dict = {}

        def visit(folded: str) -> None:
            state[folded] = "visiting"
            for pieces in module.messages[folded][1].values():
                for piece in pieces:
                    inner = fold(piece["splice"]) if isinstance(piece, dict) else None
                    if inner not in module.messages:
                        continue
                    if state.get(inner) == "visiting":
                        raise CompileError(
                            "E-0415", "`{}` includes itself".format(module.messages[inner][0]))
                    if inner not in state:
                        visit(inner)
            state[folded] = "done"

        for folded in module.messages:
            if folded not in state:
                visit(folded)

    # -- names and types -------------------------------------------------

    def lookup(self, env: Env, name: str) -> Optional[Place]:
        """A value's place: this instruction's own bindings and slots first,
        then the module's variables, then what used modules share."""
        folded = fold(name)
        if env.frame is not None:
            if folded in env.frame.locals:
                return env.frame.locals[folded]
            if folded in env.frame.params:
                return env.frame.params[folded]
        variable = self.visible(env.module, "variables", folded)
        if variable is None:
            return None
        variable.place.type = self.variable_type(variable)
        return variable.place

    def place_of(self, env: Env, name: str) -> Place:
        place = self.lookup(env, name)
        if place is None:
            raise CompileError("E-0501", "`{}` isn't defined anywhere".format(name))
        return place

    def variable_type(self, variable: Variable) -> str:
        key = variable.place.where
        if key in self._types:
            return self._types[key]
        if key in self._typing:
            raise CompileError(
                "E-0524", "`{}` is defined in terms of itself".format(
                    variable.module.words(variable.node["args"]["name"])))
        self._typing.add(key)
        env = Env(variable.module, "init", "while starting")
        self._types[key] = self.type_of(variable.node["args"]["value"], env)
        self._typing.discard(key)
        return self._types[key]

    def type_of(self, expr: dict, env: Env) -> str:
        pool = env.module.pool
        if "const number" in expr:
            self.number(pool[expr["const number"]])
            return NUMBER
        if "truth" in expr:
            return TRUTH
        if "ref" in expr:
            return self.place_of(env, pool[expr["ref"]]).type
        if "member" in expr:
            raise CompileError("E-1001", "fields like `x of y` don't run natively yet")
        op = expr.get("apply")
        if "operand" in expr:
            want = NUMBER if op == "minus" else TRUTH
            self.expect(self.type_of(expr["operand"], env), want, op)
            return want
        if op is None:
            raise CompileError("E-1001", "this value can't run natively yet")
        left, right = self.type_of(expr["left"], env), self.type_of(expr["right"], env)
        if op in ARITHMETIC or op in ORDERING:
            self.expect(left, NUMBER, op)
            self.expect(right, NUMBER, op)
            return NUMBER if op in ARITHMETIC else TRUTH
        if op in EQUALITY:
            if TEXT in (left, right):
                raise CompileError("E-1001", "comparing text doesn't run natively yet")
            if left != right:
                raise CompileError(
                    "E-0522", "`{}` compares {} with {}".format(op, left, right))
            return TRUTH
        if op in ("and", "or"):
            self.expect(left, TRUTH, op)
            self.expect(right, TRUTH, op)
            return TRUTH
        raise CompileError("E-1001", "`{}` doesn't run natively yet".format(op))

    @staticmethod
    def expect(found: str, wanted: str, op: str) -> None:
        if found != wanted:
            raise CompileError(
                "E-0522", "`{}` needs {}, not {}".format(op, wanted, found))

    def number(self, text: str) -> int:
        cleaned = text
        for mark in (",", "_", " ", " "):
            cleaned = cleaned.replace(mark, "")
        if "." in cleaned:
            raise CompileError(
                "E-1002", "decimal numbers like {} don't run natively yet".format(text))
        value = int(cleaned)
        if not -2 ** 63 <= value < 2 ** 63:
            raise CompileError("E-0523", "{} is too large for a whole number".format(text))
        return value

    def page_label(self, env: Env, name: str) -> str:
        entry = self.visible(env.module, "pages", fold(name))
        if entry is None:
            raise CompileError(
                "E-0501", "there is no page called `{}` here".format(name))
        return entry[1]

    # -- places ----------------------------------------------------------

    def load_place(self, place: Place, reg: int = RAX) -> None:
        a = self.asm
        if place.how == "global":
            a.load(reg, "bss", place.where)
        elif place.how == "frame":
            a.load_mem(reg, RBP, place.where)
        else:
            a.load_mem(R11, RBP, place.where)
            a.load_mem(reg, R11, 0)

    def store_place(self, place: Place) -> None:
        a = self.asm
        if place.how == "global":
            a.store("bss", place.where, RAX)
        elif place.how == "frame":
            a.store_mem(RBP, place.where, RAX)
        else:
            a.load_mem(R11, RBP, place.where)
            a.store_mem(R11, 0, RAX)

    def address_of(self, place: Place) -> None:
        a = self.asm
        if place.how == "global":
            a.lea(RAX, "bss", place.where)
        elif place.how == "frame":
            a.lea_mem(RAX, RBP, place.where)
        else:
            a.load_mem(RAX, RBP, place.where)

    def text_of(self, place: Place) -> None:
        """rcx = pointer, rdx = length of a text place."""
        a = self.asm
        if place.how == "frame":
            a.load_mem(RCX, RBP, place.where)
            a.load_mem(RDX, RBP, place.where + 8)
        else:
            a.load_mem(RAX, RBP, place.where)
            a.load_mem(RCX, RAX, 0)
            a.load_mem(RDX, RAX, 8)

    # -- emitting small things -------------------------------------------

    def write(self, text: str) -> None:
        if not text:
            return
        symbol, length = self.string(text)
        self.asm.lea(RCX, "rdata", symbol)
        self.asm.mov_imm(RDX, length)
        self.asm.call("rt_write")

    def emit_pieces(self, pieces: list, env: Env, end: str = "") -> None:
        pending = ""
        for index, piece in enumerate(pieces):
            pending += " " if index else ""
            if isinstance(piece, str):
                pending += piece
                continue
            self.write(pending)
            pending = ""
            self.emit_splice(piece["splice"], env)
        self.write(pending + end)

    def emit_splice(self, name: str, env: Env) -> None:
        place = self.lookup(env, name)
        if place is not None:
            if place.type == TEXT:
                self.text_of(place)
                self.asm.call("rt_write")
            else:
                self.load_place(place, RCX)
                self.asm.call("rt_print_truth" if place.type == TRUTH else "rt_print_int")
            return
        message = self.visible(env.module, "messages", fold(name))
        if message is None:
            raise CompileError("E-0501", "`{}` isn't defined, so it can't be shown".format(name))
        self.emit_message(message[1], env)

    def emit_message(self, wordings: dict, env: Env) -> None:
        """A message's wording is chosen when it is shown, from the display
        language found at start-up. Its values are the ones in scope where it
        is used (spec/04 section 4.12.3)."""
        a = self.asm
        default = "en" if "en" in wordings else next(t for t in self.locales if t in wordings)
        done = self.label(".msg")
        a.load(RAX, "bss", "g_locale")
        for index, tag in enumerate(self.locales):
            if tag not in wordings or tag == default:
                continue
            skip = self.label(".msg")
            a.cmp_imm(RAX, index)
            a.jcc("ne", skip)
            self.emit_pieces(wordings[tag], env)
            a.jmp(done)
            a.label(skip)
        self.emit_pieces(wordings[default], env)
        a.label(done)

    def emit_text_value(self, pieces: list, env: Env) -> None:
        """rcx = pointer, rdx = length of some text. Plain words are constant;
        text with values in it is assembled in the text space, which a call
        gives back when it returns."""
        a = self.asm
        if all(isinstance(p, str) for p in pieces):
            symbol, length = self.string(" ".join(pieces))
            a.lea(RCX, "rdata", symbol)
            a.mov_imm(RDX, length)
            return
        a.load(RAX, "bss", "g_texttop")
        a.store("bss", "g_textstart", RAX)
        a.mov_imm(RAX, 1)
        a.store("bss", "g_sink", RAX)
        self.emit_pieces(pieces, env)
        a.xor(RAX, RAX)
        a.store("bss", "g_sink", RAX)
        a.load(RCX, "bss", "g_textstart")
        a.load(RDX, "bss", "g_texttop")
        a.sub(RDX, RCX)

    def trap(self, reason: int, op: str, where: str) -> str:
        """A label that stops the program, naming the operation and where it
        happened (spec/05 section 5.6)."""
        key = (reason, op, where)
        if key not in self.traps:
            what = {OVERFLOW: "made a whole number too large",
                    DIVIDE_BY_ZERO: "divided by zero",
                    ALLOCATION: "made more text than there is room for"}[reason]
            text = "Shark stopped {}: `{}` {}.\n".format(where, op, what)
            self.traps[key] = (self.label(".trap"), text)
        return self.traps[key][0]

    # -- expressions: result in RAX --------------------------------------

    def emit_expr(self, expr: dict, env: Env) -> None:
        a = self.asm
        pool = env.module.pool
        if "const number" in expr:
            a.mov_imm(RAX, self.number(pool[expr["const number"]]))
        elif "truth" in expr:
            a.mov_imm(RAX, 1 if expr["truth"] == "yes" else 0)
        elif "ref" in expr:
            self.load_place(self.place_of(env, pool[expr["ref"]]))
        elif "operand" in expr:
            self.emit_expr(expr["operand"], env)
            if expr["apply"] == "minus":
                a.neg(RAX)
                a.jcc("o", self.trap(OVERFLOW, "minus", env.where))
            else:                                  # not, on a 0 / 1 value
                a.xor_imm(RAX, 1)
        else:
            self.emit_binary(expr["apply"], expr["left"], expr["right"], env)

    def emit_binary(self, op: str, left: dict, right: dict, env: Env) -> None:
        a = self.asm
        if op in ("and", "or"):
            # Short-circuit (spec/02 section 2.7.1): the right side is not
            # evaluated when the left side already decides, so
            # `x is not 0 and 10 over x is more than 1` cannot trap.
            done = self.label(".logic")
            self.emit_expr(left, env)
            a.test(RAX, RAX)
            a.jcc("e" if op == "and" else "ne", done)
            self.emit_expr(right, env)
            a.label(done)
            return
        self.emit_expr(left, env)
        a.push(RAX)
        self.emit_expr(right, env)
        a.mov(RCX, RAX)
        a.pop(RAX)
        if op == "plus":
            a.add(RAX, RCX)
            a.jcc("o", self.trap(OVERFLOW, op, env.where))
        elif op == "minus":
            a.sub(RAX, RCX)
            a.jcc("o", self.trap(OVERFLOW, op, env.where))
        elif op == "times":
            a.imul(RAX, RCX)
            a.jcc("o", self.trap(OVERFLOW, op, env.where))
        elif op in ("over", "remainder of"):
            # Whole-number division rounds toward zero, and the remainder
            # takes the sign of the number being divided (spec/05 5.6).
            a.test(RCX, RCX)
            a.jcc("e", self.trap(DIVIDE_BY_ZERO, op, env.where))
            fine = self.label(".div")
            a.cmp_imm(RCX, -1)
            a.jcc("ne", fine)
            a.mov_imm(RDX, -2 ** 63)
            a.cmp(RAX, RDX)
            a.jcc("e", self.trap(OVERFLOW, op, env.where))
            a.label(fine)
            a.cqo()
            a.idiv(RCX)
            if op == "remainder of":
                a.mov(RAX, RDX)
        else:
            a.cmp(RAX, RCX)
            a.set_rax(ORDERING.get(op) or EQUALITY[op])

    # -- statements ------------------------------------------------------

    def emit_block(self, nodes: list, env: Env) -> None:
        for node in nodes:
            self.emit_statement(node, env)

    def emit_statement(self, node: dict, env: Env) -> None:
        a = self.asm
        module = env.module
        op = node["op"]
        args = node.get("args", {})
        if op == "core.stmt.show_text":
            self.emit_pieces(module.pieces(args["content"]), env, "\n")
        elif op == "core.stmt.show_spacer":
            self.write("\n")
        elif op == "core.stmt.show_divider":
            self.write("-" * 24 + "\n")
        elif op == "core.stmt.show_button":
            if env.kind == "body":
                raise CompileError(
                    "E-1001", "buttons inside `to` instructions don't run natively yet")
            if env.kind != "render":
                raise CompileError("E-0571", "buttons belong on a page, not inside `when`")
            self.emit_button(args, env)
        elif op in LAYOUTS:
            self.emit_block(node.get("body", []), env)
        elif op in BINDINGS:
            if op == "core.stmt.set":
                name = module.ref(args["place"])
            else:
                name = module.words(args["name"])
            place = self.place_of(env, name)
            found = self.type_of(args["value"], env)
            if found != place.type:
                raise CompileError(
                    "E-0522", "`{}` holds {}, so it can't be given {}".format(
                        name, place.type, found))
            self.emit_expr(args["value"], env)
            self.store_place(place)
        elif op == "core.stmt.open_page":
            if env.kind not in ("action", "body"):
                raise CompileError("E-0571", "`open page` goes inside a `when` block")
            a.lea(RAX, "code", self.page_label(env, module.ref(args["target"])))
            a.store("bss", "g_next", RAX)
        elif op == "core.decl.handler":
            if env.kind != "render":
                raise CompileError("E-0571", "a `when` block belongs to a page or the top level")
        elif op in DECLARATIONS:
            raise CompileError("E-0571", "declarations go at the top level")
        elif op.startswith("user."):
            self.emit_call(node, env)
        else:
            what = FRIENDLY.get(op, "`{}`".format(op))
            raise CompileError("E-1001", "{} doesn't run natively yet".format(what))

    def emit_button(self, args: dict, env: Env) -> None:
        module = env.module
        pieces = module.pieces(args["label"])
        if not all(isinstance(p, str) for p in pieces):
            raise CompileError("E-1001", "button labels can't show values yet")
        if len(env.buttons) == MAX_BUTTONS:
            raise CompileError(
                "E-1003", "a page can have at most {} buttons".format(MAX_BUTTONS))
        text = " ".join(pieces)
        key = module.words(args["key"]) if "key" in args else None
        target = self.page_label(env, module.ref(args["target"])) if "target" in args else None
        action = module.prefix + "act{}".format(len(env.buttons)) + self.label("_")
        env.buttons.append((text, key, target, action))
        symbol, length = self.string(text)
        self.asm.lea(RCX, "rdata", symbol)
        self.asm.mov_imm(RDX, length)
        self.asm.lea(R8, "code", action)
        self.asm.call("rt_show_button")

    # -- declared instructions -------------------------------------------

    def resolve_action(self, module: Module, cid: str) -> Action:
        action = self.visible(module, "actions", cid)
        if action is None:
            raise CompileError("E-0501", "`{}` isn't an instruction here".format(
                cid[len("user."):].replace("_", " ")))
        return action

    def emit_call(self, node: dict, env: Env) -> None:
        """Arguments go in a block reserved on the stack, left to right
        (spec/05 section 5.9): text as (pointer, length), values as
        themselves, names as the address of what they name."""
        a = self.asm
        module = env.module
        action = self.resolve_action(module, node["op"])
        args = node.get("args", {})
        slots = sum(2 if p["kind"] == "text" else 1 for p in action.params)
        refs = sum(1 for p in action.params if p["kind"] == "ref")
        pairs = slots
        saved = slots + 2 * refs
        total = saved + 1 + (saved + 1) % 2
        a.sub_imm(RSP, 8 * total)
        a.load(RAX, "bss", "g_texttop")
        a.store_mem(RSP, 8 * saved, RAX)
        key = []
        slot = 0
        for param in action.params:
            arg = args.get(param["slot"])
            if arg is None:
                raise CompileError("E-1001", "`{}` needs its `{}`".format(
                    action.head, param["slot"]))
            if param["kind"] == "text":
                self.emit_text_value(module.pieces(arg), env)
                a.store_mem(RSP, 8 * slot, RCX)
                a.store_mem(RSP, 8 * slot + 8, RDX)
                slot += 2
                continue
            if param["kind"] == "value":
                wanted = SLOT_TYPES.get(param.get("type"))
                if wanted is None:
                    raise CompileError(
                        "E-1002", "decimal numbers don't run natively yet")
                self.expect(self.type_of(arg, env), wanted, action.head)
                self.emit_expr(arg, env)
            else:
                name = module.ref(arg)
                place = self.lookup(env, name)
                if place is not None:
                    self.address_of(place)
                    key.append(place.type)
                else:
                    message = self.visible(module, "messages", fold(name))
                    if message is not None:
                        a.load(RAX, "bss", "g_texttop")
                        a.store("bss", "g_textstart", RAX)
                        a.mov_imm(RAX, 1)
                        a.store("bss", "g_sink", RAX)
                        self.emit_message(message[1], env)
                        a.xor(RAX, RAX)
                        a.store("bss", "g_sink", RAX)
                        a.load(RCX, "bss", "g_textstart")
                        a.load(RDX, "bss", "g_texttop")
                        a.sub(RDX, RCX)
                    else:
                        # A name that is not a value stands for itself, as
                        # `bar` does in `show chart bar with sales`.
                        symbol, length = self.string(name)
                        a.lea(RCX, "rdata", symbol)
                        a.mov_imm(RDX, length)
                    a.store_mem(RSP, 8 * pairs, RCX)
                    a.store_mem(RSP, 8 * pairs + 8, RDX)
                    a.lea_mem(RAX, RSP, 8 * pairs)
                    pairs += 2
                    key.append(TEXT)
            a.store_mem(RSP, 8 * slot, RAX)
            slot += 1
        a.call(self.specialize(action, tuple(key)))
        a.load_mem(RAX, RSP, 8 * saved)
        a.store("bss", "g_texttop", RAX)
        a.add_imm(RSP, 8 * total)

    def specialize(self, action: Action, key: tuple) -> str:
        """One routine per combination of what the named slots refer to."""
        spec = (action.module.name, action.cid, key)
        if spec not in self.specs:
            self.specs[spec] = action.module.prefix + self.label("do")
            self.queue.append((action, key, self.specs[spec]))
        return self.specs[spec]

    def emit_action(self, action: Action, key: tuple, label: str) -> None:
        a = self.asm
        frame = Frame()
        env = Env(action.module, "body", "in `{}`".format(action.head), frame)
        offset = 16
        refs = iter(key)
        for param in action.params:
            folded = fold(param["slot"])
            if param["kind"] == "text":
                frame.params[folded] = Place("frame", offset, TEXT)
                offset += 16
            elif param["kind"] == "value":
                frame.params[folded] = Place("frame", offset,
                                             SLOT_TYPES.get(param.get("type")))
                offset += 8
            else:
                frame.params[folded] = Place("indirect", offset, next(refs))
                offset += 8
        body = action.node.get("body", [])
        for node in walk(body):
            if node["op"] in ("core.decl.let", "core.decl.keep"):
                folded = fold(action.module.words(node["args"]["name"]))
                if folded not in frame.locals:
                    found = self.type_of(node["args"]["value"], env)
                    frame.locals[folded] = Place(
                        "frame", -8 * (len(frame.locals) + 1), found)
        size = 32 + (8 * len(frame.locals) + 15) // 16 * 16
        a.label(label)
        a.push(RBP)
        a.mov(RBP, RSP)
        a.sub_imm(RSP, size)
        self.emit_block(body, env)
        a.mov(RSP, RBP)
        a.pop(RBP)
        a.ret()

    # -- pages -----------------------------------------------------------

    def emit_page(self, module: Module, label: str, title: Optional[str],
                  body: list) -> None:
        a = self.asm
        env = Env(module, "render",
                  "on page {}".format(title) if title else "on the opening screen")
        handlers = [n for n in walk(body) if n["op"] == "core.decl.handler"]
        a.label(label)
        a.sub_imm(RSP, 40)
        a.xor(RAX, RAX)
        a.store("bss", "g_nbuttons", RAX)
        if title:
            self.write("== {} ==\n".format(title))
        self.emit_block(body, env)
        a.add_imm(RSP, 40)
        a.ret()
        for button in env.buttons:
            self.emit_press(module, button, handlers + module.handlers)

    def emit_press(self, module: Module, button: tuple, handlers: list) -> None:
        a = self.asm
        text, key, target, label = button
        names = {fold(text)} | ({fold(key)} if key else set())
        env = Env(module, "action", "after pressing {}".format(text))
        a.label(label)
        a.sub_imm(RSP, 40)
        # The button's own `to page` first, so an explicit `open page` in a
        # `when` block can override it.
        if target:
            a.lea(RAX, "code", target)
            a.store("bss", "g_next", RAX)
        for handler in handlers:
            subject = module.ref(handler["args"]["event"]["args"]["subject"])
            if fold(subject) in names:
                self.emit_block(handler.get("body", []), env)
        a.add_imm(RSP, 40)
        a.ret()

    # -- run-time support ------------------------------------------------

    def emit_locale_detection(self) -> None:
        """SHARK_LOCALE if set, otherwise the user's Windows display language
        (spec/04 section 4.12.4)."""
        a = self.asm
        chosen = {i: self.label(".loc") for i in range(len(self.locales))}
        store, system = self.label(".loc"), self.label(".loc")
        a.label("rt_locale")
        a.sub_imm(RSP, 40)
        name, _ = self.string("SHARK_LOCALE\0")
        a.lea(RCX, "rdata", name)
        a.lea(RDX, "bss", "g_envbuf")
        a.mov_imm(R8, 64)
        a.call_import("GetEnvironmentVariableA")
        a.store("bss", "g_envlen", RAX)
        a.test(RAX, RAX)
        a.jcc("e", system)
        a.cmp_imm(RAX, 64)
        a.jcc("ae", system)
        for index, tag in enumerate(self.locales):
            for spelling in (tag, KNOWN_LOCALES[tag][0]):
                symbol, length = self.string(spelling)
                a.lea(RCX, "rdata", symbol)
                a.mov_imm(RDX, length)
                a.call("rt_env_equals")
                a.test(RAX, RAX)
                a.jcc("ne", chosen[index])
        a.label(system)
        a.call_import("GetUserDefaultUILanguage")
        a.and_imm(RAX, 0x3FF)
        for index, tag in enumerate(self.locales):
            a.cmp_imm(RAX, KNOWN_LOCALES[tag][1])
            a.jcc("e", chosen[index])
        default = self.locales.index("en") if "en" in self.locales else 0
        a.mov_imm(RAX, default)
        a.jmp(store)
        for index in range(len(self.locales)):
            a.label(chosen[index])
            a.mov_imm(RAX, index)
            a.jmp(store)
        a.label(store)
        a.store("bss", "g_locale", RAX)
        a.add_imm(RSP, 40)
        a.ret()

    def emit_runtime(self) -> None:
        a = self.asm

        # rt_write(rcx = text, rdx = byte length): to the console, or onto
        # the end of the text being assembled when g_sink is set.
        assemble, copy, copied = self.label(".w"), self.label(".w"), self.label(".w")
        a.label("rt_write")
        a.sub_imm(RSP, 40)
        a.load(RAX, "bss", "g_sink")
        a.test(RAX, RAX)
        a.jcc("ne", assemble)
        a.mov(R8, RDX)
        a.mov(RDX, RCX)
        a.load(RCX, "bss", "g_stdout")
        a.lea(R9, "bss", "g_written")
        a.xor(RAX, RAX)
        a.store_mem(RSP, 32, RAX)
        a.call_import("WriteFile")
        a.add_imm(RSP, 40)
        a.ret()
        a.label(assemble)
        a.load(R8, "bss", "g_texttop")
        a.mov(R9, R8)
        a.add(R9, RDX)
        a.lea(R10, "bss", "g_text")
        a.add_imm(R10, TEXT_SPACE)
        a.cmp(R9, R10)
        a.jcc("a", self.trap(ALLOCATION, "text", "while joining text"))
        a.label(copy)
        a.test(RDX, RDX)
        a.jcc("e", copied)
        a.load_byte(RAX, RCX, 0)
        a.store_byte(R8, 0, RAX)
        a.add_imm(RCX, 1)
        a.add_imm(R8, 1)
        a.sub_imm(RDX, 1)
        a.jmp(copy)
        a.label(copied)
        a.store("bss", "g_texttop", R8)
        a.add_imm(RSP, 40)
        a.ret()

        a.label("rt_newline")
        a.sub_imm(RSP, 40)
        self.write("\n")
        a.add_imm(RSP, 40)
        a.ret()

        # rt_print_truth(rcx = 0 or 1)
        no, done = self.label(".yn"), self.label(".yn")
        a.label("rt_print_truth")
        a.sub_imm(RSP, 40)
        a.test(RCX, RCX)
        a.jcc("e", no)
        self.write("yes")
        a.jmp(done)
        a.label(no)
        self.write("no")
        a.label(done)
        a.add_imm(RSP, 40)
        a.ret()

        # rt_print_int(rcx = signed whole number)
        loop, positive, write = self.label(".int"), self.label(".int"), self.label(".int")
        a.label("rt_print_int")
        a.sub_imm(RSP, 40)
        a.mov(RAX, RCX)
        a.lea(R8, "bss", "g_numbuf")
        a.add_imm(R8, 32)
        a.xor(R9, R9)
        a.cmp_imm(RAX, 0)
        a.jcc("ge", positive)
        a.neg(RAX)                     # the smallest number stays itself,
        a.mov_imm(R9, 1)               # which unsigned division reads right
        a.label(positive)
        a.mov_imm(RCX, 10)
        a.label(loop)
        a.xor(RDX, RDX)
        a.div(RCX)
        a.add_imm(RDX, 48)
        a.sub_imm(R8, 1)
        a.store_byte(R8, 0, RDX)
        a.test(RAX, RAX)
        a.jcc("ne", loop)
        a.test(R9, R9)
        a.jcc("e", write)
        a.sub_imm(R8, 1)
        a.mov_imm(RDX, 45)
        a.store_byte(R8, 0, RDX)
        a.label(write)
        a.mov(RCX, R8)
        a.lea(RDX, "bss", "g_numbuf")
        a.add_imm(RDX, 32)
        a.sub(RDX, R8)
        a.call("rt_write")
        a.add_imm(RSP, 40)
        a.ret()

        # rt_getc -> rax = next input byte, or -1 at the end of input
        have, eof, out = self.label(".in"), self.label(".in"), self.label(".in")
        a.label("rt_getc")
        a.sub_imm(RSP, 40)
        a.load(RAX, "bss", "g_inpos")
        a.load(RCX, "bss", "g_inlen")
        a.cmp(RAX, RCX)
        a.jcc("l", have)
        a.load(RCX, "bss", "g_stdin")
        a.lea(RDX, "bss", "g_inbuf")
        a.mov_imm(R8, 4096)
        a.lea(R9, "bss", "g_inlen")
        a.xor(RAX, RAX)
        a.store_mem(RSP, 32, RAX)
        a.call_import("ReadFile")
        a.xor(RCX, RCX)
        a.store("bss", "g_inpos", RCX)
        a.test(RAX, RAX)
        a.jcc("e", eof)
        a.load(RCX, "bss", "g_inlen")
        a.test(RCX, RCX)
        a.jcc("e", eof)
        a.label(have)
        a.load(RAX, "bss", "g_inpos")
        a.lea(RCX, "bss", "g_inbuf")
        a.add(RCX, RAX)
        a.load_byte(RDX, RCX, 0)
        a.add_imm(RAX, 1)
        a.store("bss", "g_inpos", RAX)
        a.mov(RAX, RDX)
        a.jmp(out)
        a.label(eof)
        a.xor(RCX, RCX)
        a.store("bss", "g_inlen", RCX)
        a.mov_imm(RAX, -1)
        a.label(out)
        a.add_imm(RSP, 40)
        a.ret()

        # rt_read_choice -> rax = number typed, -1 at end of input, -2 if the
        # line held no number (or one too long to be a button)
        loop, end, invalid = self.label(".ch"), self.label(".ch"), self.label(".ch")
        eof, gone, out = self.label(".ch"), self.label(".ch"), self.label(".ch")
        a.label("rt_read_choice")
        a.sub_imm(RSP, 40)
        a.xor(RAX, RAX)
        a.store("bss", "g_acc", RAX)
        a.store("bss", "g_ndig", RAX)
        a.label(loop)
        a.call("rt_getc")
        a.cmp_imm(RAX, -1)
        a.jcc("e", eof)
        a.cmp_imm(RAX, 10)
        a.jcc("e", end)
        a.cmp_imm(RAX, 48)
        a.jcc("l", loop)
        a.cmp_imm(RAX, 57)
        a.jcc("g", loop)
        a.sub_imm(RAX, 48)
        a.mov(RCX, RAX)
        a.load(RAX, "bss", "g_ndig")
        a.add_imm(RAX, 1)
        a.store("bss", "g_ndig", RAX)
        a.cmp_imm(RAX, 6)
        a.jcc("g", loop)               # stop accumulating; `end` rejects it
        a.load(RAX, "bss", "g_acc")
        a.mov_imm(RDX, 10)
        a.imul(RAX, RDX)
        a.add(RAX, RCX)
        a.store("bss", "g_acc", RAX)
        a.jmp(loop)
        a.label(end)
        a.load(RAX, "bss", "g_ndig")
        a.test(RAX, RAX)
        a.jcc("e", invalid)
        a.cmp_imm(RAX, 6)
        a.jcc("g", invalid)
        a.load(RAX, "bss", "g_acc")
        a.jmp(out)
        a.label(invalid)
        a.mov_imm(RAX, -2)
        a.jmp(out)
        a.label(eof)
        a.load(RAX, "bss", "g_ndig")
        a.test(RAX, RAX)
        a.jcc("e", gone)
        a.cmp_imm(RAX, 6)
        a.jcc("g", gone)
        a.load(RAX, "bss", "g_acc")
        a.jmp(out)
        a.label(gone)
        a.mov_imm(RAX, -1)
        a.label(out)
        a.add_imm(RSP, 40)
        a.ret()

        # rt_show_button(rcx = label, rdx = length, r8 = action routine)
        a.label("rt_show_button")
        a.sub_imm(RSP, 40)
        a.store("bss", "g_tptr", RCX)
        a.store("bss", "g_tlen", RDX)
        a.store("bss", "g_tact", R8)
        a.load(RAX, "bss", "g_nbuttons")
        a.mov_imm(RCX, BUTTON_SIZE)
        a.imul(RAX, RCX)
        a.lea(RCX, "bss", "g_buttons")
        a.add(RCX, RAX)
        a.load(RAX, "bss", "g_tptr")
        a.store_mem(RCX, 0, RAX)
        a.load(RAX, "bss", "g_tlen")
        a.store_mem(RCX, 8, RAX)
        a.load(RAX, "bss", "g_tact")
        a.store_mem(RCX, 16, RAX)
        a.load(RAX, "bss", "g_nbuttons")
        a.add_imm(RAX, 1)
        a.store("bss", "g_nbuttons", RAX)
        self.write("  [")
        a.load(RCX, "bss", "g_nbuttons")
        a.call("rt_print_int")
        self.write("] ")
        a.load(RCX, "bss", "g_tptr")
        a.load(RDX, "bss", "g_tlen")
        a.call("rt_write")
        a.call("rt_newline")
        a.add_imm(RSP, 40)
        a.ret()

        # rt_env_equals(rcx = lowercase text, rdx = length) -> rax 1 or 0
        loop, compare = self.label(".env"), self.label(".env")
        yes, no = self.label(".env"), self.label(".env")
        a.label("rt_env_equals")
        a.load(RAX, "bss", "g_envlen")
        a.cmp(RAX, RDX)
        a.jcc("ne", no)
        a.xor(R8, R8)
        a.label(loop)
        a.cmp(R8, RDX)
        a.jcc("e", yes)
        a.lea(R9, "bss", "g_envbuf")
        a.add(R9, R8)
        a.load_byte(RAX, R9, 0)
        a.cmp_imm(RAX, 65)
        a.jcc("l", compare)
        a.cmp_imm(RAX, 90)
        a.jcc("g", compare)
        a.add_imm(RAX, 32)
        a.label(compare)
        a.mov(R9, RCX)
        a.add(R9, R8)
        a.load_byte(R10, R9, 0)
        a.cmp(RAX, R10)
        a.jcc("ne", no)
        a.add_imm(R8, 1)
        a.jmp(loop)
        a.label(yes)
        a.mov_imm(RAX, 1)
        a.ret()
        a.label(no)
        a.xor(RAX, RAX)
        a.ret()

        # rt_init: UTF-8 console, standard handles, empty text space
        a.label("rt_init")
        a.sub_imm(RSP, 40)
        a.mov_imm(RCX, 65001)
        a.call_import("SetConsoleOutputCP")
        a.mov_imm(RCX, 65001)
        a.call_import("SetConsoleCP")
        for handle, symbol in ((-11, "g_stdout"), (-10, "g_stdin"), (-12, "g_stderr")):
            a.mov_imm(RCX, handle)
            a.call_import("GetStdHandle")
            a.store("bss", symbol, RAX)
        a.lea(RAX, "bss", "g_text")
        a.store("bss", "g_texttop", RAX)
        a.add_imm(RSP, 40)
        a.ret()

        # rt_trap(rcx = text, rdx = length, r8 = reason): report on the error
        # stream, then stop with the reason as the exit code (spec/10 10.8).
        # Reached by a jump from anywhere, so the stack is realigned first.
        a.label("rt_trap")
        a.and_imm(RSP, -16)
        a.sub_imm(RSP, 48)
        a.store("bss", "g_reason", R8)
        a.mov(R8, RDX)
        a.mov(RDX, RCX)
        a.load(RCX, "bss", "g_stderr")
        a.lea(R9, "bss", "g_written")
        a.xor(RAX, RAX)
        a.store_mem(RSP, 32, RAX)
        a.call_import("WriteFile")
        a.load(RCX, "bss", "g_reason")
        a.call_import("ExitProcess")

    def emit_traps(self) -> None:
        a = self.asm
        for (reason, _op, _where), (label, text) in self.traps.items():
            symbol, length = self.string(text)
            a.label(label)
            a.lea(RCX, "rdata", symbol)
            a.mov_imm(RDX, length)
            a.mov_imm(R8, reason)
            a.jmp("rt_trap")

    def emit_main(self, start: Optional[str]) -> None:
        a = self.asm
        render, prompt = self.label(".main"), self.label(".main")
        quit_line, finish = self.label(".main"), self.label(".main")
        a.label("main")
        a.sub_imm(RSP, 40)
        a.call("rt_init")
        if self.locales:
            a.call("rt_locale")
        for module in self.order:                  # used modules first
            self.emit_block(module.init, Env(module, "init", "while starting"))
        if start is None:
            a.jmp(finish)
        else:
            a.lea(RAX, "code", start)
            a.store("bss", "g_page", RAX)
            a.label(render)
            a.load(RAX, "bss", "g_page")
            a.call_reg(RAX)
            a.load(RAX, "bss", "g_nbuttons")
            a.test(RAX, RAX)
            a.jcc("e", finish)
            self.write("  [0] Quit\n")
            a.label(prompt)
            self.write("> ")
            a.call("rt_read_choice")
            a.cmp_imm(RAX, -1)
            a.jcc("e", quit_line)
            a.test(RAX, RAX)
            a.jcc("e", quit_line)
            a.cmp_imm(RAX, 1)
            a.jcc("l", prompt)
            a.load(RCX, "bss", "g_nbuttons")
            a.cmp(RAX, RCX)
            a.jcc("g", prompt)
            a.sub_imm(RAX, 1)
            a.mov_imm(RCX, BUTTON_SIZE)
            a.imul(RAX, RCX)
            a.lea(RCX, "bss", "g_buttons")
            a.add(RCX, RAX)
            a.load_mem(RAX, RCX, 16)
            a.xor(RCX, RCX)
            a.store("bss", "g_next", RCX)
            a.call_reg(RAX)
            a.call("rt_newline")
            a.load(RAX, "bss", "g_next")
            a.test(RAX, RAX)
            a.jcc("e", render)
            a.store("bss", "g_page", RAX)
            a.jmp(render)
            a.label(quit_line)
            a.call("rt_newline")
        a.label(finish)
        a.xor(RCX, RCX)
        a.call_import("ExitProcess")

    # -- the whole program -----------------------------------------------

    def compile(self) -> bytes:
        for module in self.order:
            self.collect(module)
        for module in self.order:
            self.check_message_cycles(module)
            for variable in module.variables.values():
                variable.place.type = self.variable_type(variable)
            if module is not self.main and module.opening:
                raise CompileError(
                    "E-0571", "module `{}` is used by another, so what it shows "
                    "must be inside a page".format(module.name))

        self.emit_runtime()
        if self.locales:
            self.emit_locale_detection()
        for module in self.order:
            for folded in module.page_order:
                name, label, node = module.pages[folded]
                self.emit_page(module, label, name, node.get("body", []))

        main = self.main
        start = None
        if main.opening:
            start = main.prefix + "opening"
            self.emit_page(main, start, None, main.opening)
        elif main.page_order:
            start = main.pages[main.page_order[0]][1]
        self.emit_main(start)
        while self.queue:
            self.emit_action(*self.queue.pop(0))
        self.emit_traps()
        return build_pe(self.asm, "main", self.rdata, self.bss, IMPORTS)


def compile_sir(sir: dict, modules: Optional[dict] = None) -> bytes:
    """Canonical SIR in, the bytes of a Windows x86-64 executable out.
    `modules` maps each module name the program uses to that module's SIR."""
    return NativeCompiler(sir, modules).compile()
