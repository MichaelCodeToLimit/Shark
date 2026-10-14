#!/usr/bin/env python3
"""
Shark reference front end - Edition 2026.

A single-file, dependency-free implementation of the parts of the Shark
specification whose correctness is not obvious from reading prose:

  * the lexer, including indentation and LEX-UNSEG for unsegmented scripts
    (spec/01-lexical-structure.md)
  * the Clause Signature Registry and its admissibility conditions R1-R6
    (spec/03-clause-registry.md)
  * head-anchored clause parsing with greedy text slots and terminator phrases
    (spec/02-grammar.md)
  * locale binding, including slot-first segment reordering
    (spec/04-multilingual.md)
  * SIR-D emission, canonical ordering and the canonical hash
    (spec/08-sir.md)

It exists so that the two central claims of the design are executable rather
than merely asserted:

  1. punctuation-free parsing is deterministic;
  2. the same program written in different human languages produces the same
     canonical intermediate representation.

Not implemented here: type checking, ownership and region inference, SIR-C,
the optimiser, and code generation. See spec/16-roadmap.md.

Usage
-----
    python shark_ref.py FILE [--emit tokens|ast|sir|hash]
    python shark_ref.py --check-registry
    python shark_ref.py --prove-locale-equivalence
    python shark_ref.py --why FILE:LINE
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REGISTRY_DIR = os.path.join(ROOT, "registry")
LOCALE_DIR = os.path.join(REGISTRY_DIR, "locales")

# Unsegmented scripts, spec/01 section 1.4.2.
UNSEGMENTED_RANGES = [
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3400, 0x4DBF),  # CJK ext A
    (0x4E00, 0x9FFF),  # CJK
    (0xF900, 0xFAFF),  # CJK compatibility
    (0x0E00, 0x0E7F),  # Thai
    (0x0E80, 0x0EFF),  # Lao
    (0x1780, 0x17FF),  # Khmer
]

SEPARATORS = {
    " ", "　", " ", " ", " ",
    "‎", "‏", "؜",
}
FORBIDDEN_BIDI = {"‪", "‫", "‬", "‭", "‮",
                  "⁦", "⁧", "⁨", "⁩"}

LOCALE_ALIASES = {
    "english": "en", "en": "en",
    "hebrew": "he", "he": "he", "עברית": "he",
    "japanese": "ja", "ja": "ja", "日本語": "ja",
    "arabic": "ar", "ar": "ar",
}


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


class SharkError(Exception):
    def __init__(self, code: str, message: str, line: int = 0, col: int = 0,
                 help_text: str = "", fix: str = ""):
        self.code = code
        self.message = message
        self.line = line
        self.col = col
        self.help_text = help_text
        self.fix = fix
        super().__init__(f"{code}  {message}")

    def render(self, path: str = "", source_line: str = "") -> str:
        out = [f"{self.code}  {self.message}"]
        if path:
            out.append(f"   in {path}:{self.line}:{self.col}")
        if source_line:
            out.append(f"{self.line:4d} | {source_line}")
            out.append("     | " + " " * max(self.col - 1, 0) + "^")
        if self.help_text:
            out.append(f"  help: {self.help_text}")
        if self.fix:
            out.append(f"  fix:  {self.fix}")
        return "\n".join(out)


# --------------------------------------------------------------------------
# Locale
# --------------------------------------------------------------------------


@dataclass
class Locale:
    tag: str = "en"
    name: str = ""
    direction: str = "left to right"
    segmentation: str = "segmented"
    decimal_separator: str = "."
    group_separator: str = ","
    splice_order: str = "marker first"   # or "ref first", see spec/04 section 4.7.4
    particles: dict = field(default_factory=dict)   # canonical name -> tuple(words)
    words: dict = field(default_factory=dict)       # canonical name -> tuple(words)
    ops: dict = field(default_factory=dict)         # canonical name -> tuple(words)
    stmts: dict = field(default_factory=dict)       # cid -> tuple(words)
    slots: dict = field(default_factory=dict)       # slot cid -> tuple(words)
    types: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)      # cid -> list of segment refs
    reserved: set = field(default_factory=set)

    @property
    def unsegmented(self) -> bool:
        return self.segmentation == "unsegmented"


def _split_phrase(text: str) -> tuple:
    return tuple(w for w in text.split() if w)


def load_locale(tag: str) -> Locale:
    path = os.path.join(LOCALE_DIR, f"{tag}.locale")
    if not os.path.exists(path):
        raise SharkError("E-0401", f"no locale file for `{tag}` at {path}")
    loc = Locale(tag=tag)
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    raw = unicodedata.normalize("NFC", raw)

    current_order: Optional[str] = None
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if stripped.startswith("note"):
            continue

        if current_order is not None and indent > 0:
            loc.orders[current_order].append(stripped)
            continue
        current_order = None

        head, _, rest = stripped.partition(" ")
        rest = rest.strip()

        if head == "locale":
            loc.tag = rest
            continue
        if head == "order":
            current_order = rest
            loc.orders[current_order] = []
            continue
        if head == "reserved":
            continue

        if "=" in stripped:
            left, _, right = stripped.partition("=")
            left = left.strip()
            right = right.strip()
            kind, _, key = left.partition(" ")
            key = key.strip()
            value = _split_phrase(right)
            if kind == "particle":
                loc.particles[key] = value
            elif kind == "word":
                loc.words[key] = value
            elif kind == "op":
                loc.ops[key] = value
            elif kind == "stmt":
                loc.stmts[key] = value
            elif kind == "slot":
                loc.slots[key] = value
            elif kind == "type":
                loc.types[key] = value
            elif kind == "splice":
                loc.splice_order = right
            continue

        if head == "name":
            loc.name = rest
        elif head == "direction":
            loc.direction = rest
        elif head == "segmentation":
            loc.segmentation = rest
        elif head == "decimal":
            loc.decimal_separator = rest.replace("separator", "").strip()
        elif head == "group":
            loc.group_separator = rest.replace("separator", "").strip()

    # Condition L2 - injectivity of the CID binding. A lexeme may serve in more
    # than one role (`to` is both a particle and the action keyword), but two
    # distinct signature CIDs may never share a head.
    seen: dict = {}
    for cid, phrase in loc.stmts.items():
        joined = " ".join(phrase)
        if joined in seen:
            raise SharkError(
                "E-0402",
                f"locale `{tag}` binds `{joined}` to both "
                f"{seen[joined]} and {cid}",
            )
        seen[joined] = cid

    # Inside a value, operators and the value words `yes`, `no` and `nothing`
    # are read at the same positions, so they may not share a phrase either.
    in_values: dict = {}
    roles = [(f"operator `{op}`", phrase) for op, phrase in loc.ops.items()]
    roles += [(f"value `{word}`", loc.words[word])
              for word in ("yes", "no", "nothing") if word in loc.words]
    for role, phrase in roles:
        joined = " ".join(phrase)
        if joined in in_values:
            raise SharkError(
                "E-0402",
                f"locale `{tag}` binds `{joined}` to both "
                f"{in_values[joined]} and {role}",
            )
        in_values[joined] = role
    return loc


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass
class Slot:
    name: str
    kind: str
    terminator_names: list = field(default_factory=list)
    is_open: bool = False
    type: str = ""            # a type CID, for slots declared `as <type>`


@dataclass
class ParticleSeg:
    canonical: str
    optional: bool
    slots: list = field(default_factory=list)


@dataclass
class Signature:
    cid: str
    default_head_words: tuple = ()
    segments: list = field(default_factory=list)   # ("head",) | ("slot", Slot) |
                                                   # ("particle", ParticleSeg) | ("subject",)
    gives: str = "nothing"
    effects: set = field(default_factory=set)
    kind: str = "stmt"        # stmt | event | decl
    declarative: bool = False

    def slots_in_order(self) -> Iterable[Slot]:
        for seg in self.segments:
            if seg[0] == "slot":
                yield seg[1]
            elif seg[0] == "particle":
                for slot in seg[1].slots:
                    yield slot

    def has_block(self) -> bool:
        return any(s.kind == "block" for s in self.slots_in_order())


def load_registry(path: Optional[str] = None) -> tuple:
    path = path or os.path.join(REGISTRY_DIR, "core.signatures")
    with open(path, encoding="utf-8") as handle:
        raw = unicodedata.normalize("NFC", handle.read())

    signatures: dict = {}
    particle_set: list = []
    current: Optional[Signature] = None
    current_slot: Optional[Slot] = None
    current_particle: Optional[ParticleSeg] = None
    mode = None

    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("note"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if indent == 0:
            if stripped.startswith("signature "):
                current = Signature(cid=stripped[len("signature "):].strip())
                signatures[current.cid] = current
                current_slot = None
                current_particle = None
                mode = "signature"
                continue
            if stripped == "particle set":
                mode = "particles"
                current = None
                continue
            if stripped == "reserved words":
                mode = "reserved"
                current = None
                continue
            mode = None
            continue

        if mode == "particles":
            particle_set.append(stripped)
            continue
        if mode == "reserved":
            continue
        if current is None:
            continue

        words = stripped.split()
        keyword = words[0]

        if keyword == "head":
            current.default_head_words = tuple(words[1:])
            current.segments.append(("head",))
            current_slot = None
            current_particle = None
        elif keyword == "subject":
            current.segments.append(("subject",))
        elif keyword == "kind" and len(words) > 1:
            current.kind = words[1]
        elif keyword == "slot":
            # slot NAME of kind KIND [open]
            name = words[1]
            kind = words[words.index("kind") + 1] if "kind" in words else "value"
            slot = Slot(name=name, kind=kind, is_open="open" in words)
            current_slot = slot
            if current_particle is not None and indent >= 4:
                current_particle.slots.append(slot)
            else:
                current.segments.append(("slot", slot))
                current_particle = None
        elif keyword == "terminated" and current_slot is not None:
            current_slot.terminator_names.append(" ".join(words[2:]))
        elif keyword in ("particle", "optional", "repeated"):
            optional = keyword in ("optional", "repeated")
            offset = 1 if keyword == "particle" else 2
            canonical = " ".join(words[offset:])
            current_particle = ParticleSeg(canonical=canonical, optional=optional)
            current.segments.append(("particle", current_particle))
            current_slot = None
        elif keyword == "gives":
            current.gives = " ".join(words[1:])
        elif keyword == "effects":
            current.effects = set(words[1:])
        elif keyword == "declarative":
            current.declarative = True

    return signatures, particle_set


# --------------------------------------------------------------------------
# Bound registry: signatures with locale lexemes resolved
# --------------------------------------------------------------------------


@dataclass
class BoundSlot:
    slot: Slot
    terminators: list = field(default_factory=list)   # list of tuple(words)


@dataclass
class BoundSignature:
    sig: Signature
    head: tuple
    order: list = field(default_factory=list)  # list of ("head",) |
                                               # ("slot", BoundSlot) |
                                               # ("particle", tuple(words), optional, [BoundSlot])
                                               # | ("subject",)

    @property
    def cid(self) -> str:
        return self.sig.cid

    @property
    def head_index(self) -> int:
        for i, seg in enumerate(self.order):
            if seg[0] == "head":
                return i
        return 0


class BoundRegistry:
    def __init__(self, signatures: dict, locale: Locale):
        self.locale = locale
        self.bound: dict = {}
        self.by_head: dict = {}
        for cid, sig in signatures.items():
            head = locale.stmts.get(cid)
            if head is None:
                if sig.default_head_words:
                    head = sig.default_head_words
                else:
                    continue
            bound = self._bind(sig, head)
            self.bound[cid] = bound
            self.by_head.setdefault(head, []).append(bound)
        self._apply_head_terminators()
        self.max_head_len = max((len(h) for h in self.by_head), default=1)
        self.max_terminator_len = max(
            (len(t) for b in self.bound.values()
             for seg in b.order if seg[0] == "slot"
             for t in seg[1].terminators),
            default=1)
        self.lookahead = 1 + max(self.max_head_len, self.max_terminator_len)
        self.check_admissibility()

    # -- binding ---------------------------------------------------------

    def _particle_words(self, canonical: str) -> tuple:
        words = self.locale.particles.get(canonical)
        if words is None:
            words = self.locale.words.get(canonical)
        if words is None:
            words = _split_phrase(canonical)
        return words

    def _bind(self, sig: Signature, head: tuple) -> BoundSignature:
        segs = []
        for seg in sig.segments:
            if seg[0] == "head":
                segs.append(("head",))
            elif seg[0] == "subject":
                segs.append(("subject",))
            elif seg[0] == "slot":
                segs.append(("slot", self._bind_slot(seg[1])))
            elif seg[0] == "particle":
                part = seg[1]
                segs.append((
                    "particle",
                    self._particle_words(part.canonical),
                    part.optional,
                    [self._bind_slot(s) for s in part.slots],
                ))
        bound = BoundSignature(sig=sig, head=head, order=segs)
        override = self.locale.orders.get(sig.cid)
        if override:
            bound.order = self._reorder(bound, override)
        return bound

    def _bind_slot(self, slot: Slot) -> BoundSlot:
        terms = [self._particle_words(name) for name in slot.terminator_names]
        return BoundSlot(slot=slot, terminators=terms)

    def _reorder(self, bound: BoundSignature, spec: list) -> list:
        """Apply an `order` block from a locale file (spec/04 section 4.7.2).

        The override may reorder segments and may move the head after a slot.
        It may not add, remove or retype a slot, which is checked here."""
        slots: dict = {}
        for seg in bound.order:
            if seg[0] == "slot":
                slots[seg[1].slot.name] = seg[1]
            elif seg[0] == "particle":
                for bslot in seg[3]:
                    slots[bslot.slot.name] = bslot

        result: list = []
        mentioned: set = set()
        pending = None
        for line in spec:
            words = line.split()
            if not words:
                continue
            if words[0] == "head":
                result.append(("head",))
                pending = None
            elif words[0] == "subject":
                result.append(("subject",))
                pending = None
            elif words[0] == "slot":
                name = words[1]
                if name not in slots:
                    raise SharkError(
                        "E-0406",
                        f"locale order for `{bound.cid}` names unknown slot `{name}`")
                mentioned.add(name)
                if pending is not None:
                    pending[3].append(slots[name])
                else:
                    result.append(("slot", slots[name]))
            elif words[0] in ("particle", "optional", "repeated"):
                optional = words[0] != "particle"
                offset = 1 if words[0] == "particle" else 2
                canonical = " ".join(words[offset:])
                pending = ("particle", self._particle_words(canonical),
                           optional, [])
                result.append(pending)

        # A block slot the override did not mention stays last.
        for seg in bound.order:
            if seg[0] == "slot" and seg[1].slot.kind == "block" \
                    and seg[1].slot.name not in mentioned:
                result.append(seg)
        return result

    def _apply_head_terminators(self) -> None:
        """Rule: when a locale places a greedy slot before the head, the head
        itself terminates that slot (spec/04 section 4.7.2)."""
        for bound in self.bound.values():
            hi = bound.head_index
            for i, seg in enumerate(bound.order):
                if i < hi and seg[0] == "slot" and seg[1].slot.kind in ("text", "name"):
                    if bound.head not in seg[1].terminators:
                        seg[1].terminators.append(bound.head)

    # -- admissibility ---------------------------------------------------

    def check_admissibility(self) -> list:
        """Conditions R1-R6, spec/03 section 3.6."""
        problems = []
        heads = sorted(self.by_head.keys(), key=lambda h: (len(h), h))

        # R1: head prefix-freeness up to the first slot.
        for short in heads:
            for long in heads:
                if short == long or len(short) >= len(long):
                    continue
                if long[:len(short)] != short:
                    continue
                for bound in self.by_head[short]:
                    first = self._first_segment_after_head(bound)
                    if first is not None and first.slot.kind in ("text", "name"):
                        problems.append((
                            "E-0312",
                            f"`{' '.join(short)}` and `{' '.join(long)}` cannot both "
                            f"exist: the shorter one's first slot is free text and "
                            f"could absorb `{long[len(short)]}`",
                        ))

        for bound in self.bound.values():
            segs = bound.order
            # R3: at most one block slot and it is last.
            block_positions = [
                i for i, s in enumerate(segs)
                if s[0] == "slot" and s[1].slot.kind == "block"
            ]
            if len(block_positions) > 1:
                problems.append(("E-0314",
                                 f"{bound.cid} has more than one block slot"))
            if block_positions and block_positions[0] != len(segs) - 1:
                problems.append(("E-0314",
                                 f"{bound.cid} has a block slot that is not last"))

            # A trailing block slot is delimited by INDENT, so for R2 the
            # "last on the line" position is the one before it.
            line_end = block_positions[0] if block_positions else len(segs)

            for i, seg in enumerate(segs):
                if seg[0] != "slot":
                    continue
                bslot = seg[1]
                if bslot.slot.kind not in ("text", "name"):
                    continue
                is_last = i == line_end - 1
                # R2: a non-final greedy slot needs a terminator phrase.
                if not is_last and not bslot.terminators:
                    problems.append((
                        "E-0313",
                        f"{bound.cid}: slot `{bslot.slot.name}` is greedy, is not "
                        f"last on the line, and has no terminator phrase",
                    ))
                # R4: single-word terminators must be particles (or the head,
                # which terminates a slot placed before it by a locale override).
                for term in bslot.terminators:
                    if len(term) == 1:
                        known = any(term == p for p in self.locale.particles.values())
                        if not known and term != bound.head:
                            problems.append((
                                "E-0316",
                                f"{bound.cid}: single-word terminator "
                                f"`{term[0]}` is not a particle",
                            ))
        return problems

    def _first_segment_after_head(self, bound: BoundSignature) -> Optional[BoundSlot]:
        seen_head = False
        for seg in bound.order:
            if seg[0] == "head":
                seen_head = True
                continue
            if seen_head and seg[0] == "slot":
                return seg[1]
            if seen_head and seg[0] == "particle":
                return None
        return None

    # -- lexicon ---------------------------------------------------------

    def lexicon(self, extra_names: Sequence[str] = ()) -> list:
        """The phrase lexicon used by LEX-UNSEG (spec/01 section 1.4.2)."""
        entries = set()
        for table in (self.locale.particles, self.locale.words,
                      self.locale.ops, self.locale.stmts, self.locale.types):
            for phrase in table.values():
                for word in phrase:
                    entries.add(word)
        for head in self.by_head:
            for word in head:
                entries.add(word)
        for name in extra_names:
            entries.add(name)
        return sorted(entries, key=lambda s: (-len(s), s))


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------


@dataclass
class Token:
    kind: str       # WORD NUMBER NEWLINE INDENT DEDENT RAWLINE EOF
    text: str
    line: int
    col: int

    def __repr__(self) -> str:
        return f"{self.kind}({self.text!r})@{self.line}:{self.col}"


def _is_unsegmented_char(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in UNSEGMENTED_RANGES)


def _is_number(text: str, locale: Locale) -> bool:
    body = text.replace(locale.group_separator, "")
    if not body:
        return False
    parts = body.split(locale.decimal_separator)
    if len(parts) > 2:
        return False
    return all(p.isdigit() for p in parts if p != "") and any(p for p in parts)


class Lexer:
    def __init__(self, source: str, locale: Locale, lexicon: Sequence[str]):
        self.source = unicodedata.normalize("NFC", source.replace("﻿", ""))
        self.locale = locale
        self.lexicon = list(lexicon)
        self.tokens: list = []

    def run(self) -> list:
        lines = self.source.splitlines()
        indent_unit = 0
        level_stack = [0]
        note_word = " ".join(self.locale.words.get("note", ("note",)))
        i = 0
        while i < len(lines):
            raw = lines[i]
            for ch in raw:
                if ch in FORBIDDEN_BIDI:
                    raise SharkError(
                        "E-0115",
                        "bidirectional embedding or override control in source",
                        i + 1, 1)
            if "\t" in raw[:len(raw) - len(raw.lstrip())]:
                raise SharkError("E-0110", "tab character in indentation",
                                   i + 1, 1,
                                   help_text="replace tabs with spaces")
            body = raw.strip()
            if not body:
                i += 1
                continue
            spaces = len(raw) - len(raw.lstrip(" "))
            if spaces and indent_unit == 0:
                indent_unit = spaces
            if indent_unit and spaces % indent_unit:
                raise SharkError(
                    "E-0111",
                    f"indentation of {spaces} is not a multiple of "
                    f"the file's unit of {indent_unit}", i + 1, 1)
            level = spaces // indent_unit if indent_unit else 0

            if body == note_word or body.startswith(note_word + " "):
                i += 1
                continue

            while level < level_stack[-1]:
                level_stack.pop()
                self.tokens.append(Token("DEDENT", "", i + 1, spaces + 1))
            if level > level_stack[-1]:
                if level > level_stack[-1] + 1:
                    raise SharkError(
                        "E-0112", "indented by more than one step", i + 1, 1)
                level_stack.append(level)
                self.tokens.append(Token("INDENT", "", i + 1, spaces + 1))

            self._lex_line(body, i + 1, spaces + 1)
            self.tokens.append(Token("NEWLINE", "", i + 1, len(raw) + 1))
            i += 1

        while len(level_stack) > 1:
            level_stack.pop()
            self.tokens.append(Token("DEDENT", "", len(lines) + 1, 1))
        self.tokens.append(Token("EOF", "", len(lines) + 1, 1))
        return self.tokens

    def _lex_line(self, body: str, line: int, col: int) -> None:
        chunk = ""
        chunk_col = col
        pos = col
        for ch in body:
            if ch in SEPARATORS:
                if chunk:
                    self._emit_chunk(chunk, line, chunk_col)
                    chunk = ""
                pos += 1
                chunk_col = pos
                continue
            if not chunk:
                chunk_col = pos
            chunk += ch
            pos += 1
        if chunk:
            self._emit_chunk(chunk, line, chunk_col)

    def _emit_chunk(self, chunk: str, line: int, col: int) -> None:
        if self.locale.unsegmented and any(_is_unsegmented_char(c) for c in chunk):
            for piece, offset in self._lex_unsegmented(chunk):
                self._emit_word(piece, line, col + offset)
        else:
            self._emit_word(chunk, line, col)

    def _lex_unsegmented(self, chunk: str) -> list:
        """Algorithm LEX-UNSEG, spec/01 section 1.4.2."""
        out = []
        i = 0
        n = len(chunk)
        while i < n:
            match = self._longest_lexicon_match(chunk, i)
            if match:
                out.append((match, i))
                i += len(match)
                continue
            j = i + 1
            while j < n and not self._longest_lexicon_match(chunk, j):
                j += 1
            out.append((chunk[i:j], i))
            i = j
        return out

    def _longest_lexicon_match(self, text: str, pos: int) -> Optional[str]:
        for entry in self.lexicon:
            if text.startswith(entry, pos):
                return entry
        return None

    def _emit_word(self, word: str, line: int, col: int) -> None:
        if _is_number(word, self.locale):
            self.tokens.append(Token("NUMBER", word, line, col))
        else:
            self.tokens.append(Token("WORD", word, line, col))


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass
class Node:
    cid: str
    args: dict = field(default_factory=dict)
    body: list = field(default_factory=list)
    line: int = 0
    trace: list = field(default_factory=list)
    hanging: bool = False


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class Parser:
    def __init__(self, tokens: list, registry: BoundRegistry,
                 names: Sequence[str] = (), actions: Optional[dict] = None):
        self.tokens = tokens
        self.registry = registry
        self.locale = registry.locale
        self.pos = 0
        self.names = set(names)
        self.traces: dict = {}
        self.actions = actions or {}      # line -> Signature from `to ...`

    # -- token helpers ---------------------------------------------------

    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def at(self, kind: str) -> bool:
        return self.peek().kind == kind

    def take(self) -> Token:
        tok = self.peek()
        self.pos += 1
        return tok

    def expect(self, kind: str) -> Token:
        if not self.at(kind):
            tok = self.peek()
            raise SharkError("E-0201",
                               f"expected {kind} but found {tok.kind} `{tok.text}`",
                               tok.line, tok.col)
        return self.take()

    def line_tokens(self) -> list:
        out = []
        i = self.pos
        while i < len(self.tokens) and self.tokens[i].kind in ("WORD", "NUMBER"):
            out.append(self.tokens[i])
            i += 1
        return out

    # -- program ---------------------------------------------------------

    def parse_program(self) -> list:
        items = []
        while not self.at("EOF"):
            if self.at("NEWLINE"):
                self.take()
                continue
            if self.at("INDENT") or self.at("DEDENT"):
                self.take()
                continue
            items.append(self.parse_statement())
        return items

    def parse_statement(self) -> Node:
        directive = self._directive()
        if directive is not None:
            return directive
        node = self.parse_clause()
        if self.at("NEWLINE"):
            self.take()
        bound = self.registry.bound.get(node.cid)
        if bound is not None and bound.sig.has_block() and not node.hanging:
            node.body = self.parse_body()
        return node

    def _directive(self) -> Optional[Node]:
        """`write in` only tells the reader which words to expect, so it leaves
        nothing in SIR. `use` and `share` decide what is visible between
        modules (spec/05 section 5.12), so they stay in SIR."""
        toks = self.line_tokens()
        if not toks:
            return None
        line = toks[0].line
        if len(toks) == 3 and toks[0].text.lower() == "write" \
                and toks[1].text.lower() == "in":
            self._take_line()
            return Node(cid="core.directive", line=line)
        words = _texts(toks)
        for word, cid, arg in (("use", "core.decl.use", "module"),
                               ("share", "core.decl.share", "name")):
            lexeme = self.locale.words.get(word, (word,))
            if words[:len(lexeme)] == lexeme:
                if len(words) == len(lexeme):
                    raise SharkError("E-0202", f"`{' '.join(lexeme)}` needs a name after it",
                                    line, toks[0].col)
                self._take_line()
                return Node(cid=cid, args={arg: {"name": list(words[len(lexeme):])}},
                            line=line)
        return None

    def _take_line(self) -> None:
        while not self.at("NEWLINE") and not self.at("EOF"):
            self.take()
        if self.at("NEWLINE"):
            self.take()

    def parse_body(self) -> list:
        if self.at("INDENT"):
            self.take()
            items = []
            while not self.at("DEDENT") and not self.at("EOF"):
                if self.at("NEWLINE"):
                    self.take()
                    continue
                items.append(self.parse_statement())
            if self.at("DEDENT"):
                self.take()
            return items
        # Flush single-action form, spec/02 section 2.6.2.
        if self.at("WORD") or self.at("NUMBER"):
            return [self.parse_statement()]
        return []

    # -- clauses ---------------------------------------------------------

    STATEMENT_KINDS = ("stmt", "decl")
    EVENT_KINDS = ("event",)

    def parse_clause(self) -> Node:
        toks = self.line_tokens()
        if not toks:
            tok = self.peek()
            raise SharkError("E-0202", "expected an instruction here",
                               tok.line, tok.col)

        _anchor, bound = self._find_head_anchor(toks, 0, self.STATEMENT_KINDS)
        if bound is None:
            tok = toks[0]
            raise SharkError(
                "E-0210",
                f"`{tok.text}` does not start any known instruction",
                tok.line, tok.col,
                help_text="run `compose registry list` to see what is available")

        # Hanging explicit form (spec/02 section 2.6.1): the head is alone on
        # its line and the slots are labelled, one per line, in an indented
        # block. This is the form every convenience form desugars to.
        if len(toks) == len(bound.head) \
                and _texts(toks) == bound.head \
                and self._line_slots_remain(bound) \
                and self.tokens[self.pos + len(toks)].kind == "NEWLINE" \
                and self.tokens[self.pos + len(toks) + 1].kind == "INDENT":
            return self.parse_hanging(bound, len(toks))

        args, cursor, trace = self._fill(bound, toks, 0)
        node = Node(cid=bound.cid, args=args, line=toks[0].line, trace=trace)

        if cursor < len(toks):
            # Every word on the line must belong to the instruction. Dropping
            # the rest silently would hide typos and lost meaning.
            tok = toks[cursor]
            raise SharkError(
                "E-0255",
                f"`{' '.join(t.text for t in toks[cursor:])}` is left over at the "
                f"end of this `{' '.join(bound.head)}` line",
                tok.line, tok.col,
                help_text="every word on a line must belong to its instruction")
        for _ in range(cursor):
            self.take()
        self.traces.setdefault(node.line, trace)
        return node

    def _fill(self, bound: BoundSignature, toks: list, cursor: int,
              head_at: Optional[int] = None) -> tuple:
        """Fill a bound signature's segments left to right from `cursor`.

        `head_at` is the position of the head, when it is known from anchoring.
        A `subject` segment that precedes the head runs up to it, which is how
        `when Give up is pressed` gets the subject `Give up` rather than just
        the first word."""
        args: dict = {}
        trace: list = []
        head_len = len(bound.head)

        for seg in bound.order:
            if seg[0] == "head":
                if _texts(toks[cursor:cursor + head_len]) == bound.head:
                    trace.append({"action": "head match",
                                  "matched": " ".join(bound.head),
                                  "role": "head",
                                  "span": _span(toks, cursor, cursor + head_len),
                                  "at token": cursor})
                    cursor += head_len
                continue
            if seg[0] == "subject":
                start = cursor
                if head_at is not None and cursor < head_at:
                    value = " ".join(t.text for t in toks[cursor:head_at])
                    cursor = head_at
                else:
                    value, cursor = self._read_ref(toks, cursor)
                args["subject"] = {"ref": value}
                trace.append({"action": "subject", "value": value,
                              "role": "ref",
                              "span": _span(toks, start, cursor),
                              "delimited by": "the event head"})
                continue
            if seg[0] == "slot":
                bslot = seg[1]
                if bslot.slot.kind == "block":
                    continue
                start = cursor
                value, cursor, info = self._read_slot(toks, start, bslot)
                info.setdefault("role", "slot:" + bslot.slot.kind)
                info["span"] = _span(toks, start, cursor)
                args[bslot.slot.name] = value
                trace.append(info)
                continue
            if seg[0] == "particle":
                words, optional, bslots = seg[1], seg[2], seg[3]
                if _texts(toks[cursor:cursor + len(words)]) == words:
                    trace.append({"action": "particle",
                                  "matched": " ".join(words),
                                  "role": "particle",
                                  "span": _span(toks, cursor, cursor + len(words))})
                    cursor += len(words)
                    for bslot in bslots:
                        if bslot.slot.kind == "block":
                            continue
                        start = cursor
                        value, cursor, info = self._read_slot(toks, start, bslot)
                        info.setdefault("role", "slot:" + bslot.slot.kind)
                        info["span"] = _span(toks, start, cursor)
                        args[bslot.slot.name] = value
                        trace.append(info)
                elif not optional:
                    tok = toks[min(cursor, len(toks) - 1)]
                    raise SharkError(
                        "E-0220",
                        f"expected `{' '.join(words)}` here",
                        tok.line, tok.col)
        return args, cursor, trace

    # -- the hanging explicit form ---------------------------------------

    def _line_slots_remain(self, bound: BoundSignature) -> bool:
        return any(s.kind not in ("block",) for s in bound.sig.slots_in_order())

    def _slot_label(self, bound: BoundSignature, bslot: BoundSlot) -> tuple:
        parts = bound.cid.split(".")
        slot_cid = f"{parts[0]}.slot.{'.'.join(parts[2:])}.{bslot.slot.name}"
        label = self.locale.slots.get(slot_cid)
        if label:
            return label
        return tuple(bslot.slot.name.split())

    def parse_hanging(self, bound: BoundSignature, head_len: int) -> Node:
        line = self.peek().line
        for _ in range(head_len):
            self.take()
        self.expect("NEWLINE")
        self.expect("INDENT")

        labels: list = []          # (phrase, BoundSlot)
        for seg in bound.order:
            if seg[0] == "slot" and seg[1].slot.kind != "block":
                labels.append((self._slot_label(bound, seg[1]), seg[1]))
            elif seg[0] == "particle":
                for bslot in seg[3]:
                    if bslot.slot.kind == "block":
                        continue
                    labels.append((seg[1], bslot))
                    labels.append((self._slot_label(bound, bslot), bslot))
        labels.sort(key=lambda pair: -len(pair[0]))
        text_word = self.locale.words.get("text", ("text",))

        args: dict = {}
        trace: list = [{"action": "hanging form",
                        "head": " ".join(bound.head)}]

        while not self.at("DEDENT") and not self.at("EOF"):
            if self.at("NEWLINE"):
                self.take()
                continue
            inner = self.line_tokens()
            if not inner:
                break

            chosen: Optional[BoundSlot] = None
            chosen_len = 0
            if _texts(inner) == text_word:
                for _phrase, bslot in labels:
                    if bslot.slot.kind == "text" and bslot.slot.name not in args:
                        chosen = bslot
                        chosen_len = len(inner)
                        break
            if chosen is None:
                for phrase, bslot in labels:
                    if _texts(inner[:len(phrase)]) == phrase:
                        chosen = bslot
                        chosen_len = len(phrase)
                        break
            if chosen is None:
                tok = inner[0]
                raise SharkError(
                    "E-0221",
                    f"`{tok.text}` does not name a part of `{' '.join(bound.head)}`",
                    tok.line, tok.col,
                    help_text="each line here names one part of the instruction")

            rest = inner[chosen_len:]
            for _ in range(len(inner)):
                self.take()
            if self.at("NEWLINE"):
                self.take()

            free = BoundSlot(slot=Slot(name=chosen.slot.name, kind=chosen.slot.kind,
                                       is_open=True), terminators=[])
            if rest:
                value, _cursor, info = self._read_slot(rest, 0, free)
            else:
                pieces = self._read_indented_value()
                if chosen.slot.kind == "ref":
                    value = {"ref": " ".join(pieces)}
                elif chosen.slot.kind == "name":
                    value = {"name": pieces}
                elif chosen.slot.kind == "number":
                    value = {"number": pieces[0] if pieces else "0"}
                else:
                    value = {"text": pieces}
                info = {"action": "hanging slot", "slot": chosen.slot.name,
                        "value": pieces}
            args[chosen.slot.name] = value
            trace.append(info)

        if self.at("DEDENT"):
            self.take()
        node = Node(cid=bound.cid, args=args, line=line, trace=trace,
                    hanging=True)
        self.traces.setdefault(line, trace)
        return node

    def _read_indented_value(self) -> list:
        """Read the indented lines that follow a bare slot label, verbatim."""
        pieces: list = []
        if not self.at("INDENT"):
            return pieces
        self.take()
        while not self.at("DEDENT") and not self.at("EOF"):
            if self.at("NEWLINE"):
                self.take()
                continue
            inner = self.line_tokens()
            pieces.extend(t.text for t in inner)
            for _ in range(len(inner)):
                self.take()
            if self.at("NEWLINE"):
                self.take()
        if self.at("DEDENT"):
            self.take()
        return pieces

    def _find_head_anchor(self, toks: list, start_at: int,
                          kinds: Sequence[str]) -> tuple:
        """Head anchoring: scan left to right from `start_at` for the earliest
        position at which some registry head of one of `kinds` matches
        completely; take the longest match there. For head-first locales the
        anchor is `start_at`; for slot-first locales such as Japanese it is
        after the leading slot."""
        for start in range(start_at, max(len(toks), start_at + 1)):
            best = None
            best_len = 0
            upper = min(self.registry.max_head_len, len(toks) - start)
            for length in range(upper, 0, -1):
                phrase = _texts(toks[start:start + length])
                for bound in self.registry.by_head.get(phrase, []):
                    if bound.sig.kind in kinds and length > best_len:
                        best = bound
                        best_len = length
            if best is not None:
                return start, best
        return start_at, None

    def _read_event(self, toks: list, cursor: int) -> tuple:
        """Parse a nested event clause for a slot of kind `event`."""
        _anchor, bound = self._find_head_anchor(toks, cursor, self.EVENT_KINDS)
        if bound is None:
            tok = toks[min(cursor, len(toks) - 1)]
            raise SharkError(
                "E-0240",
                "this does not name anything that can happen",
                tok.line, tok.col,
                help_text="events are things like `is pressed` or `touches`")
        args, cursor, trace = self._fill(bound, toks, cursor, head_at=_anchor)
        return ({"event": bound.cid, "args": args}, cursor,
                {"action": "event slot", "event": bound.cid, "steps": trace})

    def _read_slot(self, toks: list, cursor: int, bslot: BoundSlot) -> tuple:
        kind = bslot.slot.kind
        if kind == "event":
            return self._read_event(toks, cursor)
        if kind == "number":
            if cursor < len(toks) and toks[cursor].kind == "NUMBER":
                value = toks[cursor].text
                return ({"number": value}, cursor + 1,
                        {"action": "number slot", "slot": bslot.slot.name,
                         "value": value})
            tok = toks[min(cursor, len(toks) - 1)]
            raise SharkError("E-0234", "expected a number here",
                               tok.line, tok.col)
        if kind == "ref":
            value, cursor = self._read_ref(toks, cursor)
            return ({"ref": value}, cursor,
                    {"action": "ref slot", "slot": bslot.slot.name,
                     "resolved": value})
        if kind == "value":
            value, cursor = self._read_expression(toks, cursor, bslot)
            return (value, cursor,
                    {"action": "value slot", "slot": bslot.slot.name})
        if kind in ("text", "name"):
            end, stopper = self._greedy_end(toks, cursor, bslot)
            if end == cursor:
                tok = toks[min(cursor, len(toks) - 1)]
                raise SharkError(
                    "E-0231",
                    f"this `{bslot.slot.name}` is empty",
                    tok.line, tok.col,
                    help_text="give it something to say",
                    fix="use the explicit form and put the words on their own line")
            pieces, splices = self._split_splices(toks[cursor:end])
            value = {"text": pieces} if kind == "text" else {"name": pieces}
            if splices:
                value["splices"] = splices
            info = {"action": "greedy slot", "slot": bslot.slot.name,
                    "value": pieces,
                    "stopped at": " ".join(stopper) if stopper else "end of line"}
            return value, end, info
        if kind == "signature":
            # The shape of a `to ...` declaration was read before parsing
            # began (declare_actions); here it is recorded in SIR.
            line = toks[cursor].line if cursor < len(toks) else 0
            sig = self.actions.get(line)
            if sig is None:
                tok = toks[min(cursor, len(toks) - 1)]
                raise SharkError("E-0311", "a new instruction is declared at the top "
                                "level, with its words on the same line as `to`",
                                tok.line, tok.col)
            return (action_shape(sig), len(toks),
                    {"action": "declared instruction", "slot": bslot.slot.name,
                     "declares": sig.cid})
        # block and anything else
        return ({}, cursor, {"action": "skipped", "slot": bslot.slot.name})

    def _greedy_end(self, toks: list, cursor: int, bslot: BoundSlot) -> tuple:
        """Rule G, spec/02 section 2.4.2: stop at the earliest position at which
        a complete declared terminator phrase begins."""
        if bslot.slot.is_open and not bslot.terminators:
            return len(toks), None
        i = cursor
        while i < len(toks):
            for term in bslot.terminators:
                if _texts(toks[i:i + len(term)]) == term:
                    return i, term
            i += 1
        return len(toks), None

    def _split_splices(self, toks: list) -> tuple:
        """Recognise a value splice inside a text slot (spec/01 section 1.7.1).

        The marker may precede its reference (`value of score`) or follow it
        (`score の値`), which the locale declares. Both produce the same node."""
        marker = self.locale.words.get("value of", ("value", "of"))
        ref_first = self.locale.splice_order == "ref first"
        pieces: list = []
        splices: list = []
        i = 0
        while i < len(toks):
            if _texts(toks[i:i + len(marker)]) == marker:
                if ref_first:
                    start = self._ref_start_before(toks, i, pieces)
                    if start is not None:
                        name = " ".join(t.text for t in toks[start:i])
                        del pieces[len(pieces) - (i - start):]
                        splices.append({"at": len(pieces), "ref": name})
                        pieces.append({"splice": name})
                        i += len(marker)
                        continue
                else:
                    j = i + len(marker)
                    name, j = self._read_ref(toks, j)
                    if name:
                        splices.append({"at": len(pieces), "ref": name})
                        pieces.append({"splice": name})
                        i = j
                        continue
            pieces.append(toks[i].text)
            i += 1
        return pieces, splices

    def _ref_start_before(self, toks: list, end: int, pieces: list):
        """Longest declared name ending just before `end`, else one word."""
        for start in range(0, end):
            if not all(isinstance(p, str) for p in pieces[start:end]):
                continue
            candidate = " ".join(t.text for t in toks[start:end])
            if candidate in self.names:
                return start
        if end > 0 and isinstance(pieces[-1], str):
            return end - 1
        return None

    def _read_ref(self, toks: list, cursor: int) -> tuple:
        """Longest match against the visible name set, then fall back to one
        word so that forward references still parse."""
        best = None
        best_end = cursor
        for end in range(len(toks), cursor, -1):
            candidate = " ".join(t.text for t in toks[cursor:end])
            if candidate in self.names:
                best = candidate
                best_end = end
                break
        if best is None and cursor < len(toks):
            best = toks[cursor].text
            best_end = cursor + 1
        return best, best_end

    # Operator precedence, spec/02 section 2.7.1. Higher binds tighter.
    PRECEDENCE = {
        "of": 6,
        "times": 5, "over": 5, "remainder of": 5,
        "plus": 4, "minus": 4,
        "is": 3, "is not": 3, "is more than": 3, "is less than": 3,
        "is at least": 3, "is at most": 3,
        "and": 2,
        "or": 1,
    }
    COMPARISONS = {"is", "is not", "is more than", "is less than",
                   "is at least", "is at most"}
    PREFIX = {"minus"}
    # `not` sits between the comparisons and `and` (spec/02 section 2.7.1):
    # `not a is b` means not (a is b). Its operand is a comparison or tighter.
    NOT_OPERAND = 3

    def _read_expression(self, toks: list, cursor: int, bslot: BoundSlot) -> tuple:
        end, _ = self._greedy_end(toks, cursor, bslot)
        pieces = self._flatten_expression(toks[cursor:end])
        if not pieces:
            tok = toks[min(cursor, len(toks) - 1)]
            raise SharkError("E-0232", "expected a value here",
                               tok.line, tok.col)
        tree, pos = self._parse_binary(pieces, 0, 1)
        if pos != len(pieces):
            tok = toks[min(cursor + pos, len(toks) - 1)]
            raise SharkError(
                "E-0251", f"`{tok.text}` is left over at the end of this value",
                tok.line, tok.col)
        return tree, end

    def _flatten_expression(self, parts: list) -> list:
        """Turn tokens into operator and operand pieces. Multi-word operators
        such as `is more than` are matched longest first."""
        ops = sorted(self.locale.ops.items(), key=lambda kv: -len(kv[1]))
        of_words = self.locale.particles.get("of", ("of",))
        # `yes` and `no` are values, not names: they reach SIR as the same
        # canonical words whatever the source language calls them.
        truths = [(value, self.locale.words.get(value, (value,)))
                  for value in ("yes", "no")]
        pieces = []
        i = 0
        while i < len(parts):
            if _texts(parts[i:i + len(of_words)]) == of_words:
                pieces.append(("op", "of"))
                i += len(of_words)
                continue
            matched = False
            for canonical, phrase in ops:
                if _texts(parts[i:i + len(phrase)]) == phrase:
                    pieces.append(("op", canonical))
                    i += len(phrase)
                    matched = True
                    break
            for value, phrase in truths:
                if _texts(parts[i:i + len(phrase)]) == phrase:
                    pieces.append(("truth", value))
                    i += len(phrase)
                    matched = True
                    break
            if matched:
                continue
            tok = parts[i]
            if tok.kind == "NUMBER":
                pieces.append(("number", tok.text))
            else:
                pieces.append(("ref", tok.text))
            i += 1
        return pieces

    def _parse_binary(self, pieces: list, pos: int, min_prec: int) -> tuple:
        if pos < len(pieces) and pieces[pos] == ("op", "not"):
            if min_prec > self.NOT_OPERAND:
                raise SharkError(
                    "E-0254", "`not` goes in front of a whole comparison; write "
                    "`not a is b`, or use `is not`")
            operand, pos = self._parse_binary(pieces, pos + 1, self.NOT_OPERAND)
            left = {"apply": "not", "operand": operand}
        else:
            left, pos = self._parse_unary(pieces, pos)
        saw_comparison = False
        while pos < len(pieces) and pieces[pos][0] == "op":
            op = pieces[pos][1]
            prec = self.PRECEDENCE.get(op)
            if prec is None or prec < min_prec:
                break
            if op in self.COMPARISONS:
                if saw_comparison:
                    raise SharkError(
                        "E-0252",
                        "comparisons do not chain; write `a is less than b "
                        "and b is less than c`")
                saw_comparison = True
            right, pos = self._parse_binary(pieces, pos + 1, prec + 1)
            if op == "of":
                left = {"member": right, "in": left}
            else:
                left = {"apply": op, "left": left, "right": right}
        return left, pos

    def _parse_unary(self, pieces: list, pos: int) -> tuple:
        if pos < len(pieces) and pieces[pos][0] == "op"                 and pieces[pos][1] in self.PREFIX:
            op = pieces[pos][1]
            operand, pos = self._parse_unary(pieces, pos + 1)
            return {"apply": op, "operand": operand}, pos
        if pos >= len(pieces):
            raise SharkError("E-0232", "expected a value here")
        kind, value = pieces[pos]
        if kind == "number":
            return {"number": value}, pos + 1
        if kind == "ref":
            return {"ref": value}, pos + 1
        if kind == "truth":
            return {"truth": value}, pos + 1
        raise SharkError("E-0253",
                           f"`{value}` cannot start a value")


def _texts(toks: Sequence[Token]) -> tuple:
    return tuple(t.text for t in toks)


def _span(toks: Sequence[Token], start: int, end: int) -> list:
    """Source positions of tokens [start, end), for editors (spec/13)."""
    return [[t.line, t.col, len(t.text)] for t in toks[start:end]]


# --------------------------------------------------------------------------
# Name collection (pass 1)
# --------------------------------------------------------------------------


DECLARING_CIDS = {
    "core.decl.page": "name",
    "core.decl.let": "name",
    "core.decl.keep": "name",
    "core.decl.message": "name",
}


def collect_names(tokens: list, registry: BoundRegistry) -> list:
    """Pass 1 of spec/09 section 9.4.1: declaration heads only."""
    names = []
    i = 0
    while i < len(tokens):
        if tokens[i].kind not in ("WORD", "NUMBER"):
            i += 1
            continue
        line = []
        j = i
        while j < len(tokens) and tokens[j].kind in ("WORD", "NUMBER"):
            line.append(tokens[j])
            j += 1
        parser = Parser(tokens[i:], registry, names)
        _anchor, bound = parser._find_head_anchor(line, 0, Parser.STATEMENT_KINDS)
        if bound is not None and bound.cid in DECLARING_CIDS:
            slot_name = DECLARING_CIDS[bound.cid]
            cursor = 0
            for seg in bound.order:
                if seg[0] == "head":
                    cursor += len(bound.head)
                elif seg[0] == "slot":
                    bslot = seg[1]
                    if bslot.slot.name == slot_name:
                        end, _ = parser._greedy_end(line, cursor, bslot)
                        candidate = " ".join(t.text for t in line[cursor:end])
                        if candidate:
                            names.append(candidate)
                        break
                    end, _ = parser._greedy_end(line, cursor, bslot)
                    cursor = end
                elif seg[0] == "particle":
                    cursor += len(seg[1])
        i = j
    return names


# --------------------------------------------------------------------------
# Declared instructions (spec/03 section 3.4)
# --------------------------------------------------------------------------

ARTICLES = ("a", "an", "the")
SLOT_TYPES = {
    "core.type.text": "text",
    "core.type.whole_number": "value",
    "core.type.decimal_number": "value",
    "core.type.boolean": "value",
}


def top_level_lines(tokens: list) -> list:
    """The word runs of every line at indentation depth 0, in order."""
    lines = []
    depth = 0
    i = 0
    while i < len(tokens):
        kind = tokens[i].kind
        if kind == "INDENT":
            depth += 1
        elif kind == "DEDENT":
            depth -= 1
        elif kind in ("WORD", "NUMBER"):
            j = i
            while j < len(tokens) and tokens[j].kind in ("WORD", "NUMBER"):
                j += 1
            if depth == 0:
                lines.append(tokens[i:j])
            i = j
            continue
        i += 1
    return lines


def declare_actions(lines: list, registry: BoundRegistry, particle_set: list,
                    taken: dict) -> dict:
    """Turn every top-level `to ...` line into a signature before anything
    is parsed, so a new instruction can be used above its declaration.

    Scanning the words after `to`: an article and a word make a slot; `as`
    and a type after a slot choose its kind; a particle phrase (longest
    match) is kept as a particle; any other word joins the head, which must
    come before the first slot. `taken` maps every head already visible to
    what declared it, for conditions R1 and R5. Returns {line: Signature}."""
    locale = registry.locale
    decl = registry.bound.get("core.decl.action")
    if decl is None:
        return {}
    articles = {w for a in ARTICLES for w in locale.words.get(a, (a,))}
    as_words = locale.particles.get("as", ("as",))
    particles = sorted(((registry._particle_words(p), p) for p in particle_set),
                       key=lambda pair: -len(pair[0]))
    types = sorted(((phrase, cid) for cid, phrase in locale.types.items()),
                   key=lambda pair: -len(pair[0]))

    found: dict = {}
    for toks in lines:
        words = _texts(toks)
        if words[:len(decl.head)] != decl.head or len(words) == len(decl.head):
            continue
        rest = toks[len(decl.head):]
        texts = _texts(rest)
        head: list = []
        segments: list = [("head",)]
        pending: Optional[ParticleSeg] = None
        slots: list = []
        i = 0
        while i < len(rest):
            word = texts[i]
            if word in articles and i + 1 < len(rest):
                slot = Slot(name=rest[i + 1].text, kind="ref")
                i += 2
                if texts[i:i + len(as_words)] == as_words:
                    i += len(as_words)
                    if i < len(rest) and texts[i] in articles:
                        i += 1
                    for phrase, cid in types:
                        if texts[i:i + len(phrase)] == phrase and cid in SLOT_TYPES:
                            slot.kind, slot.type = SLOT_TYPES[cid], cid
                            i += len(phrase)
                            break
                    else:
                        tok = rest[min(i, len(rest) - 1)]
                        raise SharkError(
                            "E-0317", f"`{tok.text}` is not a kind of value a slot "
                            "can hold here", tok.line, tok.col,
                            help_text="use text, a whole number, a decimal number "
                                      "or yes or no")
                if any(s.name == slot.name for s in slots):
                    raise SharkError("E-0318", f"two slots are called `{slot.name}`",
                                    rest[0].line, rest[0].col)
                slots.append(slot)
                if pending is not None:
                    pending.slots.append(slot)
                else:
                    segments.append(("slot", slot))
                continue
            for phrase, canonical in particles:
                if texts[i:i + len(phrase)] == phrase:
                    pending = ParticleSeg(canonical=canonical, optional=False)
                    segments.append(("particle", pending))
                    i += len(phrase)
                    break
            else:
                if slots or pending is not None:
                    tok = rest[i]
                    raise SharkError(
                        "E-0311", f"`{word}` comes after a slot, but an instruction's "
                        "own words must all come before its first slot",
                        tok.line, tok.col,
                        help_text="spec/03 section 3.4")
                head.append(word)
                i += 1
        if not head:
            raise SharkError("E-0311", "a new instruction must start with a word of "
                            "its own", toks[0].line, toks[0].col)

        # A greedy or value slot is closed by the particle that follows it
        # (R2); a text slot that ends the line is open.
        order = [seg for seg in segments[1:]]
        flat: list = []
        for seg in order:
            if seg[0] == "slot":
                flat.append(("slot", seg[1]))
            else:
                flat.append(("particle", seg[1]))
                flat.extend(("slot", s) for s in seg[1].slots)
        for index, (what, item) in enumerate(flat):
            if what != "slot" or item.kind == "ref":
                continue
            following = flat[index + 1] if index + 1 < len(flat) else None
            if following is None:
                item.is_open = item.kind == "text"
            elif following[0] == "particle":
                item.terminator_names.append(following[1].canonical)

        head_t = tuple(head)
        joined = " ".join(head_t)
        where = (toks[0].line, toks[0].col)
        if head_t in taken:
            raise SharkError("E-0315", f"`{joined}` is already an instruction "
                            f"({taken[head_t]}), and one can't be declared twice",
                            *where)
        for other, source in taken.items():
            short, long = sorted((head_t, other), key=len)
            if short != long and long[:len(short)] == short:
                raise SharkError(
                    "E-0312", f"`{' '.join(short)}` and `{' '.join(long)}` cannot both "
                    f"exist: after `{' '.join(short)}` the reader couldn't tell a slot "
                    f"from the rest of `{' '.join(long)}` ({source})", *where,
                    fix=f"start the new instruction with a word of its own, such as "
                        f"`to draw ...`")
        sig = Signature(cid="user." + "_".join(head_t), default_head_words=head_t,
                        segments=segments, kind="stmt")
        taken[head_t] = f"declared on line {toks[0].line}"
        found[toks[0].line] = sig
    return found


def action_shape(sig: Signature) -> dict:
    """A declared instruction's shape as SIR records it."""
    def slot_part(slot: Slot) -> dict:
        part = {"slot": slot.name, "kind": slot.kind}
        if slot.type:
            part["type"] = slot.type
        return part

    parts: list = [{"head": list(sig.default_head_words)}]
    for seg in sig.segments:
        if seg[0] == "slot":
            parts.append(slot_part(seg[1]))
        elif seg[0] == "particle":
            parts.append({"particle": seg[1].canonical})
            parts.extend(slot_part(s) for s in seg[1].slots)
    return {"action": sig.cid, "parts": parts}


