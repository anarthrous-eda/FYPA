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

Tokenising uses one compiled regex over the whole text and the parser consumes
the token iterator with an explicit stack (no full token list materialised), so
a large zone-filled ``.kicad_pcb`` parses without the char-by-char loop and the
multi-hundred-MB transient token list a naive reader would build.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Backslash escape sequences KiCAD emits inside quoted strings.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}

# One token: an open/close paren, a double-quoted string (group 1 = its raw
# contents, escapes intact), or a bare atom (any run without whitespace, parens
# or a quote). Quoted strings are matched before bare atoms so a leading ``"``
# always opens a string.
_TOKEN_RE = re.compile(r'[()]|"((?:[^"\\]|\\.)*)"|[^\s()"]+')
_STR_ESCAPE_RE = re.compile(r"\\(.)")


def _unescape(s: str) -> str:
    """Resolve backslash escapes in a quoted-string body (``\\n`` etc.).

    Mirrors the original char-by-char reader: a known escape maps via
    :data:`_ESCAPES`, an unknown one drops the backslash and keeps the
    following character.
    """
    if "\\" not in s:
        return s
    return _STR_ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), s)


@dataclass(slots=True)
class SNode:
    """One S-expression list: a ``tag`` symbol followed by child items.

    ``items`` holds the children in order — each is either a leaf ``str`` (a
    bare atom or the unescaped contents of a quoted string) or a nested
    :class:`SNode`.

    ``_atoms`` / ``_by_tag`` are lazily-populated read caches. They are safe
    because the tree is only mutated while :func:`parse` builds it; every
    accessor below runs after parsing is complete, so a cache is never left
    stale. (Mutating ``items`` after an accessor has run would strand a cache —
    FYPA never does.)
    """

    tag: str
    items: list = field(default_factory=list)
    _atoms: list | None = field(default=None, compare=False, repr=False)
    _by_tag: dict | None = field(default=None, compare=False, repr=False)

    # --- child-node access ------------------------------------------------
    def nodes(self, tag: str | None = None):
        """Child :class:`SNode`\\ s, optionally filtered to those named *tag*.

        Filtered lookups build a ``tag → [SNode, ...]`` index once and reuse it,
        so repeatedly probing one node for many different tags (e.g. the huge
        top-level ``kicad_pcb`` node, queried for segments, vias, zones …) costs
        a single scan instead of one scan per tag.
        """
        if tag is None:
            return (it for it in self.items if isinstance(it, SNode))
        if self._by_tag is None:
            idx: dict[str, list] = {}
            for it in self.items:
                if isinstance(it, SNode):
                    idx.setdefault(it.tag, []).append(it)
            self._by_tag = idx
        return iter(self._by_tag.get(tag, ()))

    def node(self, tag: str) -> SNode | None:
        """First child node named *tag*, or ``None``."""
        return next(self.nodes(tag), None)

    # --- leaf-atom access -------------------------------------------------
    @property
    def atoms(self) -> list[str]:
        """The leaf (``str``) children, in order (cached after first access)."""
        if self._atoms is None:
            self._atoms = [it for it in self.items if isinstance(it, str)]
        return self._atoms

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
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok == "(" or tok == ")":
            yield (tok, None)
        elif tok[0] == '"':
            yield ("atom", _unescape(m.group(1)))
        else:
            yield ("atom", tok)


def parse(text: str) -> SNode:
    """Parse S-expression *text* and return its single top-level node.

    Consumes the token iterator with an explicit stack — no intermediate list
    of every token is built, so a large board's parse stays proportional to the
    tree it produces rather than to a materialised token stream.

    Raises :class:`ValueError` on unbalanced parentheses or empty input.
    """
    stack: list[SNode] = []
    root: SNode | None = None
    need_tag = False   # next atom is the current list's tag symbol

    for kind, val in _tokenize(text):
        if kind == "(":
            node = SNode(tag="")
            if stack:
                stack[-1].items.append(node)
            stack.append(node)
            need_tag = True
        elif kind == ")":
            if not stack:
                raise ValueError("unbalanced parentheses in S-expression")
            node = stack.pop()
            if not stack:
                root = node          # closed the top-level list
                break
            need_tag = False
        else:  # atom
            if not stack:
                continue             # stray atom before the first '(' — skip
            if need_tag:
                stack[-1].tag = val
                need_tag = False
            else:
                stack[-1].items.append(val)

    if stack:
        raise ValueError("unbalanced parentheses in S-expression")
    if root is None:
        raise ValueError("no S-expression found")
    return root


def parse_file(path: str | Path) -> SNode:
    """Read and :func:`parse` the S-expression file at *path* (UTF-8)."""
    return parse(Path(path).read_text(encoding="utf-8"))
