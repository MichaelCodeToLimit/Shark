#!/usr/bin/env python3
"""
A minimal LSP client, used to prove that editor/shark_lsp.py actually speaks the
protocol over a pipe — the same way VS Code, Neovim, Helix and Zed will.

    python tests/lsp_client.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SAMPLE = """page Home
  show text Welcome
  show button Start to page Game

page Game
  show text Game started
"""

BROKEN = "show button to page Home\n"


class Client:
    """Speaks LSP over the server's stdin and stdout."""

    def __init__(self, root: str):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(root, "editor", "shark_lsp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self.next_id = 0
        self.notifications: list = []

    # -- transport -------------------------------------------------------

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = "Content-Length: {}\r\n\r\n".format(len(data))
        assert self.proc.stdin is not None
        self.proc.stdin.write(header.encode("ascii"))
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _read(self) -> Optional[dict]:
        assert self.proc.stdout is not None
        headers = {}
        while True:
            raw = self.proc.stdout.readline()
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
        return json.loads(self.proc.stdout.read(length).decode("utf-8"))

    # -- rpc -------------------------------------------------------------

    def request(self, method: str, params: Any) -> Any:
        self.next_id += 1
        wanted = self.next_id
        self._send({"jsonrpc": "2.0", "id": wanted,
                    "method": method, "params": params})
        while True:
            message = self._read()
            if message is None:
                raise RuntimeError("server closed while waiting for " + method)
            if message.get("id") == wanted:
                if "error" in message:
                    raise RuntimeError(method + ": " + str(message["error"]))
                return message.get("result")
            if "method" in message:
                self.notifications.append(message)

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def drain(self, method: str, uri: Optional[str] = None,
              limit: int = 8) -> list:
        """Collect notifications of one kind, optionally for one document."""
        def matches(note: dict) -> bool:
            if note.get("method") != method:
                return False
            return uri is None or (note.get("params") or {}).get("uri") == uri

        found = [n for n in self.notifications if matches(n)]
        while not found and limit > 0:
            message = self._read()
            limit -= 1
            if message is None:
                break
            self.notifications.append(message)
            if matches(message):
                found.append(message)
        return found

    def open(self, uri: str, text: str, language: str = "shark") -> None:
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language, "version": 1, "text": text}})

    def close(self) -> None:
        try:
            self.request("shutdown", None)
            self.notify("exit", None)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def smoke(root: str = ROOT) -> dict:
    """Drive the server the way an editor would. Returns a result summary."""
    client = Client(root)
    result: dict = {}
    try:
        init = client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": None,
            "capabilities": {},
        })
        caps = init.get("capabilities", {})
        result["initialized"] = bool(caps)
        result["serverInfo"] = init.get("serverInfo")
        result["has_completion"] = "completionProvider" in caps
        result["has_tokens"] = "semanticTokensProvider" in caps
        client.notify("initialized", {})

        good = "file:///tour.shark"
        client.open(good, SAMPLE)
        published = client.drain("textDocument/publishDiagnostics")
        result["diagnostics_ok"] = bool(published) and \
            published[0]["params"]["diagnostics"] == []

        completion = client.request("textDocument/completion", {
            "textDocument": {"uri": good},
            "position": {"line": 1, "character": 2},
        })
        labels = [item["label"] for item in completion.get("items", [])]
        result["completion_ok"] = "show text" in labels
        result["completion_count"] = len(labels)

        tokens = client.request("textDocument/semanticTokens/full",
                                {"textDocument": {"uri": good}})
        data = tokens.get("data", [])
        result["tokens_ok"] = len(data) > 0 and len(data) % 5 == 0
        result["token_count"] = len(data) // 5

        symbols = client.request("textDocument/documentSymbol",
                                 {"textDocument": {"uri": good}})
        result["symbols"] = [s["name"] for s in symbols]

        info = client.request("textDocument/hover", {
            "textDocument": {"uri": good},
            "position": {"line": 2, "character": 3},
        })
        result["hover_ok"] = info is not None and \
            "show button" in info["contents"]["value"]

        where = client.request("textDocument/definition", {
            "textDocument": {"uri": good},
            "position": {"line": 2, "character": 29},
        })
        result["definition_line"] = where["range"]["start"]["line"] if where else None

        trace = client.request("shark/parseTrace",
                               {"textDocument": {"uri": good}, "line": 3})
        result["trace_roles"] = [s.get("role") for s in trace.get("steps", [])]

        bad = "file:///bad.shark"
        client.open(bad, BROKEN)
        published = client.drain("textDocument/publishDiagnostics", bad)
        codes = [d["code"] for n in published for d in n["params"]["diagnostics"]]
        result["error_ok"] = "E-0231" in codes
        result["error_codes"] = codes
    finally:
        client.close()
    return result


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    result = smoke()
    checks = [
        ("initialize answered", result.get("initialized")),
        ("advertises completion", result.get("has_completion")),
        ("advertises semantic tokens", result.get("has_tokens")),
        ("clean file has no diagnostics", result.get("diagnostics_ok")),
        ("completion over the wire", result.get("completion_ok")),
        ("semantic tokens over the wire", result.get("tokens_ok")),
        ("document symbols", result.get("symbols") == ["Home", "Game"]),
        ("hover", result.get("hover_ok")),
        ("go to definition", result.get("definition_line") == 4),
        ("parse trace extension", "head" in (result.get("trace_roles") or [])),
        ("broken file reports E-0231", result.get("error_ok")),
    ]
    width = max(len(name) for name, _ in checks)
    failed = 0
    for name, passed in checks:
        print(("  ok    " if passed else "  FAIL  ") + name.ljust(width))
        failed += 0 if passed else 1
    print()
    print("server:", result.get("serverInfo"))
    print("completions offered:", result.get("completion_count"),
          " semantic tokens:", result.get("token_count"))
    print()
    print("{} passed, {} failed".format(len(checks) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