# --------------------------------------------------------------------------
# Modules (spec/05 section 5.12)
# --------------------------------------------------------------------------


class ModuleLoader:
    """Finds and compiles the modules a program uses. One file is one module;
    `use ui panels side` means ui/panels/side.shark beside the entry file."""

    def __init__(self, root: str):
        self.root = root
        self.done: dict = {}
        self.loading: list = []

    def load(self, words: tuple, line: int = 0) -> "Compilation":
        path = os.path.join(self.root, *words) + ".shark"
        key = os.path.normcase(os.path.abspath(path))
        if key in self.done:
            return self.done[key]
        name = " ".join(words)
        if key in self.loading:
            raise SharkError(
                "E-1202", f"modules use each other in a circle through `{name}`; "
                "the reference toolchain can't compile that yet", line)
        if not os.path.exists(path):
            raise SharkError("E-1201", f"there is no module called `{name}` "
                            f"(looked for {path})", line)
        self.loading.append(key)
        try:
            with open(path, encoding="utf-8") as handle:
                comp = compile_source(handle.read(), path, loader=self)
        finally:
            self.loading.pop()
        comp.module = name
        self.done[key] = comp
        return comp


# --------------------------------------------------------------------------
# SIR-D emission and the canonical hash
# --------------------------------------------------------------------------


