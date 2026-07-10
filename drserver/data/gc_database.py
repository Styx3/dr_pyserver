"""In-memory registry + inheritance resolver for ``.gc`` content.

Faithful port of the relevant parts of C# ``Data/GCDatabase.cs``: load a tree of
``.gc`` files, register every node by name and by ``parent.child`` path, resolve
a dotted GC path to a node (exact / last-segment / last-two fallbacks), and
flatten an ``extends`` chain so a child sees its parents' properties.

The C# server loads a *flat* directory; the extracted client content is nested
(``skills/generic/…``), so :meth:`GCDatabase.load_tree` walks recursively. Nodes
are keyed by filename stem (== top-level GC object name), matching how ``extends``
paths resolve via their last segment.
"""
from __future__ import annotations

import os

from .gc_parser import GCNode, parse_file


class GCDatabase:
    def __init__(self) -> None:
        # Top-level nodes keyed by filename stem (== GC object name).
        self._nodes: dict[str, GCNode] = {}
        # Full path registry: "Stomp" and "Stomp.Effect" -> node.
        self._path_registry: dict[str, GCNode] = {}
        self._resolved_cache: dict[str, GCNode] = {}
        self.file_count = 0
        # Names seen more than once during a recursive load (diagnostic).
        self.collisions: list[str] = []

    # ── loading ──

    def load_tree(self, root: str, dotted_prefix: str | None = None) -> "GCDatabase":
        """Recursively parse every ``*.gc`` under ``root`` and register it.

        When ``dotted_prefix`` is given, each node is *additionally* registered
        under its full file-derived dotted path (``prefix`` + path relative to
        ``root``), e.g. ``quests/base/token/fi/MythicBody.gc`` →
        ``quests.base.token.fi.MythicBody``. This makes :meth:`resolve` find the
        exact file by its full ``extends`` path *before* the last-segment
        fallback — essential where stems collide across nested dirs (the quest
        ``token/`` tree repeats ``MythicBody`` four times). Default (``None``)
        leaves behaviour byte-identical to the original stem-only registration,
        so existing callers (skills) are unaffected.
        """
        if not os.path.isdir(root):
            raise NotADirectoryError(root)
        files: list[str] = []
        for dirpath, _dirs, names in os.walk(root):
            for nm in names:
                if nm.lower().endswith(".gc"):
                    files.append(os.path.join(dirpath, nm))
        files.sort()
        for fp in files:
            try:
                node = parse_file(fp)
            except Exception:  # pragma: no cover - skip unparseable, keep going
                continue
            if node is None or not node.name:
                continue
            self.file_count += 1
            if node.name in self._nodes:
                self.collisions.append(node.name)
            self._nodes[node.name] = node
            self._register(node.name, node)
            if dotted_prefix is not None:
                rel = os.path.splitext(os.path.relpath(fp, root))[0]
                dotted = rel.replace(os.sep, ".").replace("/", ".")
                full = f"{dotted_prefix}.{dotted}" if dotted_prefix else dotted
                self._register(full, node)
        return self

    def _register(self, parent_path: str, parent: GCNode) -> None:
        # Lowercase keys so lookups are case-insensitive (C# uses an
        # OrdinalIgnoreCase dictionary).
        self._path_registry[parent_path.lower()] = parent
        for key, child in parent.children.items():
            self._register(f"{parent_path}.{key}", child)

    # ── lookup ──

    def get_node(self, name_or_path: str) -> GCNode | None:
        return self._path_registry.get(name_or_path.lower())

    def resolve(self, path: str) -> GCNode | None:
        """Resolve a dotted GC path: exact -> last segment -> last two."""
        if not path:
            return None
        key = path.lower()
        exact = self._path_registry.get(key)
        if exact is not None:
            return exact

        last_dot = path.rfind(".")
        if last_dot >= 0:
            last = path[last_dot + 1 :]
            by_last = self._path_registry.get(last.lower())
            if by_last is not None:
                return by_last
            prev_dot = path.rfind(".", 0, last_dot)
            if prev_dot >= 0:
                last_two = path[prev_dot + 1 :]
                by_two = self._path_registry.get(last_two.lower())
                if by_two is not None:
                    return by_two
        return None

    def resolve_with_inheritance(self, path: str) -> GCNode | None:
        if path in self._resolved_cache:
            return self._resolved_cache[path]
        node = self.resolve(path)
        if node is None:
            return None
        resolved = self._flatten(node, set())
        self._resolved_cache[path] = resolved
        return resolved

    def flatten(self, node: GCNode) -> GCNode:
        """Flatten a specific node's ``extends`` chain against this registry.

        Unlike :meth:`resolve_with_inheritance` (which first resolves a *path*
        and so collapses colliding stems), this flattens the exact ``node`` you
        pass — the importer needs this to keep per-file identity when two files
        share a stem.
        """
        return self._flatten(node, set())

    def _flatten(self, node: GCNode, visited: set[str]) -> GCNode:
        key = f"{node.name}|{node.extends or ''}"
        if key in visited:
            return node
        visited.add(key)

        if not node.extends:
            return node
        parent = self.resolve(node.extends)
        if parent is None:
            return node
        resolved_parent = self._flatten(parent, visited)

        merged = GCNode(
            name=node.name,
            extends=node.extends,
            is_static=node.is_static,
            is_anonymous=node.is_anonymous,
            source_file=node.source_file,
        )
        merged.properties.update(resolved_parent.properties)
        merged.properties.update(node.properties)

        merged.children.update(resolved_parent.children)
        for k, child in node.children.items():
            if k in merged.children:
                merged.children[k] = self._merge_nodes(merged.children[k], child)
            else:
                merged.children[k] = child

        merged.anonymous_children.extend(resolved_parent.anonymous_children)
        merged.anonymous_children.extend(node.anonymous_children)
        return merged

    def _merge_nodes(self, parent: GCNode, child: GCNode) -> GCNode:
        merged = GCNode(
            name=child.name,
            extends=child.extends or parent.extends,
            is_static=child.is_static or parent.is_static,
            source_file=child.source_file,
        )
        merged.properties.update(parent.properties)
        merged.properties.update(child.properties)
        merged.children.update(parent.children)
        for k, c in child.children.items():
            if k in merged.children:
                merged.children[k] = self._merge_nodes(merged.children[k], c)
            else:
                merged.children[k] = c
        return merged
