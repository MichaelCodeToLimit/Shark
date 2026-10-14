#!/usr/bin/env python3
"""
Shark language server.

Speaks the Language Server Protocol over stdin/stdout, which means one
implementation serves VS Code, Neovim, Helix, Zed, Emacs, Sublime Text,
Kate, JetBrains and anything else that speaks LSP. See editor/README.md for
per-editor setup, and spec/13-lsp.md for the protocol surface this implements.

No dependencies. Python 3.8 or newer.

    python editor/shark_lsp.py            # serve on stdio
    python editor/shark_lsp.py --selftest # run without an editor

Implemented: diagnostics, completion, hover, semantic tokens, document
symbols, go to definition, and the shark/parseTrace extension.
Not implemented: formatting, rename, references, inlay hints. Those need
pieces of the compiler that do not exist yet (spec/16-roadmap.md).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Optional
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "reference"))

import shark_ref as shark  # noqa: E402

SERVER_NAME = "shark-lsp"
SERVER_VERSION = "0.1.0"

# Standard LSP token types, so a client colours Shark correctly with no
# extra theme configuration.
TOKEN_TYPES = [
    "keyword",    # signature heads
    "operator",   # particle phrases and word operators
    "string",     # the contents of a text slot
    "variable",   # names and references
    "number",
    "comment",    # note lines
    "property",   # slot labels in the hanging form
]
TOKEN_MODIFIERS = ["declaration", "readonly"]

ROLE_TO_TOKEN = {
    "head": "keyword",
    "particle": "operator",
    "ref": "variable",
    "slot:text": "string",
    "slot:name": "variable",
    "slot:ref": "variable",
    "slot:number": "number",
    "slot:value": "variable",
    "slot:event": "variable",
}


def log(*parts: Any) -> None:
    print(SERVER_NAME + ":", *parts, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Position conversion
# --------------------------------------------------------------------------
#
# Shark counts lines and columns from 1, in code points (spec/01 section 1.8:
# logical order, never visual). LSP counts from 0, in UTF-16 code units.


def to_utf16(line_text: str, code_point_col: int) -> int:
    prefix = line_text[:max(code_point_col, 0)]
    return len(prefix.encode("utf-16-le")) // 2


def from_utf16(line_text: str, utf16_col: int) -> int:
    raw = line_text.encode("utf-16-le")
    return len(raw[:utf16_col * 2].decode("utf-16-le", errors="ignore"))


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


class Analysis:
    """One compilation of one document, indexed for editor queries."""

    def __init__(self, text: str, uri: str):
        self.text = text
        self.uri = uri
        self.lines = text.splitlines()
        self.error: Optional[shark.SharkError] = None
        self.comp: Optional[shark.Compilation] = None
        self.locale_tag = shark.detect_locale(text)
        self.registry = build_registry(self.locale_tag)
        self.locale = self.registry.locale
        self.node_by_line: dict = {}
        self.declarations: dict = {}
        self.token_at: dict = {}
        self.names: list = []

        try:
            self.comp = shark.compile_source(text, uri_to_path(uri) or "<untitled>")
        except shark.SharkError as err:
            self.error = err
        except Exception:                       # never take the server down
            log("internal error while analysing", uri)
            log(traceback.format_exc())

        if self.comp is not None:
            for tok in self.comp.tokens:
                self.token_at[(tok.line, tok.col)] = tok
            self._index(self.comp.items)
            self.names = sorted(
                {n for n in self.declarations},
                key=lambda n: (-len(n), n))

    def _index(self, items: list) -> None:
        for node in items:
            if node.cid == "core.directive":
                continue
            self.node_by_line.setdefault(node.line, node)
            slot = shark.DECLARING_CIDS.get(node.cid)
            if slot and slot in node.args:
                value = node.args[slot]
                pieces = value.get("name") or value.get("text") or []
                name = " ".join(p for p in pieces if isinstance(p, str))
                span = self._slot_span(node, "slot:name")
                if name and span:
                    self.declarations[name] = (node.cid, span)
            self._index(node.body)

    def _slot_span(self, node, role: str) -> Optional[list]:
        for step in node.trace:
            if step.get("role") == role and step.get("span"):
                return step["span"]
        return None

    def line_text(self, line_1based: int) -> str:
        idx = line_1based - 1
        return self.lines[idx] if 0 <= idx < len(self.lines) else ""

    # -- queries ---------------------------------------------------------

    def note_word(self) -> str:
        return " ".join(self.locale.words.get("note", ("note",)))

    def is_note_line(self, text: str) -> bool:
        word = self.note_word()
        body = text.strip()
        return body == word or body.startswith(word + " ")

    def step_at(self, line: int, col: int):
        """The parse-trace step covering a 1-based line and column."""
        node = self.node_by_line.get(line)
        if node is None:
            return None, None
        for step in flatten_trace(node.trace):
            for tok_line, tok_col, length in step.get("span") or []:
                if tok_line == line and tok_col <= col < tok_col + length:
                    return node, step
        return node, None


def flatten_trace(trace: list) -> list:
    out = []
    for step in trace or []:
        out.append(step)
        if isinstance(step.get("steps"), list):
            out.extend(flatten_trace(step["steps"]))
    return out


_REGISTRY_CACHE: dict = {}


def build_registry(locale_tag: str):
    if locale_tag not in _REGISTRY_CACHE:
        signatures, _particles = shark.load_registry()
        locale = shark.load_locale(locale_tag)
        _REGISTRY_CACHE[locale_tag] = shark.BoundRegistry(signatures, locale)
    return _REGISTRY_CACHE[locale_tag]


# --------------------------------------------------------------------------
# Feature: diagnostics
# --------------------------------------------------------------------------


def diagnostics_for(analysis: Analysis) -> list:
    err = analysis.error
    if err is None:
        return []
    line = max(err.line - 1, 0)
    text = analysis.line_text(err.line)
    start = to_utf16(text, max(err.col - 1, 0))
    end = to_utf16(text, len(text)) if text else start + 1
    message = err.message
    if err.help_text:
        message += "\n\nhelp: " + err.help_text
    if err.fix:
        message += "\nfix:  " + err.fix
    return [{
        "range": {"start": {"line": line, "character": start},
                  "end": {"line": line, "character": max(end, start + 1)}},
        "severity": 1,
        "code": err.code,
        "source": "shark",
        "message": message,
        "codeDescription": {
            "href": "https://shark-lang.org/e/" + err.code},
    }]


# --------------------------------------------------------------------------
# Feature: semantic tokens
# --------------------------------------------------------------------------


def semantic_tokens(analysis: Analysis) -> list:
    """Colour by clause part, which is what makes quote-free text readable."""
    marks: list = []          # (line, col, length, type, modifiers)

    for index, text in enumerate(analysis.lines):
        if analysis.is_note_line(text):
            stripped = len(text) - len(text.lstrip())
            marks.append((index + 1, stripped + 1,
                          len(text.strip()), "comment", 0))

    if analysis.comp is not None:
        splice = analysis.locale.words.get("value of", ("value", "of"))
        operators = {" ".join(p): True for p in analysis.locale.ops.values()}

        for node in walk(analysis.comp.items):
            declaring = shark.DECLARING_CIDS.get(node.cid)
            for step in flatten_trace(node.trace):
                role = step.get("role")
                span = step.get("span") or []
                if not role or not span:
                    continue
                kind = ROLE_TO_TOKEN.get(role)
                if kind is None:
                    continue
                if role == "slot:value":
                    marks.extend(value_marks(analysis, span, operators))
                    continue
                modifiers = 0
                if declaring and role == "slot:name":
                    modifiers = 1                    # declaration
                if role == "slot:text":
                    marks.extend(text_marks(analysis, span, splice))
                    continue
                for line, col, length in span:
                    marks.append((line, col, length, kind, modifiers))

    return encode_tokens(analysis, marks)


def walk(items: list):
    for node in items:
        if node.cid != "core.directive":
            yield node
        yield from walk(node.body)


def text_marks(analysis: Analysis, span: list, splice: tuple) -> list:
    """Inside a text slot everything is string, except a value splice."""
    out = []
    index = 0
    while index < len(span):
        window = []
        for offset in range(len(splice)):
            if index + offset < len(span):
                line, col, length = span[index + offset]
                tok = analysis.token_at.get((line, col))
                window.append(tok.text if tok else None)
        if tuple(window) == tuple(splice):
            for offset in range(len(splice)):
                line, col, length = span[index + offset]
                out.append((line, col, length, "keyword", 0))
            index += len(splice)
            if index < len(span):
                line, col, length = span[index]
                out.append((line, col, length, "variable", 0))
                index += 1
            continue
        line, col, length = span[index]
        out.append((line, col, length, "string", 0))
        index += 1
    return out


def value_marks(analysis: Analysis, span: list, operators: dict) -> list:
    out = []
    for line, col, length in span:
        tok = analysis.token_at.get((line, col))
        if tok is None:
            continue
        if tok.kind == "NUMBER":
            out.append((line, col, length, "number", 0))
        elif tok.text in operators:
            out.append((line, col, length, "operator", 0))
        else:
            out.append((line, col, length, "variable", 0))
    return out


def encode_tokens(analysis: Analysis, marks: list) -> list:
    marks = sorted(set(marks), key=lambda m: (m[0], m[1]))
    data: list = []
    prev_line = 0
    prev_char = 0
    for line, col, length, kind, modifiers in marks:
        if kind not in TOKEN_TYPES:
            continue
        text = analysis.line_text(line)
        char = to_utf16(text, col - 1)
        width = to_utf16(text, col - 1 + length) - char
        if width <= 0:
            continue
        delta_line = (line - 1) - prev_line
        delta_char = char - prev_char if delta_line == 0 else char
        data.extend([delta_line, delta_char, width,
                     TOKEN_TYPES.index(kind), modifiers])
        prev_line = line - 1
        prev_char = char
    return data


# --------------------------------------------------------------------------
# Feature: completion
# --------------------------------------------------------------------------


def completions(analysis: Analysis, line: int, character: int) -> list:
    text = analysis.line_text(line)
    cut = from_utf16(text, character)
    prefix = text[:cut]
    if analysis.is_note_line(prefix):
        return []

    words = prefix.split()
    partial = "" if prefix.endswith(" ") or not words else words.pop()
    folded = tuple(w.casefold() for w in words)
    partial_folded = partial.casefold()
    registry = analysis.registry
    items: list = []
    seen: set = set()

    # 1. Signature heads that continue what has been typed.
    for head, bounds in registry.by_head.items():
        if len(head) <= len(folded):
            continue
        if tuple(h.casefold() for h in head[:len(folded)]) != folded:
            continue
        if partial_folded and not head[len(folded)].casefold().startswith(partial_folded):
            continue
        bound = bounds[0]
        if bound.sig.kind == "event" and "when" not in folded:
            continue
        label = " ".join(head)
        if label in seen:
            continue
        seen.add(label)
        items.append(head_item(bound, head, folded))

    # 2. Once a head is complete, offer the parts that may follow it.
    exact = registry.by_head.get(tuple(words)) if words else None
    if exact:
        for bound in exact:
            for seg in bound.order:
                if seg[0] != "particle":
                    continue
                phrase = " ".join(seg[1])
                if phrase in seen:
                    continue
                seen.add(phrase)
                items.append({
                    "label": phrase,
                    "kind": 24,                       # operator
                    "detail": ("optional" if seg[2] else "required")
                              + " part of " + " ".join(bound.head),
                    "documentation": describe(bound),
                    "sortText": "1" + phrase,
                })

    # 3. Names that are in scope.
    for name in analysis.names:
        if partial_folded and not name.casefold().startswith(partial_folded):
            continue
        cid = analysis.declarations[name][0]
        items.append({
            "label": name,
            "kind": 6,                                # variable
            "detail": cid.rsplit(".", 1)[-1],
            "sortText": "2" + name,
        })

    return items


def head_item(bound, head: tuple, typed: tuple) -> dict:
    slots = [s.name for s in bound.sig.slots_in_order() if s.kind != "block"]
    detail = " " + " ".join("<" + s + ">" for s in slots) if slots else ""
    remaining = " ".join(head[len(typed):])
    return {
        "label": " ".join(head),
        "labelDetails": {"detail": detail, "description": bound.cid},
        "kind": 14,                                   # keyword
        "detail": detail.strip(),
        "documentation": describe(bound),
        "insertText": remaining,
        "filterText": remaining,
        "sortText": "0" + " ".join(head),
        "data": {"cid": bound.cid},
    }


def describe(bound) -> dict:
    lines = ["`" + " ".join(bound.head) + "`", ""]
    for seg in bound.order:
        if seg[0] == "slot":
            slot = seg[1].slot
            note = " (runs to the end of the line)" if slot.is_open else ""
            terms = seg[1].terminators
            if terms:
                note = " (ends at " + " or ".join(
                    "`" + " ".join(t) + "`" for t in terms) + ")"
            lines.append("- **" + slot.name + "** — " + slot.kind + note)
        elif seg[0] == "particle":
            lines.append("- " + ("optional " if seg[2] else "")
                         + "`" + " ".join(seg[1]) + "`")
        elif seg[0] == "subject":
            lines.append("- **subject** — the thing the event happens to")
    if bound.sig.gives and bound.sig.gives != "nothing":
        lines += ["", "gives " + bound.sig.gives]
    if bound.sig.effects:
        lines.append("effects " + " ".join(sorted(bound.sig.effects)))
    lines += ["", "`" + bound.cid + "`"]
    return {"kind": "markdown", "value": "\n".join(lines)}


# --------------------------------------------------------------------------
# Feature: hover, definition, symbols
# --------------------------------------------------------------------------


def hover(analysis: Analysis, line: int, character: int) -> Optional[dict]:
    text = analysis.line_text(line)
    col = from_utf16(text, character) + 1
    node, step = analysis.step_at(line, col)
    if node is None:
        return None
    bound = analysis.registry.bound.get(node.cid)
    if step is None or bound is None:
        return None

    role = step.get("role", "")
    if role == "head":
        body = describe(bound)["value"]
    elif role == "particle":
        body = ("`" + step.get("matched", "") + "` — part of `"
                + " ".join(bound.head) + "`")
    elif role.startswith("slot:") or role == "ref":
        slot_name = step.get("slot", "subject")
        kind = role.split(":", 1)[-1] if ":" in role else "ref"
        body = "**" + str(slot_name) + "** — " + kind
        stopped = step.get("stopped at")
        if stopped:
            body += "\n\nstops at `" + str(stopped) + "`"
        resolved = step.get("resolved") or step.get("value")
        if resolved:
            body += "\n\n= `" + str(resolved) + "`"
    else:
        return None

    return {"contents": {"kind": "markdown", "value": body}}


def definition(analysis: Analysis, line: int, character: int):
    text = analysis.line_text(line)
    col = from_utf16(text, character) + 1
    node, step = analysis.step_at(line, col)
    if step is None:
        return None
    target = step.get("resolved") or step.get("value")
    if not isinstance(target, str):
        return None
    entry = analysis.declarations.get(target)
    if entry is None:
        return None
    _cid, span = entry
    first_line, first_col, _ = span[0]
    last_line, last_col, last_len = span[-1]
    start_text = analysis.line_text(first_line)
    end_text = analysis.line_text(last_line)
    return {
        "uri": analysis.uri,
        "range": {
            "start": {"line": first_line - 1,
                      "character": to_utf16(start_text, first_col - 1)},
            "end": {"line": last_line - 1,
                    "character": to_utf16(end_text, last_col - 1 + last_len)},
        },
    }


SYMBOL_KIND = {
    "core.decl.page": 2,        # module
    "core.decl.message": 14,    # constant
    "core.decl.let": 14,
    "core.decl.keep": 13,       # variable
}


def document_symbols(analysis: Analysis) -> list:
    if analysis.comp is None:
        return []
    out = []
    for node in analysis.comp.items:
        symbol = symbol_for(analysis, node)
        if symbol:
            out.append(symbol)
    return out


def symbol_for(analysis: Analysis, node) -> Optional[dict]:
    slot = shark.DECLARING_CIDS.get(node.cid)
    if not slot or slot not in node.args:
        return None
    pieces = node.args[slot].get("name") or []
    name = " ".join(p for p in pieces if isinstance(p, str))
    if not name:
        return None
    text = analysis.line_text(node.line)
    full = {"start": {"line": node.line - 1, "character": 0},
            "end": {"line": node.line - 1, "character": to_utf16(text, len(text))}}
    children = [s for s in (symbol_for(analysis, c) for c in node.body) if s]
    return {
        "name": name,
        "detail": node.cid.rsplit(".", 1)[-1],
        "kind": SYMBOL_KIND.get(node.cid, 13),
        "range": full,
        "selectionRange": full,
        "children": children,
    }


def parse_trace(analysis: Analysis, line: int) -> dict:
    node = analysis.node_by_line.get(line)
    if node is None:
        return {"line": line, "steps": []}
    steps = []
    for step in flatten_trace(node.trace):
        steps.append({k: v for k, v in step.items() if k != "span"})
    return {"line": line, "cid": node.cid, "steps": steps}


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------


class Server:
    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.documents: dict = {}
        self.analyses: dict = {}
        self.shutdown_requested = False

    # -- transport -------------------------------------------------------

    def read(self) -> Optional[dict]:
        headers = {}
        while True:
            raw = self.stdin.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                break
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        if length <= 0:
            return None
        body = self.stdin.read(length)
        return json.loads(body.decode("utf-8"))

    def write(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = "Content-Length: {}\r\n\r\n".format(len(data)).encode("ascii")
        self.stdout.write(header)
        self.stdout.write(data)
        self.stdout.flush()

    def respond(self, request_id: Any, result: Any) -> None:
        self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def notify(self, method: str, params: Any) -> None:
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- documents -------------------------------------------------------

    def analyse(self, uri: str) -> Analysis:
        text = self.documents.get(uri, "")
        analysis = Analysis(text, uri)
        self.analyses[uri] = analysis
        return analysis

    def refresh(self, uri: str) -> None:
        analysis = self.analyse(uri)
        self.notify("textDocument/publishDiagnostics", {
            "uri": uri,
            "diagnostics": diagnostics_for(analysis),
        })

    def current(self, params: dict) -> Analysis:
        uri = params["textDocument"]["uri"]
        return self.analyses.get(uri) or self.analyse(uri)

    # -- dispatch --------------------------------------------------------

    def handle(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        request_id = message.get("id")

        if method == "initialize":
            self.respond(request_id, self.capabilities())
        elif method == "initialized":
            pass
        elif method == "shutdown":
            self.shutdown_requested = True
            self.respond(request_id, None)
        elif method == "exit":
            raise SystemExit(0 if self.shutdown_requested else 1)

        elif method == "textDocument/didOpen":
            doc = params["textDocument"]
            self.documents[doc["uri"]] = doc.get("text", "")
            self.refresh(doc["uri"])
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            changes = params.get("contentChanges") or []
            if changes:
                self.documents[uri] = changes[-1].get("text", "")
            self.refresh(uri)
        elif method == "textDocument/didSave":
            self.refresh(params["textDocument"]["uri"])
        elif method == "textDocument/didClose":
            uri = params["textDocument"]["uri"]
            self.documents.pop(uri, None)
            self.analyses.pop(uri, None)
            self.notify("textDocument/publishDiagnostics",
                        {"uri": uri, "diagnostics": []})

        elif method == "textDocument/completion":
            analysis = self.current(params)
            position = params["position"]
            self.respond(request_id, {
                "isIncomplete": False,
                "items": completions(analysis, position["line"] + 1,
                                     position["character"]),
            })
        elif method == "completionItem/resolve":
            self.respond(request_id, params)
        elif method == "textDocument/hover":
            analysis = self.current(params)
            position = params["position"]
            self.respond(request_id, hover(analysis, position["line"] + 1,
                                           position["character"]))
        elif method == "textDocument/definition":
            analysis = self.current(params)
            position = params["position"]
            self.respond(request_id, definition(analysis, position["line"] + 1,
                                                position["character"]))
        elif method == "textDocument/documentSymbol":
            self.respond(request_id, document_symbols(self.current(params)))
        elif method == "textDocument/semanticTokens/full":
            self.respond(request_id, {"data": semantic_tokens(self.current(params))})

        elif method == "shark/parseTrace":
            analysis = self.current(params)
            self.respond(request_id, parse_trace(analysis, params.get("line", 1)))
        elif method == "shark/canonicalHash":
            analysis = self.current(params)
            self.respond(request_id, {
                "hash": analysis.comp.hash if analysis.comp else None,
                "locale": analysis.locale_tag,
            })

        elif request_id is not None:
            self.respond(request_id, None)

    def capabilities(self) -> dict:
        return {
            "capabilities": {
                "positionEncoding": "utf-16",
                "textDocumentSync": {"openClose": True, "change": 1, "save": True},
                "completionProvider": {
                    "triggerCharacters": [" "],
                    "resolveProvider": True,
                },
                "hoverProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
                "semanticTokensProvider": {
                    "legend": {"tokenTypes": TOKEN_TYPES,
                               "tokenModifiers": TOKEN_MODIFIERS},
                    "full": True,
                },
                "experimental": {
                    "shark": {"parseTrace": True, "canonicalHash": True}},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def serve(self) -> int:
        log("ready")
        while True:
            try:
                message = self.read()
            except Exception:
                log("bad message"), log(traceback.format_exc())
                continue
            if message is None:
                return 0
            try:
                self.handle(message)
            except SystemExit as stop:
                return int(stop.code or 0)
            except Exception:
                log(traceback.format_exc())
                if message.get("id") is not None:
                    self.write({
                        "jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32603,
                                  "message": "internal error, see server log"},
                    })


# --------------------------------------------------------------------------
# Self test
# --------------------------------------------------------------------------


SAMPLE = """page Home
  show text Welcome
  show button Start to page Game

