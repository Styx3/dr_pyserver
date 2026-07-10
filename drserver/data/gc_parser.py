"""Parser for the Dungeon Runners ``.gc`` content format.

Faithful port of the C# ``Data/GCParser.cs`` from DR-Server. The ``.gc`` files
under ``extracter/`` are tier-2 ground truth (extracted client content), so this
parser is the foundation for rebuilding the server's content tables directly
from what the client actually ships, instead of trusting the baked SQLite
snapshot inherited from the older C# build.

Grammar (informal):

    [static] Name [extends dotted.Parent.Path] { body }

    body := ( property | child_block )*
    property   := Key = Value ;        # value: quoted string or bare-to-EOL/;/}
    child_block := [static] Name [extends Path] { body }

``*`` is the anonymous-child marker (``* extends Foo { ... }``). Comments are
``//`` line and ``/* */`` block, ignored inside quoted strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Characters that may appear in a "word" (name / extends path / bare token).
# Mirrors the C# TryReadWord set: letters, digits, _ . - * : (paths, item
# tiers like ClothArmor0-1, anonymous ``*``, and ``VISUAL:`` style prefixes).
_WORD_EXTRA = set("_.-*:")


@dataclass
class GCNode:
    """One node in a parsed ``.gc`` tree.

    Mirrors the C# ``GCNode``: a name, an optional ``extends`` path, flat
    ``properties`` (key->str, case-insensitive lookup), named ``children`` and
    a list of ``anonymous_children`` (``* extends ...`` blocks).
    """

    name: str = ""
    extends: str | None = None
    is_static: bool = False
    is_anonymous: bool = False
    source_file: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    children: dict[str, "GCNode"] = field(default_factory=dict)
    anonymous_children: list["GCNode"] = field(default_factory=list)

    # ── property accessors (case-insensitive, like the C# Dictionary) ──

    def _lookup(self, key: str) -> str | None:
        kl = key.lower()
        for k, v in self.properties.items():
            if k.lower() == kl:
                return v
        return None

    def get_string(self, key: str, fallback: str = "") -> str:
        v = self._lookup(key)
        return v if v is not None else fallback

    def get_float(self, key: str, fallback: float = 0.0) -> float:
        v = self._lookup(key)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                pass
        return fallback

    def get_int(self, key: str, fallback: int = 0) -> int:
        v = self._lookup(key)
        if v is not None:
            try:
                return int(v)
            except ValueError:
                try:
                    return int(float(v))
                except ValueError:
                    pass
        return fallback

    def get_bool(self, key: str, fallback: bool = False) -> bool:
        v = self._lookup(key)
        if v is not None:
            return v.lower() == "true" or v == "1"
        return fallback

    def has_property(self, key: str) -> bool:
        return self._lookup(key) is not None

    def get_child(self, name: str) -> "GCNode | None":
        nl = name.lower()
        for k, v in self.children.items():
            if k.lower() == nl:
                return v
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"GCNode[{self.name}] extends={self.extends or 'none'} "
            f"props={len(self.properties)} children={len(self.children)}"
        )


# ──────────────────────────────────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────────────────────────────────


def parse_file(path: str) -> GCNode | None:
    """Parse a ``.gc`` file. The node name defaults to the filename stem."""
    import os

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    stem = os.path.splitext(os.path.basename(path))[0]
    return parse(text, stem)


def parse(text: str, source_file: str = "") -> GCNode | None:
    """Parse ``.gc`` source text into a :class:`GCNode` tree."""
    text = _strip_comments(text)
    pos = 0
    node, _ = _parse_top_level(text, pos, source_file)
    return node


def _strip_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if c == '"' and (i == 0 or text[i - 1] != "\\"):
            in_string = not in_string
            out.append(c)
            i += 1
            continue
        if in_string:
            out.append(c)
            i += 1
            continue
        if i + 1 < n and c == "/" and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if i + 1 < n and c == "/" and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            if i + 1 < n:
                i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _read_word(text: str, pos: int) -> tuple[str | None, int]:
    pos = _skip_ws(text, pos)
    n = len(text)
    if pos >= n:
        return None, pos
    start = pos
    while pos < n and (text[pos].isalnum() or text[pos] in _WORD_EXTRA):
        pos += 1
    if pos == start:
        return None, pos
    return text[start:pos], pos


def _is_word(text: str, pos: int, word: str) -> bool:
    if pos + len(word) > len(text):
        return False
    if text[pos : pos + len(word)].lower() != word.lower():
        return False
    after = pos + len(word)
    if after < len(text) and text[after].isalnum():
        return False
    return True


def _parse_top_level(text: str, pos: int, source_file: str) -> tuple[GCNode | None, int]:
    pos = _skip_ws(text, pos)
    is_static = False

    first, pos = _read_word(text, pos)
    if first is None:
        return None, pos
    if first.lower() == "static":
        is_static = True
        name, pos = _read_word(text, pos)
    else:
        name = first
    if name is None:
        return None, pos

    extends_ = None
    saved = pos
    maybe, pos = _read_word(text, pos)
    if maybe is not None and maybe.lower() == "extends":
        extends_, pos = _read_word(text, pos)
    else:
        pos = saved

    pos = _skip_ws(text, pos)
    node = GCNode(
        name=name,
        extends=extends_,
        is_static=is_static,
        is_anonymous=(name == "*"),
        source_file=source_file,
    )
    if pos < len(text) and text[pos] == "{":
        pos += 1
        pos = _parse_block_body(text, pos, node, source_file)
    return node, pos


def _parse_block_body(text: str, pos: int, parent: GCNode, source_file: str) -> int:
    n = len(text)
    while pos < n:
        pos = _skip_ws(text, pos)
        if pos >= n:
            break
        if text[pos] == "}":
            return pos + 1

        is_static = False
        word, pos = _read_word(text, pos)
        if word is None:
            break
        if word.lower() == "static":
            is_static = True
            word, pos = _read_word(text, pos)
            if word is None:
                break

        pos = _skip_ws(text, pos)
        if pos >= n:
            break
        nxt = text[pos]

        if nxt == "=":
            pos += 1
            value, pos = _read_value(text, pos)
            parent.properties[word] = value
        elif nxt == "{" or _is_word(text, pos, "extends"):
            child_extends = None
            if _is_word(text, pos, "extends"):
                pos += len("extends")
                child_extends, pos = _read_word(text, pos)
                pos = _skip_ws(text, pos)
            child = GCNode(
                name=word,
                extends=child_extends,
                is_static=is_static,
                is_anonymous=(word == "*"),
                source_file=source_file,
            )
            if pos < n and text[pos] == "{":
                pos += 1
                pos = _parse_block_body(text, pos, child, source_file)
            if child.is_anonymous:
                parent.anonymous_children.append(child)
            else:
                parent.children[word] = child
        else:
            pos = _skip_to_next_statement(text, pos)
    return pos


def _read_value(text: str, pos: int) -> tuple[str, int]:
    pos = _skip_ws(text, pos)
    n = len(text)
    if pos >= n:
        return "", pos
    out: list[str] = []
    if text[pos] == '"':
        pos += 1
        while pos < n and text[pos] != '"':
            if text[pos] == "\\" and pos + 1 < n:
                out.append(text[pos + 1])
                pos += 2
            else:
                out.append(text[pos])
                pos += 1
        if pos < n:
            pos += 1  # closing quote
    else:
        while pos < n and text[pos] not in ';\n\r}':
            out.append(text[pos])
            pos += 1
    # Consume the terminating ';' even when whitespace (e.g. a newline after a
    # multi-line quoted string) separates it from the value:
    #     Summary = "…long…"\n        ;
    # Without this, the stray ';' on its own line aborts the enclosing block and
    # silently drops every property after it (e.g. QuestObsolete lost
    # TokenReward/CashReward → wrong inherited rewards).
    look = pos
    while look < n and text[look] in " \t\r\n":
        look += 1
    if look < n and text[look] == ";":
        pos = look + 1
    return "".join(out).strip(), pos


def _skip_to_next_statement(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos] not in ';}{\n':
        pos += 1
    if pos < n and text[pos] == ";":
        pos += 1
    return pos
