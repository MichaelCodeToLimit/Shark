#!/usr/bin/env python3
"""
Generate editor/shark.tmLanguage.json from the Clause Signature Registry.

    python editor/build_grammar.py

A TextMate grammar is a regular-expression colouriser. It cannot know where a
text slot ends, because that depends on the signature being matched — which is
the whole point of Shark's design. So this grammar is a *fallback*: it colours
notes, signature heads, particles, word operators and numbers, and treats the
tail of an open-slot statement as text. Accurate colouring comes from the
language server's semantic tokens (editor/shark_lsp.py), which run the real
parser.

The keyword lists are the union of every shipped locale, so a Hebrew or
Japanese file is coloured by the same grammar as an English one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "reference"))

import shark_ref as shark  # noqa: E402

WORDY = re.compile(r"^[\w' -]+$", re.UNICODE)


def bounded(phrase: str) -> str:
    """Word boundaries where the script has them; bare match where it does not."""
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    if WORDY.match(phrase) and phrase.isascii():
        return r"\b" + escaped + r"\b"
    if WORDY.match(phrase):
        return r"(?<![^\W\d_])" + escaped + r"(?![^\W\d_])"
    return escaped


def alternation(phrases) -> str:
    unique = sorted({p for p in phrases if p}, key=lambda p: (-len(p), p))
    return "|".join(bounded(p) for p in unique)


def collect() -> dict:
    signatures, _particles = shark.load_registry()
    locales = sorted(f[:-len(".locale")] for f in os.listdir(shark.LOCALE_DIR)
                     if f.endswith(".locale"))

    notes, open_heads, heads, particles, operators, types, literals = (
        set(), set(), set(), set(), set(), set(), set())

    for tag in locales:
        locale = shark.load_locale(tag)
        registry = shark.BoundRegistry(signatures, locale)
        notes.add(" ".join(locale.words.get("note", ("note",))))
        for phrase in locale.particles.values():
            particles.add(" ".join(phrase))
        for phrase in locale.ops.values():
            operators.add(" ".join(phrase))
        for phrase in locale.types.values():
            types.add(" ".join(phrase))
        for key in ("yes", "no", "nothing", "empty"):
            if key in locale.words:
                literals.add(" ".join(locale.words[key]))
        for key in ("let", "keep", "set", "when", "if", "otherwise", "while",
                    "repeat", "for each", "to", "give", "stop", "skip", "try",
                    "use", "share", "during", "group", "text", "value of"):
            if key in locale.words:
                operators.add(" ".join(locale.words[key])) if key in (
                    "value of",) else heads.add(" ".join(locale.words[key]))
        for bound in registry.bound.values():
            phrase = " ".join(bound.head)
            last_line_slot = None
            for seg in bound.order:
                if seg[0] == "slot" and seg[1].slot.kind != "block":
                    last_line_slot = seg[1]
            is_open = (last_line_slot is not None
                       and last_line_slot.slot.kind == "text"
                       and last_line_slot.slot.is_open
                       and bound.order[-1][0] != "head")
            (open_heads if is_open else heads).add(phrase)

    open_heads -= heads
    return {
        "locales": locales,
        "notes": sorted(notes),
        "open_heads": sorted(open_heads),
        "heads": sorted(heads),
        "particles": sorted(particles),
        "operators": sorted(operators),
        "types": sorted(types),
        "literals": sorted(literals),
    }


def build(parts: dict) -> dict:
    patterns = [
        {
            "name": "comment.line.note.shark",
            "match": r"^\s*(?:" + alternation(parts["notes"]) + r")(?:\s.*)?$",
        },
        {
            "name": "meta.directive.shark",
            "match": r"^\s*(write\s+in|use|share|this\s+is\s+edition)\s+(.*)$",
            "captures": {
                "1": {"name": "keyword.control.directive.shark"},
                "2": {"name": "entity.name.namespace.shark"},
            },
        },
    ]

    if parts["open_heads"]:
        patterns.append({
            "name": "meta.statement.open.shark",
            "match": r"(?i)^(\s*)(" + alternation(parts["open_heads"]) + r")(\s+)(.*)$",
            "captures": {
                "2": {"name": "keyword.control.shark"},
                "4": {"name": "string.unquoted.shark"},
            },
        })

    patterns += [
        {
            "name": "keyword.control.shark",
            "match": r"(?i)(?:" + alternation(parts["heads"]) + r")",
        },
        {
            "name": "keyword.operator.particle.shark",
            "match": r"(?i)(?:" + alternation(parts["particles"]) + r")",
        },
        {
            "name": "keyword.operator.word.shark",
            "match": r"(?i)(?:" + alternation(parts["operators"]) + r")",
        },
        {
            "name": "support.type.shark",
            "match": r"(?i)(?:" + alternation(parts["types"]) + r")",
        },
        {
            "name": "constant.language.shark",
            "match": r"(?i)(?:" + alternation(parts["literals"]) + r")",
        },
        {
            "name": "constant.numeric.shark",
            "match": r"\b\d+(?:[.,]\d+)*\b",
        },
    ]

    return {
        "$schema": "https://raw.githubusercontent.com/martinring/tmlanguage/"
                   "master/tmlanguage.json",
        "name": "Shark",
        "scopeName": "source.shark",
        "fileTypes": ["shark"],
        "information_for_contributors": [
            "Generated by editor/build_grammar.py from registry/. Do not edit.",
            "Keywords are the union of locales: " + ", ".join(parts["locales"]),
            "Accurate colouring comes from the language server, not from this file.",
        ],
        "patterns": patterns,
    }


def main() -> int:
    parts = collect()
    grammar = build(parts)
    out = os.path.join(HERE, "shark.tmLanguage.json")
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(grammar, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    # Sanity: every pattern must compile as a regular expression.
    for pattern in grammar["patterns"]:
        re.compile(pattern["match"])

    # The VS Code extension needs its own copy; keep both in step here
    # rather than asking anyone to remember to copy it.
    copy = os.path.join(HERE, "vscode", "shark.tmLanguage.json")
    os.makedirs(os.path.dirname(copy), exist_ok=True)
    shutil.copyfile(out, copy)
    print("wrote", os.path.relpath(out, ROOT))
    print("wrote", os.path.relpath(copy, ROOT))
    print("  locales   ", ", ".join(parts["locales"]))
    print("  open heads", len(parts["open_heads"]))
    print("  heads     ", len(parts["heads"]))
    print("  particles ", len(parts["particles"]))
    print("  operators ", len(parts["operators"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