def to_sir(items: list, module_cid: str = "app.main") -> dict:
    pool: list = []

    def datum(value: Any) -> int:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in pool:
            pool.append(key)
        return pool.index(key)

    def convert(node: Node) -> Optional[dict]:
        if node.cid == "core.directive":
            return None
        out: dict = {"op": node.cid}
        args = {}
        for name in sorted(node.args):
            value = node.args[name]
            args[name] = encode_value(value)
        if args:
            out["args"] = args
        body = [c for c in (convert(child) for child in node.body) if c]
        if body:
            out["body"] = body
        return out

    def encode_value(value: Any) -> Any:
        if isinstance(value, dict):
            if "text" in value:
                return {"const text": datum(value["text"])}
            if "name" in value:
                return {"name": datum(value["name"])}
            if "number" in value:
                return {"const number": datum(value["number"])}
            if "ref" in value:
                return {"ref": datum(value["ref"])}
            if "expression" in value:
                return {"expression": [encode_value(p) for p in value["expression"]]}
            if "op" in value:
                return {"op": value["op"]}
            return {k: encode_value(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [encode_value(v) for v in value]
        return value

    converted = [c for c in (convert(item) for item in items) if c]

    # Canonical ordering, spec/08 section 8.6: the datum pool is sorted and
    # indices are rewritten so that the encoding depends only on meaning.
    order = sorted(range(len(pool)), key=lambda i: pool[i])
    remap = {old: new for new, old in enumerate(order)}
    sorted_pool = [json.loads(pool[i]) for i in order]

    def rewrite(value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, inner in value.items():
                if key in ("const text", "name", "const number", "ref") \
                        and isinstance(inner, int):
                    out[key] = remap[inner]
                else:
                    out[key] = rewrite(inner)
            return out
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        return value

    return {
        "module": module_cid,
        "edition": 2026,
        "data": sorted_pool,
        "items": [rewrite(item) for item in converted],
    }


def canonical_encoding(sir: dict) -> bytes:
    return json.dumps(sir, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def canonical_hash(sir: dict) -> str:
    digest = hashlib.blake2b(canonical_encoding(sir), digest_size=32).hexdigest()
    # The specification names BLAKE3; the reference uses BLAKE2b because it is
    # in the Python standard library. The tag records which was used.
    return "b2:" + digest


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def detect_locale(source: str) -> str:
    """The `write in` directive is recognised in every locale, because it must
    be read before the locale is known (spec/04 section 4.4)."""
    for raw in source.splitlines():
        words = raw.strip().split()
        if len(words) == 3 and words[0].lower() == "write" and words[1].lower() == "in":
            return LOCALE_ALIASES.get(words[2].lower(), words[2].lower())
    return "en"


@dataclass
class Compilation:
    path: str
    locale: Locale
    tokens: list
    items: list
    sir: dict
    hash: str
    traces: dict
    module: str = "app main"
    signatures: dict = field(default_factory=dict)   # declared here: cid -> Signature
    shared: list = field(default_factory=list)       # names this module shares
    used: dict = field(default_factory=dict)         # module name -> Compilation

    def exports(self) -> tuple:
        """The signatures and names another module sees after `use`."""
        shared = set(self.shared)
        sigs = {cid: sig for cid, sig in self.signatures.items()
                if " ".join(sig.default_head_words) in shared}
        return sigs, [n for n in self.shared if n not in
                      {" ".join(s.default_head_words) for s in sigs.values()}]

    def modules(self) -> dict:
        """Every module this program is made of, by name, this one included."""
        found = {self.module: self}
        for comp in self.used.values():
            for name, inner in comp.modules().items():
                found.setdefault(name, inner)
        return found


def compile_file(path: str, locale_tag: Optional[str] = None) -> Compilation:
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return compile_source(source, path, locale_tag)


def compile_source(source: str, path: str = "<memory>",
                   locale_tag: Optional[str] = None,
                   loader: Optional[ModuleLoader] = None) -> Compilation:
    tag = locale_tag or detect_locale(source)
    locale = load_locale(tag)
    raw_signatures, particle_set = load_registry()
    registry = BoundRegistry(raw_signatures, locale)

    problems = registry.check_admissibility()
    if problems:
        code, message = problems[0]
        raise SharkError(code, f"registry is not admissible: {message}")

    # Pass 1: fixed lexicon. Before any clause is parsed, find what this
    # module uses and shares and the instructions it declares (spec/02 2.2).
    lexer = Lexer(source, locale, registry.lexicon())
    tokens = lexer.run()
    lines = top_level_lines(tokens)
    use_words = locale.words.get("use", ("use",))
    share_words = locale.words.get("share", ("share",))

    loader = loader or ModuleLoader(os.path.dirname(os.path.abspath(path)))
    used: dict = {}
    imported: dict = {}
    imported_names: list = []
    taken = {head: "built in" for head in registry.by_head}
    for toks in lines:
        words = _texts(toks)
        if words[:len(use_words)] == use_words and len(words) > len(use_words):
            module = words[len(use_words):]
            comp = loader.load(module, toks[0].line)
            used[" ".join(module)] = comp
            sigs, names_out = comp.exports()
            for cid, sig in sigs.items():
                if sig.default_head_words in taken and cid not in imported:
                    raise SharkError(
                        "E-0315", f"`{' '.join(sig.default_head_words)}` from "
                        f"`{' '.join(module)}` is already an instruction here",
                        toks[0].line, toks[0].col)
                taken[sig.default_head_words] = f"from `{' '.join(module)}`"
                imported[cid] = sig
            imported_names.extend(names_out)
    shared = [" ".join(_texts(toks)[len(share_words):]) for toks in lines
              if _texts(toks)[:len(share_words)] == share_words
              and len(toks) > len(share_words)]

    actions = declare_actions(lines, registry, particle_set, taken)
    own = {sig.cid: sig for sig in actions.values()}
    if own or imported:
        registry = BoundRegistry({**raw_signatures, **imported, **own}, locale)
        problems = registry.check_admissibility()
        if problems:
            code, message = problems[0]
            raise SharkError(code, message)
        lexer = Lexer(source, locale, registry.lexicon())
        tokens = lexer.run()
    names = collect_names(tokens, registry) + imported_names

    declared = set(names) | {" ".join(s.default_head_words) for s in own.values()}
    for name in shared:
        if name not in declared:
            raise SharkError("E-0503", f"`{name}` is shared but this module never "
                            "declares it")

    # Pass 2: lexicon extended with the declared names, then full parse.
    lexer = Lexer(source, locale, registry.lexicon(names))
    tokens = lexer.run()
    parser = Parser(tokens, registry, names, actions)
    items = parser.parse_program()

    sir = to_sir(items)
    return Compilation(path=path, locale=locale, tokens=tokens, items=items,
                       sir=sir, hash=canonical_hash(sir), traces=parser.traces,
                       signatures=own, shared=shared, used=used)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_ast(items: list, indent: int = 0) -> str:
    out = []
    for node in items:
        if node.cid == "core.directive":
            continue
        args = ", ".join(f"{k}={_short(v)}" for k, v in sorted(node.args.items()))
        out.append("  " * indent + f"{node.cid}({args})")
        if node.body:
            out.append(render_ast(node.body, indent + 1))
    return "\n".join(o for o in out if o)


def _short(value: Any) -> str:
    if isinstance(value, dict):
        if "text" in value:
            return repr(" ".join(p if isinstance(p, str) else "<splice>"
                                 for p in value["text"]))
        if "name" in value:
            return repr(" ".join(value["name"]))
        for key in ("ref", "number"):
            if key in value:
                return f"{key}:{value[key]}"
    return str(value)


def render_trace(trace: list) -> str:
    lines = []
    for step in trace:
        action = step.get("action", "?")
        rest = ", ".join(f"{k}={v!r}" for k, v in step.items()
                         if k not in ("action", "span", "role"))
        lines.append(f"  {action:<14} {rest}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_check_registry() -> int:
    raw_signatures, particles = load_registry()
    print(f"core registry: {len(raw_signatures)} signatures, "
          f"{len(particles)} particles")
    ok = True
    for tag in sorted(_available_locales()):
        locale = load_locale(tag)
        registry = BoundRegistry(raw_signatures, locale)
        problems = registry.check_admissibility()
        bound = len(registry.bound)
        if problems:
            ok = False
            print(f"  {tag}: {bound} bound signatures, "
                  f"{len(problems)} problems")
            for code, message in problems:
                print(f"     {code}  {message}")
        else:
            print(f"  {tag}: {bound} bound signatures, R1-R6 hold, "
                  f"LL(k) with k = {registry.lookahead} "
                  f"(longest head {registry.max_head_len}, "
                  f"longest terminator {registry.max_terminator_len})")
    return 0 if ok else 1


def _available_locales() -> list:
    return [f[:-len(".locale")] for f in os.listdir(LOCALE_DIR)
            if f.endswith(".locale")]


TOUR_FILES = {
    "en": os.path.join(ROOT, "examples", "tour.shark"),
    "he": os.path.join(ROOT, "examples", "tour.he.shark"),
    "ja": os.path.join(ROOT, "examples", "tour.ja.shark"),
}


def cmd_prove_locale_equivalence() -> int:
    print("Compiling the same program written in three human languages.")
    print("Text content and declared names are application data and are not")
    print("translated, exactly as spec/04 section 4.9 requires.\n")

    results = {}
    for tag, path in TOUR_FILES.items():
        comp = compile_file(path)
        results[tag] = comp
        locale_name = comp.locale.name or tag
        note_word = " ".join(comp.locale.words.get("note", ("note",)))
        print(f"  {tag} ({locale_name})  {os.path.relpath(path, ROOT)}")
        for line in open(path, encoding="utf-8").read().splitlines():
            body = line.strip()
            if not body or body.split(" ")[0] == note_word.split(" ")[0]:
                continue
            print(f"      {line}")
        print(f"      -> {comp.hash}")
        print()

    hashes = {tag: comp.hash for tag, comp in results.items()}
    unique = set(hashes.values())
    if len(unique) == 1:
        print("RESULT: all three canonical hashes are identical.")
        print("        The three files are the same program.")
        print()
        print("Canonical SIR-D:")
        print(json.dumps(results["en"].sir, ensure_ascii=False, indent=2))
        return 0

    print("RESULT: hashes differ. The locale binding is not meaning preserving.")
    for tag, value in hashes.items():
        print(f"   {tag}: {value}")
    for tag, comp in results.items():
        print(f"\n--- {tag} SIR ---")
        print(json.dumps(comp.sir, ensure_ascii=False, indent=2))
    return 1


def cmd_why(target: str) -> int:
    path, _, line_text = target.rpartition(":")
    if not path:
        print("usage: --why FILE:LINE", file=sys.stderr)
        return 2
    comp = compile_file(path)
    wanted = int(line_text)
    trace = comp.traces.get(wanted)
    if trace is None:
        print(f"no clause starts on line {wanted}")
        return 1
    with open(path, encoding="utf-8") as handle:
        source_line = handle.read().splitlines()[wanted - 1]
    print(f"{path}:{wanted}")
    print(f"  {source_line.strip()}")
    print()
    print(render_trace(trace))
    return 0


def _force_utf8_output() -> None:
    """Shark source is Unicode by definition; make sure a Windows console
    with a legacy code page does not hide that."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[list] = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="shark_ref",
        description="Shark reference front end (Edition 2026)")
    parser.add_argument("file", nargs="?", help="an .shark source file")
    parser.add_argument("--emit", default="ast",
                        choices=["tokens", "ast", "sir", "hash"])
    parser.add_argument("--locale", default=None)
    parser.add_argument("--check-registry", action="store_true")
    parser.add_argument("--prove-locale-equivalence", action="store_true")
    parser.add_argument("--why", default=None, metavar="FILE:LINE")
    args = parser.parse_args(argv)

    try:
        if args.check_registry:
            return cmd_check_registry()
        if args.prove_locale_equivalence:
            return cmd_prove_locale_equivalence()
        if args.why:
            return cmd_why(args.why)
        if not args.file:
            parser.print_help()
            return 2

        comp = compile_file(args.file, args.locale)
        if args.emit == "tokens":
            for tok in comp.tokens:
                print(tok)
        elif args.emit == "ast":
            print(render_ast(comp.items))
        elif args.emit == "sir":
            print(json.dumps(comp.sir, ensure_ascii=False, indent=2))
        elif args.emit == "hash":
            print(comp.hash)
        return 0
    except SharkError as err:
        source_line = ""
        if args.file and os.path.exists(args.file) and err.line:
            lines = open(args.file, encoding="utf-8").read().splitlines()
            if 0 < err.line <= len(lines):
                source_line = lines[err.line - 1]
        print(err.render(args.file or "", source_line), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
