"""Parse Hive/Glue column type strings into a readable, flattened summary.

Glue's ``Column.Type`` is a Hive type string — scalars like ``bigint`` /
``decimal(10,2)`` but also nested ``struct<a:int,b:string>``,
``array<struct<...>>``, ``map<string,bigint>``. The reference agent's schema
tables list one row per (possibly nested) field, so we flatten these into
``FlatField(name, type, depth)`` rows the agent can drop into a ``# Schema``
markdown table (indent by ``depth`` for nested records).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FlatField:
    name: str  # dotted path, e.g. "author.login"
    type: str  # readable leaf/compound type
    depth: int  # nesting level (0 = top-level column)


def _split_top_level(inner: str) -> list[str]:
    """Split a comma-separated struct body, respecting ``<>`` and ``()`` nesting."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in inner:
        if ch in "<(":
            depth += 1
            buf.append(ch)
        elif ch in ">)":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _flatten(name: str, hive_type: str, depth: int, out: list[FlatField]) -> None:
    t = hive_type.strip()
    low = t.lower()
    if low.startswith("struct<") and t.endswith(">"):
        inner = t[len("struct<") : -1]
        out.append(FlatField(name=name, type="struct", depth=depth))
        for field in _split_top_level(inner):
            if ":" not in field:
                continue
            fname, ftype = field.split(":", 1)
            child = f"{name}.{fname.strip()}" if name else fname.strip()
            _flatten(child, ftype, depth + 1, out)
    elif low.startswith("array<") and t.endswith(">"):
        elem = t[len("array<") : -1].strip()
        if elem.lower().startswith("struct<"):
            # array<struct<...>>: a repeated record. Mark the column as such,
            # then flatten the struct's fields one level deeper under this name.
            out.append(FlatField(name=name, type="array<struct>", depth=depth))
            inner = elem[len("struct<") : -1]
            for field in _split_top_level(inner):
                if ":" not in field:
                    continue
                fname, ftype = field.split(":", 1)
                child = f"{name}.{fname.strip()}" if name else fname.strip()
                _flatten(child, ftype, depth + 1, out)
        else:
            out.append(FlatField(name=name, type=f"array<{elem}>", depth=depth))
    else:
        # Scalars, decimal(p,s), map<...>, and anything else: keep verbatim.
        out.append(FlatField(name=name, type=t, depth=depth))


def flatten_hive_type(name: str, hive_type: str) -> list[FlatField]:
    """Flatten one column into one-or-more readable rows (parent + nested)."""
    out: list[FlatField] = []
    _flatten(name, hive_type, 0, out)
    return out
