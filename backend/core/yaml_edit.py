"""Change one key in a YAML file's text while keeping everything else, comments included.

The block of the key (its line and the more-indented lines under it) is replaced with the new
value; a missing key is added at the end of its parent block. The result is parsed again and
must equal the intended data exactly, otherwise ``YamlEditError`` is raised and nothing should
be written. Only block-style mappings are supported (what PIE's config files use).
"""

import re
from typing import Any

import yaml

_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_][A-Za-z0-9_ \-]*?)\s*:(?:\s|$)")


class YamlEditError(ValueError):
    pass


def _is_filler(line: str) -> bool:
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find(lines: list[str], path: list[str]) -> tuple[int, int, int] | None:
    """(start, end, indent) of the block for ``path``: the key's line through its last
    content line (end exclusive; trailing comments/blank lines belong to what follows).
    None if the key is missing."""
    lo, hi, parent_indent = 0, len(lines), -1
    for depth, key in enumerate(path):
        child_indent: int | None = None
        found = None
        for i in range(lo, hi):
            line = lines[i]
            if _is_filler(line):
                continue
            ind = _indent(line)
            if ind <= parent_indent:
                break
            if child_indent is None:
                child_indent = ind
            if ind != child_indent:
                continue
            m = _KEY.match(line)
            if m and m["key"] == key:
                found = i
                break
        if found is None or child_indent is None:
            return None
        last = found + 1
        for j in range(found + 1, hi):
            line = lines[j]
            if _is_filler(line):
                continue
            ind = _indent(line)
            # List items written at the key's own indent ("key:" then "- item") belong to it.
            same_level_item = ind == child_indent and line.lstrip().startswith("- ")
            if ind < child_indent or (ind == child_indent and not same_level_item):
                break
            last = j + 1
        if depth == len(path) - 1:
            return found, last, child_indent
        lo, hi, parent_indent = found + 1, last, child_indent
    return None


def _render(key: str, value: Any, indent: int) -> list[str]:
    text = yaml.safe_dump(
        {key: value}, default_flow_style=False, allow_unicode=True, sort_keys=False, width=1000
    )
    pad = " " * indent
    return [pad + line for line in text.rstrip("\n").split("\n")]


def _set_in(data: Any, path: list[str], value: Any) -> Any:
    if not path:
        return value
    data = dict(data or {})
    data[path[0]] = _set_in(data.get(path[0]), path[1:], value)
    return data


def set_value(text: str, path: list[str], value: Any) -> str:
    """Return ``text`` with ``path`` set to ``value`` (comments elsewhere kept)."""
    if not path:
        raise YamlEditError("Empty path")
    try:
        original = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise YamlEditError(f"The file is not valid YAML: {exc}") from exc
    expected = _set_in(original, path, value)
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines = lines[:-1]
    found = _find(lines, path)
    if found is not None:
        start, end, indent = found
        lines[start:end] = _render(path[-1], value, indent)
    elif len(path) > 1:
        parent = _find(lines, path[:-1])
        if parent is None:
            return set_value(text, path[:-1], {path[-1]: value})
        pstart, pend, pindent = parent
        body = [ln for ln in lines[pstart + 1 : pend] if not _is_filler(ln)]
        if body and _KEY.match(body[0]) is None:
            raise YamlEditError(f"{'.'.join(path[:-1])} is not a mapping")
        child_indent = _indent(body[0]) if body else pindent + 2
        if not body:  # e.g. "verified_answers: {}" or an empty key: make it a block
            lines[pstart] = " " * pindent + f"{path[-2]}:"
        lines[pend:pend] = _render(path[-1], value, child_indent)
    else:
        lines += _render(path[-1], value, 0)
    result = "\n".join(lines) + ("\n" if trailing_newline else "")
    try:
        parsed = yaml.safe_load(result) or {}
    except yaml.YAMLError as exc:
        raise YamlEditError(f"The edit produced invalid YAML: {exc}") from exc
    if parsed != expected:
        raise YamlEditError(f"Could not change {'.'.join(path)} safely; nothing was saved")
    return result
