"""Small stdio Language Server Protocol bridge for the graph service."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .graph import GraphNode
from .service import GraphService

LSP_SYMBOL_KIND = {
    "File": 1,
    "Class": 5,
    "Function": 12,
    "Test": 12,
    "Type": 23,
}


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    return unquote(parsed.path)


def path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _range(node: GraphNode) -> dict[str, Any]:
    start = max(node.line_start - 1, 0)
    end = max(node.line_end - 1, start)
    return {
        "start": {"line": start, "character": 0},
        "end": {"line": end, "character": 0},
    }


def _location(node: GraphNode) -> dict[str, Any]:
    return {"uri": path_to_uri(node.file_path), "range": _range(node)}


def _document_symbol(node: GraphNode) -> dict[str, Any]:
    return {
        "name": node.name,
        "detail": node.qualified_name,
        "kind": LSP_SYMBOL_KIND.get(node.kind, 13),
        "range": _range(node),
        "selectionRange": _range(node),
    }


class LspServer:
    def __init__(self, repo_root: str | None = None) -> None:
        self.service = GraphService(repo_root)
        self.shutdown_requested = False

    def close(self) -> None:
        self.service.close()

    def dispatch(self, method: str, params: dict[str, Any] | None) -> Any:
        params = params or {}
        if method == "initialize":
            return self.initialize()
        if method == "shutdown":
            self.shutdown_requested = True
            return None
        if method == "exit":
            raise SystemExit(0 if self.shutdown_requested else 1)
        if method == "initialized":
            return None
        if method == "workspace/symbol":
            return self.workspace_symbol(params)
        if method == "textDocument/documentSymbol":
            return self.document_symbol(params)
        if method == "textDocument/references":
            return self.references(params)
        if method == "textDocument/definition":
            return self.definition(params)
        if method == "textDocument/codeLens":
            return self.code_lens(params)
        if method == "workspace/executeCommand":
            return self.execute_command(params)
        return None

    def initialize(self) -> dict[str, Any]:
        return {
            "serverInfo": {"name": "axon-graph-lsp", "version": "0.1.0"},
            "capabilities": {
                "definitionProvider": True,
                "referencesProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "codeLensProvider": {"resolveProvider": False},
                "executeCommandProvider": {
                    "commands": [
                        "axon.blastRadius",
                        "axon.callers",
                        "axon.callees",
                    ],
                },
            },
        }

    def workspace_symbol(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(params.get("query") or "")
        if not query:
            return []
        result = self.service.search(query, limit=100)
        return [
            {
                "name": node["name"],
                "kind": LSP_SYMBOL_KIND.get(node["kind"], 13),
                "location": {
                    "uri": path_to_uri(node["file_path"]),
                    "range": {
                        "start": {"line": max(node["line_start"] - 1, 0), "character": 0},
                        "end": {"line": max(node["line_end"] - 1, 0), "character": 0},
                    },
                },
                "containerName": node.get("parent_name") or node["file_path"],
            }
            for node in result["nodes"]
        ]

    def document_symbol(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        text_document = params.get("textDocument") or {}
        uri = text_document.get("uri")
        if not uri:
            return []
        summary = self.service.file_summary(uri_to_path(uri))
        return [
            _document_symbol(node)
            for raw in summary["nodes"]
            if (node := self.service.store.get_node(raw["qualified_name"])) is not None
            and node.kind != "File"
        ]

    def references(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        node = self._node_from_position(params)
        if not node:
            return []
        refs = []
        callers = self.service.callers(node.qualified_name)
        callees = self.service.callees(node.qualified_name)
        for edge in callers.get("edges", []) + callees.get("edges", []):
            endpoint = self.service.store.get_node(edge["source"])
            if endpoint:
                refs.append(_location(endpoint))
            endpoint = self.service.store.get_node(edge["target"])
            if endpoint:
                refs.append(_location(endpoint))
        return refs

    def definition(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        node = self._node_from_position(params)
        return [_location(node)] if node else []

    def code_lens(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        text_document = params.get("textDocument") or {}
        uri = text_document.get("uri")
        if not uri:
            return []
        summary = self.service.file_summary(uri_to_path(uri))
        lenses = []
        for raw in summary["nodes"]:
            if raw["kind"] not in ("Function", "Class", "Test"):
                continue
            callers = self.service.callers(raw["qualified_name"])
            title = f"{callers.get('total', 0)} callers"
            lenses.append({
                "range": {
                    "start": {"line": max(raw["line_start"] - 1, 0), "character": 0},
                    "end": {"line": max(raw["line_start"] - 1, 0), "character": 0},
                },
                "command": {
                    "title": title,
                    "command": "axon.callers",
                    "arguments": [raw["qualified_name"]],
                },
            })
        return lenses

    def execute_command(self, params: dict[str, Any]) -> dict[str, Any]:
        command = params.get("command")
        args = params.get("arguments") or []
        target = str(args[0]) if args else ""
        if command == "axon.callers":
            return self.service.callers(target)
        if command == "axon.callees":
            return self.service.callees(target)
        if command == "axon.blastRadius":
            node = self.service.store.get_node(target)
            if not node:
                return {"status": "not_found", "target": target}
            return self.service.impact([node.file_path])
        return {"status": "error", "error": f"unknown command: {command}"}

    def _node_from_position(self, params: dict[str, Any]) -> GraphNode | None:
        text_document = params.get("textDocument") or {}
        position = params.get("position") or {}
        uri = text_document.get("uri")
        if not uri:
            return None
        return self.service.node_at(uri_to_path(uri), int(position.get("line", 0)) + 1)


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main(repo_root: str | None = None) -> None:
    server = LspServer(repo_root)
    try:
        while True:
            message = _read_message()
            if message is None:
                break
            msg_id = message.get("id")
            method = message.get("method")
            if not method:
                continue
            try:
                result = server.dispatch(method, message.get("params"))
                if msg_id is not None:
                    _write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})
            except SystemExit:
                raise
            except Exception as exc:
                if msg_id is not None:
                    _write_message({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(exc)},
                    })
    finally:
        server.close()
