"""A small, self-contained S-expression reader for KiCAD files.

KiCAD stores ``.kicad_pcb`` / ``.kicad_sch`` / ``.kicad_pro`` project data as
plain S-expressions: nested parenthesised lists whose leaves are either bare
symbols (``F.Cu``, ``1.5``, ``yes``) or double-quoted strings (``"Net-(R1-Pad1)"``).

This module tokenises and parses that syntax into a lightweight tree of
:class:`SNode` objects, plus a handful of accessor helpers. It deliberately
knows *nothing* about KiCAD's schema — a permissive reader that pulls only the
tokens FYPA needs is robust across KiCAD 7/8/9 (each of which adds tokens),
whereas a schema-bound library (e.g. kiutils) raises or drops on unknown
tokens. It also adds no third-party dependency and nothing for PyInstaller to
miss.

A parsed node is::

    SNode(tag="segment", items=[
        SNode(tag="start", items=["1.0", "2.0"]),
        SNode(tag="end",   items=["3.0", "4.0"]),
        SNode(tag="width", items=["0.25"]),
        SNode(tag="net",   items=["3"]),
    ])

Leaf atoms are always kept as ``str`` (quoted and bare atoms are
indistinguishable in the tree — callers coerce with :meth:`SNode.f` /
:meth:`SNode.s` as needed, which is all FYPA requires).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Backslash escape sequences KiCAD emits inside quoted strings.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


@dataclass(slots=True)
class SNode:
    """One S-expression list: a ``tag`` symbol followed by child items.

    ``items`` holds the children in order — each is either a leaf ``str`` (a
    bare atom or the unescaped contents of a quoted string) or a nested
    :class:`SNode`.
    """

    tag: str
    items: list = field(default_factory=list)

    # --- child-node access ------------------------------------------------
    def nodes(self, tag: str | None = None):
        """Child :class:`SNode`\\ s, optionally filtered to those named *tag*."""
        for it in self.items:
            if isinstance(it, SNode) and (tag is None or it.tag == tag):
                yield it

    def node(self, tag: str) -> SNode | None:
        """First child node named *tag*, or ``None``."""
        return next(self.nodes(tag), None)

    # --- leaf-atom access -------------------------------------------------
    @property
    def atoms(self) -> list[str]:
        """The leaf (``str``) children, in order."""
        return [it for it in self.items if isinstance(it, str)]

    def atom(self, index: int = 0, default: str | None = None) -> str | None:
        """The *index*-th leaf atom of this node, or *default*."""
        a = self.atoms
        return a[index] if 0 <= index < len(a) else default

    def f_at(self, index: int = 0, default: float = 0.0) -> float:
        """The *index*-th leaf atom of this node coerced to ``float``.

        Used for positional numeric payloads like ``(start x y)`` or
        ``(at x y rot)``.
        """
        v = self.atom(index)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            return default

    def s(self, tag: str, index: int = 0, default: str | None = None) -> str | None:
        """First leaf atom of the child named *tag* (``(tag value ...)``)."""
        child = self.node(tag)
        return child.atom(index, default) if child is not None else default

    def f(self, tag: str, index: int = 0, default: float = 0.0) -> float:
        """First leaf atom of child *tag* coerced to ``float`` (or *default*)."""
        v = self.s(tag, index)
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            return default


def _tokenize(text: str):
    """Yield ``("(" | ")" , None)`` / ``("atom", str)`` tokens for *text*."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "(" or c == ")":
            yield (c, None)
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                elif ch == '"':
                    i += 1
                    break
                else:
                    buf.append(ch)
                    i += 1
            yield ("atom", "".join(buf))
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            yield ("atom", text[i:j])
            i = j


def parse(text: str) -> SNode:
    """Parse S-expression *text* and return its single top-level node.

    Raises :class:`ValueError` on unbalanced parentheses or empty input.
    """
    tokens = list(_tokenize(text))
    pos = 0

    def parse_list() -> SNode:
        nonlocal pos
        pos += 1  # consume '('
        # The tag is the first item; KiCAD always opens a list with a symbol.
        if pos >= len(tokens):
            raise ValueError("unexpected end of S-expression after '('")
        kind, val = tokens[pos]
        tag = val if kind == "atom" else ""
        if kind == "atom":
            pos += 1
        node = SNode(tag=tag)
        while pos < len(tokens):
            kind, val = tokens[pos]
            if kind == "(":
                node.items.append(parse_list())
            elif kind == ")":
                pos += 1
                return node
            else:
                node.items.append(val)
                pos += 1
        raise ValueError("unbalanced parentheses in S-expression")

    # Skip to the first '('.
    while pos < len(tokens) and tokens[pos][0] != "(":
        pos += 1
    if pos >= len(tokens):
        raise ValueError("no S-expression found")
    return parse_list()


def parse_file(path: str | Path) -> SNode:
    """Read and :func:`parse` the S-expression file at *path* (UTF-8)."""
    return parse(Path(path).read_text(encoding="utf-8"))
