"""Defensive immutability helpers for adaptive contract mappings."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping


def freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Recursively freeze mappings (and nested dict/list containers)."""

    if value is None:
        return MappingProxyType({})
    if isinstance(value, MappingProxyType):
        # Re-freeze to ensure nested mutables were copied.
        value = dict(value)
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        frozen[str(key)] = _freeze_value(item)
    return MappingProxyType(frozen)


def _freeze_value(item: Any) -> Any:
    if isinstance(item, Mapping) and not isinstance(item, (str, bytes)):
        return freeze_mapping(item)
    if isinstance(item, list):
        return tuple(_freeze_value(v) for v in item)
    if isinstance(item, tuple):
        return tuple(_freeze_value(v) for v in item)
    return item


def mapping_to_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-copy a mapping to a plain mutable dict for serialization."""

    if not value:
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        out[str(key)] = _unfreeze_value(item)
    return out


def _unfreeze_value(item: Any) -> Any:
    if isinstance(item, Mapping) and not isinstance(item, (str, bytes)):
        return mapping_to_dict(item)
    if isinstance(item, tuple):
        return [_unfreeze_value(v) for v in item]
    if isinstance(item, list):
        return [_unfreeze_value(v) for v in item]
    return item