page Game
  show text Game started
"""


def selftest() -> int:
    analysis = Analysis(SAMPLE, "file:///sample.shark")
    checks = []

    checks.append(("compiles", analysis.error is None))
    checks.append(("no diagnostics", diagnostics_for(analysis) == []))

    tokens = semantic_tokens(analysis)
    checks.append(("semantic tokens produced", len(tokens) > 0 and len(tokens) % 5 == 0))

    heads = [item["label"] for item in completions(analysis, 2, 2)]
    checks.append(("completes heads after `sh`", "show text" in heads))

    parts = [item["label"] for item in completions(analysis, 3, 14)]
    checks.append(("offers `to page` after `show button`", "to page" in parts))

    names = [item["label"] for item in completions(analysis, 6, 0)]
    checks.append(("offers declared names", "Home" in names and "Game" in names))

    info = hover(analysis, 3, 3)
    checks.append(("hover on a head", info is not None
                   and "show button" in info["contents"]["value"]))

    where = definition(analysis, 3, 29)
    checks.append(("go to definition of `Game`",
                   where is not None and where["range"]["start"]["line"] == 4))

    symbols = document_symbols(analysis)
    checks.append(("document symbols", [s["name"] for s in symbols] == ["Home", "Game"]))

    trace = parse_trace(analysis, 3)
    roles = [s.get("role") for s in trace["steps"]]
    checks.append(("parse trace", "head" in roles and "particle" in roles))

    broken = Analysis("show button to page Home\n", "file:///bad.shark")
    found = diagnostics_for(broken)
    checks.append(("reports E-0231", found and found[0]["code"] == "E-0231"))

    width = max(len(name) for name, _ in checks)
    failed = 0
    for name, passed in checks:
        print(("  ok    " if passed else "  FAIL  ") + name.ljust(width))
        failed += 0 if passed else 1
    print()
    print("{} passed, {} failed".format(len(checks) - failed, failed))
    return 1 if failed else 0


def main(argv: list) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--version" in argv:
        print(SERVER_NAME, SERVER_VERSION)
        return 0
    return Server(sys.stdin.buffer, sys.stdout.buffer).serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
