"""Read, inspect, validate and losslessly patch the understood parts of GIL.

This is deliberately a raw-wire editor.  The format is only partially known;
unknown protobuf fields and all untouched encoded fields are retained byte for
byte.  The supported image operation targets an existing scene-level empty
model and its decoration records.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import struct
from typing import Any, Callable, Iterable

from .wire import (
    WireError,
    WireField,
    decode_packed_varints,
    encode_packed_varints,
    first_bytes,
    first_field,
    first_index,
    first_varint,
    fixed32_field,
    len_field,
    parse_fields,
    rebuild_message,
    set_bytes,
    set_fixed32,
    set_varint,
    unpack_fixed32,
    varint_field,
)


class GilError(ValueError):
    """Raised when a GIL operation cannot be performed safely."""


KNOWN_TOP_LEVEL_FIELDS: dict[int, str] = {
    2: "level_name",
    3: "unknown_3",
    4: "prefab_definitions",
    5: "scene_entities",
    6: "categories",
    7: "terrain",
    8: "preview_entities",
    9: "ui_control_group",
    10: "node_graph",
    11: "level_settings",
    18: "camera_templates",
    22: "level_flags",
    25: "peripheral_system",
    27: "decorations",
    29: "editor_info",
    36: "localized_text",
}


@dataclass(frozen=True)
class GilHeader:
    left_size: int
    schema: int
    head_tag: int
    file_type: int
    proto_size: int
    tail_tag: int


@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float

    def as_list(self) -> list[float]:
        return [self.x, self.y, self.z]


@dataclass(frozen=True)
class Transform:
    position: Vec3
    rotation: Vec3
    scale: Vec3


@dataclass(frozen=True)
class SceneObject:
    index: int
    object_id: int
    ref_id: int | None
    asset_id: int | None
    name: str
    transform: Transform
    decoration_ids: tuple[int, ...]


@dataclass(frozen=True)
class Decoration:
    index: int
    store_field: int
    decoration_id: int
    asset_id: int | None
    owner_id: int | None
    name: str
    transform: Transform
    color_enabled: bool
    argb: int | None
    rgb: int | None
    opacity_percent: float | None


@dataclass(frozen=True)
class DecorationSpec:
    name: str
    asset_id: int
    position: Vec3
    scale: Vec3
    rgba: tuple[int, int, int, int]
    rotation: Vec3 | None = None


@dataclass(frozen=True)
class DecorationGroupSpec:
    """One scene parent and the decorations stored in its local space."""

    position: Vec3
    decorations: tuple[DecorationSpec, ...]
    tile_row: int = 0
    tile_column: int = 0
    scale: Vec3 = Vec3(1.0, 1.0, 1.0)


@dataclass(frozen=True)
class MutationSummary:
    parent_id: int
    old_count: int
    new_count: int
    reused_ids: tuple[int, ...]
    allocated_ids: tuple[int, ...]
    removed_ids: tuple[int, ...]
    parent_ids: tuple[int, ...] = ()
    allocated_parent_ids: tuple[int, ...] = ()
    per_parent_counts: tuple[int, ...] = ()
    changed_top_fields: tuple[int, ...] = (5, 27)

    def as_dict(self, *, include_ids: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "parentId": self.parent_id,
            "oldDecorationCount": self.old_count,
            "newDecorationCount": self.new_count,
            "reusedIdCount": len(self.reused_ids),
            "allocatedIdCount": len(self.allocated_ids),
            "removedIdCount": len(self.removed_ids),
            "reusedIdSample": list(self.reused_ids[:5]),
            "allocatedIdSample": list(self.allocated_ids[:5]),
            "removedIdSample": list(self.removed_ids[:5]),
            "parentCount": len(self.parent_ids) or 1,
            "parentIds": list(self.parent_ids) or [self.parent_id],
            "allocatedParentIds": list(self.allocated_parent_ids),
            "perParentDecorationCounts": list(self.per_parent_counts)
            or [self.new_count],
            "changedTopFields": list(self.changed_top_fields),
        }
        if include_ids:
            result.update(
                {
                    "reusedIds": list(self.reused_ids),
                    "allocatedIds": list(self.allocated_ids),
                    "removedIds": list(self.removed_ids),
                }
            )
        return result


def _parse(data: bytes, context: str) -> list[WireField]:
    try:
        return parse_fields(data, context=context)
    except WireError as exc:
        raise GilError(str(exc)) from exc


def _require_finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise GilError(f"{label} must be finite")
    try:
        encoded_value = struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError as exc:
        raise GilError(f"{label} exceeds the finite float32 range used by GIL") from exc
    if not math.isfinite(encoded_value):
        raise GilError(f"{label} exceeds the finite float32 range used by GIL")


def _require_finite_vec3(value: Vec3, label: str) -> None:
    _require_finite(value.x, f"{label}.x")
    _require_finite(value.y, f"{label}.y")
    _require_finite(value.z, f"{label}.z")


def _validate_decoration_spec(spec: DecorationSpec, index: int) -> None:
    _require_finite_vec3(spec.position, f"decoration[{index}].position")
    _require_finite_vec3(spec.scale, f"decoration[{index}].scale")
    if spec.rotation is not None:
        _require_finite_vec3(spec.rotation, f"decoration[{index}].rotation")
    if spec.scale.x <= 0 or spec.scale.y <= 0 or spec.scale.z <= 0:
        raise GilError(f"decoration[{index}] scale components must be positive")
    if any(
        struct.unpack("<f", struct.pack("<f", component))[0] <= 0
        for component in spec.scale.as_list()
    ):
        raise GilError(
            f"decoration[{index}] scale is too small to remain positive in float32"
        )
    if spec.asset_id <= 0:
        raise GilError(f"decoration[{index}] asset ID must be positive")
    if len(spec.rgba) != 4 or any(
        not isinstance(channel, int) or not 0 <= channel <= 255
        for channel in spec.rgba
    ):
        raise GilError(f"decoration[{index}] RGBA must contain four integers in 0..255")


def _field_data(fields: list[WireField], number: int) -> bytes | None:
    return first_bytes(fields, number)


def _nested_bytes(data: bytes, path: Iterable[int]) -> bytes | None:
    current = data
    path_tuple = tuple(path)
    for number in path_tuple:
        fields = _parse(current, f"nested path {'.'.join(map(str, path_tuple))}")
        current = first_bytes(fields, number)
        if current is None:
            return None
    return current


def _component(
    entry_fields: list[WireField], container_number: int, component_type: int
) -> tuple[int, list[WireField]] | None:
    for index, field in enumerate(entry_fields):
        if field.number != container_number or field.wire_type != 2:
            continue
        component_fields = _parse(bytes(field.value), f"component {component_type}")
        if first_varint(component_fields, 1) == component_type:
            return index, component_fields
    return None


def _read_component_string(
    entry_fields: list[WireField], container_number: int, component_type: int
) -> str:
    found = _component(entry_fields, container_number, component_type)
    if found is None:
        return ""
    _, component_fields = found
    block = first_bytes(component_fields, 11)
    if block is None:
        return ""
    value = first_bytes(_parse(block, "name block"), 1)
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace")


def _read_vec3(data: bytes | None) -> Vec3:
    if data is None:
        return Vec3(0.0, 0.0, 0.0)
    fields = _parse(data, "vec3")
    values: list[float] = []
    for number in (1, 2, 3):
        value = unpack_fixed32(first_field(fields, number, 5))
        values.append(0.0 if value is None else float(value))
    return Vec3(*values)


def _read_transform(
    entry_fields: list[WireField], container_number: int
) -> Transform:
    found = _component(entry_fields, container_number, 1)
    if found is None:
        zero = Vec3(0.0, 0.0, 0.0)
        return Transform(zero, zero, zero)
    _, component_fields = found
    block = first_bytes(component_fields, 11)
    if block is None:
        zero = Vec3(0.0, 0.0, 0.0)
        return Transform(zero, zero, zero)
    transform_fields = _parse(block, "transform block")
    return Transform(
        _read_vec3(first_bytes(transform_fields, 1)),
        _read_vec3(first_bytes(transform_fields, 2)),
        _read_vec3(first_bytes(transform_fields, 3)),
    )


def _read_owner(entry_fields: list[WireField]) -> int | None:
    found = _component(entry_fields, 4, 40)
    if found is None:
        return None
    _, component_fields = found
    owner_payload = first_bytes(component_fields, 50)
    if owner_payload is None:
        return None
    return first_varint(_parse(owner_payload, "owner payload"), 502)


def _read_color(
    entry_fields: list[WireField], container_number: int
) -> tuple[bool, int | None, int | None, float | None]:
    found = _component(entry_fields, container_number, 22)
    if found is None:
        return False, None, None, None
    _, component_fields = found
    payload = first_bytes(component_fields, 32)
    if payload is None:
        return False, None, None, None
    color_fields = _parse(payload, "model color payload")
    opacity = unpack_fixed32(first_field(color_fields, 4, 5))
    return (
        first_varint(color_fields, 1) not in (None, 0),
        first_varint(color_fields, 3),
        first_varint(color_fields, 5),
        opacity,
    )


def _read_decoration_refs(entry_fields: list[WireField]) -> tuple[int, ...]:
    found = _component(entry_fields, 5, 40)
    if found is None:
        return ()
    _, component_fields = found
    payload = first_bytes(component_fields, 50)
    if payload is None:
        return ()
    packed = first_bytes(_parse(payload, "decoration reference payload"), 501)
    if packed is None:
        return ()
    try:
        return tuple(decode_packed_varints(packed))
    except WireError as exc:
        raise GilError(f"invalid packed decoration reference list: {exc}") from exc


def _set_vec3_message(original: bytes | None, value: Vec3) -> bytes:
    fields = [] if original is None else _parse(original, "vec3 patch")
    set_fixed32(fields, 1, value.x)
    set_fixed32(fields, 2, value.y)
    set_fixed32(fields, 3, value.z)
    return rebuild_message(fields)


def _patch_transform(
    entry_fields: list[WireField],
    position: Vec3,
    scale: Vec3,
    rotation: Vec3 | None = None,
) -> None:
    found = _component(entry_fields, 5, 1)
    if found is None:
        transform_fields = [
            len_field(1, _set_vec3_message(None, position)),
            len_field(
                2,
                b"" if rotation is None else _set_vec3_message(None, rotation),
            ),
            len_field(3, _set_vec3_message(None, scale)),
        ]
        component_fields = [
            varint_field(1, 1),
            len_field(11, rebuild_message(transform_fields)),
        ]
        entry_fields.append(len_field(5, rebuild_message(component_fields)))
        return

    entry_index, component_fields = found
    block = first_bytes(component_fields, 11)
    transform_fields = [] if block is None else _parse(block, "transform patch")
    set_bytes(
        transform_fields,
        1,
        _set_vec3_message(first_bytes(transform_fields, 1), position),
    )
    if rotation is not None:
        set_bytes(
            transform_fields,
            2,
            _set_vec3_message(first_bytes(transform_fields, 2), rotation),
        )
    set_bytes(
        transform_fields,
        3,
        _set_vec3_message(first_bytes(transform_fields, 3), scale),
    )
    set_bytes(component_fields, 11, rebuild_message(transform_fields))
    entry_fields[entry_index] = entry_fields[entry_index].with_value(
        rebuild_message(component_fields)
    )


def _patch_name(entry_fields: list[WireField], name: str) -> None:
    encoded = name.encode("utf-8")
    found = _component(entry_fields, 4, 1)
    if found is None:
        block = rebuild_message([len_field(1, encoded)])
        component = rebuild_message([varint_field(1, 1), len_field(11, block)])
        entry_fields.append(len_field(4, component))
        return
    entry_index, component_fields = found
    block = first_bytes(component_fields, 11)
    block_fields = [] if block is None else _parse(block, "name patch")
    set_bytes(block_fields, 1, encoded)
    set_bytes(component_fields, 11, rebuild_message(block_fields))
    entry_fields[entry_index] = entry_fields[entry_index].with_value(
        rebuild_message(component_fields)
    )


def _patch_owner(entry_fields: list[WireField], owner_id: int) -> None:
    found = _component(entry_fields, 4, 40)
    if found is None:
        payload = rebuild_message([varint_field(502, owner_id)])
        component = rebuild_message([varint_field(1, 40), len_field(50, payload)])
        entry_fields.append(len_field(4, component))
        return
    entry_index, component_fields = found
    payload = first_bytes(component_fields, 50)
    payload_fields = [] if payload is None else _parse(payload, "owner patch")
    set_varint(payload_fields, 502, owner_id)
    set_bytes(component_fields, 50, rebuild_message(payload_fields))
    entry_fields[entry_index] = entry_fields[entry_index].with_value(
        rebuild_message(component_fields)
    )


def _patch_color(
    entry_fields: list[WireField], rgba: tuple[int, int, int, int]
) -> None:
    r, g, b, a = rgba
    for channel in rgba:
        if not 0 <= channel <= 255:
            raise GilError("RGBA channels must be in the range 0..255")
    argb = (a << 24) | (r << 16) | (g << 8) | b
    rgb = (r << 16) | (g << 8) | b

    found = _component(entry_fields, 5, 22)
    if found is None:
        color_fields = [
            varint_field(1, 1),
            varint_field(3, argb),
            fixed32_field(4, a * 100.0 / 255.0),
            varint_field(5, rgb),
            varint_field(6, 6700),
        ]
        component = rebuild_message(
            [varint_field(1, 22), len_field(32, rebuild_message(color_fields))]
        )
        entry_fields.append(len_field(5, component))
        return

    entry_index, component_fields = found
    payload = first_bytes(component_fields, 32)
    color_fields = [] if payload is None else _parse(payload, "color patch")
    set_varint(color_fields, 1, 1)
    set_varint(color_fields, 3, argb)
    set_fixed32(color_fields, 4, a * 100.0 / 255.0)
    set_varint(color_fields, 5, rgb)
    set_bytes(component_fields, 32, rebuild_message(color_fields))
    entry_fields[entry_index] = entry_fields[entry_index].with_value(
        rebuild_message(component_fields)
    )


def _patch_decoration_entry(
    template: bytes,
    decoration_id: int,
    parent_id: int,
    spec: DecorationSpec,
) -> bytes:
    fields = _parse(template, "decoration entry patch")
    set_varint(fields, 1, decoration_id)
    set_varint(fields, 2, spec.asset_id)
    _patch_name(fields, spec.name)
    _patch_owner(fields, parent_id)
    _patch_transform(fields, spec.position, spec.scale, spec.rotation)
    _patch_color(fields, spec.rgba)
    return rebuild_message(fields)


def _patch_parent_refs(entry: bytes, decoration_ids: Iterable[int]) -> bytes:
    entry_fields = _parse(entry, "parent decoration refs patch")
    found = _component(entry_fields, 5, 40)
    if found is None:
        raise GilError("target parent has no type-40 decoration reference component")
    entry_index, component_fields = found
    payload = first_bytes(component_fields, 50)
    if payload is None:
        raise GilError("target parent type-40 component has no field 50 payload")
    payload_fields = _parse(payload, "parent decoration refs payload patch")
    if first_index(payload_fields, 501, 2) is None:
        raise GilError("target parent has no field 501 packed decoration ID list")
    set_bytes(payload_fields, 501, encode_packed_varints(decoration_ids))
    set_bytes(component_fields, 50, rebuild_message(payload_fields))
    entry_fields[entry_index] = entry_fields[entry_index].with_value(
        rebuild_message(component_fields)
    )
    return rebuild_message(entry_fields)


def _patch_scene_parent(
    entry: bytes,
    *,
    object_id: int,
    position: Vec3,
    scale: Vec3,
    decoration_ids: Iterable[int],
) -> bytes:
    _require_finite_vec3(position, "parent position")
    _require_finite_vec3(scale, "parent scale")
    entry_fields = _parse(entry, "scene parent patch")
    set_varint(entry_fields, 1, object_id)
    found = _component(entry_fields, 6, 1)
    if found is None:
        raise GilError("target parent has no scene transform component")
    entry_index, component_fields = found
    block = first_bytes(component_fields, 11)
    if block is None:
        raise GilError("target parent scene transform has no field 11 payload")
    transform_fields = _parse(block, "scene parent transform patch")
    set_bytes(
        transform_fields,
        1,
        _set_vec3_message(first_bytes(transform_fields, 1), position),
    )
    set_bytes(
        transform_fields,
        3,
        _set_vec3_message(first_bytes(transform_fields, 3), scale),
    )
    set_bytes(component_fields, 11, rebuild_message(transform_fields))
    entry_fields[entry_index] = entry_fields[entry_index].with_value(
        rebuild_message(component_fields)
    )
    return _patch_parent_refs(rebuild_message(entry_fields), decoration_ids)


def _append_top6_scene_mappings(top6: bytes, object_ids: Iterable[int]) -> bytes:
    fields = _parse(top6, "top-level field 6 scene mapping patch")
    target_index: int | None = None
    target_entry_fields: list[WireField] | None = None
    child_index: int | None = None
    child_fields: list[WireField] | None = None
    for index, field in enumerate(fields):
        if field.number != 1 or field.wire_type != 2:
            continue
        entry_fields = _parse(bytes(field.value), "top6 category entry")
        if first_varint(entry_fields, 1) != 3:
            continue
        for nested_index, nested in enumerate(entry_fields):
            if nested.number != 3 or nested.wire_type != 2:
                continue
            target_index = index
            target_entry_fields = entry_fields
            child_index = nested_index
            child_fields = _parse(bytes(nested.value), "top6 category 3 child")
            break
        if target_index is not None:
            break
    if (
        target_index is None
        or target_entry_fields is None
        or child_index is None
        or child_fields is None
    ):
        raise GilError("category ID 3 / child field 3 was not found in top-level field 6")

    existing_targets: set[int] = set()
    for field in child_fields:
        if field.number != 5 or field.wire_type != 2:
            continue
        mapping_fields = _parse(bytes(field.value), "top6 scene mapping")
        if first_varint(mapping_fields, 1) == 200:
            target = first_varint(mapping_fields, 2)
            if target is not None:
                existing_targets.add(target)
    for object_id in object_ids:
        if object_id in existing_targets:
            raise GilError(f"top6 already contains a scene mapping for new ID {object_id}")
        mapping = rebuild_message(
            [varint_field(1, 200), varint_field(2, object_id)]
        )
        child_fields.append(len_field(5, mapping))
        existing_targets.add(object_id)

    target_entry_fields[child_index] = target_entry_fields[child_index].with_value(
        rebuild_message(child_fields)
    )
    fields[target_index] = fields[target_index].with_value(
        rebuild_message(target_entry_fields)
    )
    return rebuild_message(fields)


def _top6_scene_mapping_targets(top6: bytes) -> tuple[int, ...]:
    targets: list[int] = []
    for field in _parse(top6, "top-level field 6 scene mapping read"):
        if field.number != 1 or field.wire_type != 2:
            continue
        entry_fields = _parse(bytes(field.value), "top6 category mapping entry")
        if first_varint(entry_fields, 1) != 3:
            continue
        for child in entry_fields:
            if child.number != 3 or child.wire_type != 2:
                continue
            for mapping in _parse(bytes(child.value), "top6 category mapping child"):
                if mapping.number != 5 or mapping.wire_type != 2:
                    continue
                mapping_fields = _parse(bytes(mapping.value), "top6 scene mapping record")
                if first_varint(mapping_fields, 1) != 200:
                    continue
                target = first_varint(mapping_fields, 2)
                if target is not None:
                    targets.append(target)
    return tuple(targets)


class GilDocument:
    """A GIL envelope and its raw protobuf payload."""

    def __init__(
        self,
        header: GilHeader,
        payload: bytes,
        *,
        source_path: Path | None = None,
        source_bytes: bytes | None = None,
    ) -> None:
        self.header = header
        self.payload = payload
        self.source_path = source_path
        self._source_bytes = source_bytes

    @classmethod
    def from_bytes(cls, data: bytes, *, source_path: Path | None = None) -> "GilDocument":
        if len(data) < 24:
            raise GilError("file is smaller than the 24-byte GIL envelope")
        left_size, schema, head_tag, file_type, proto_size = struct.unpack(
            ">IIIII", data[:20]
        )
        tail_tag = struct.unpack(">I", data[-4:])[0]
        return cls(
            GilHeader(left_size, schema, head_tag, file_type, proto_size, tail_tag),
            data[20:-4],
            source_path=source_path,
            source_bytes=data,
        )

    @classmethod
    def load(cls, path: str | Path) -> "GilDocument":
        resolved = Path(path)
        return cls.from_bytes(resolved.read_bytes(), source_path=resolved)

    def build_bytes(self) -> bytes:
        prefix = struct.pack(
            ">IIIII",
            len(self.payload) + 20,
            self.header.schema,
            self.header.head_tag,
            self.header.file_type,
            len(self.payload),
        )
        return prefix + self.payload + struct.pack(">I", self.header.tail_tag)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.build_bytes()).hexdigest()

    def top_fields(self) -> list[WireField]:
        return _parse(self.payload, "GIL top-level payload")

    def top_data(self, number: int) -> bytes | None:
        return first_bytes(self.top_fields(), number)

    def _replace_top_data(self, number: int, value: bytes) -> "GilDocument":
        fields = self.top_fields()
        index = first_index(fields, number, 2)
        if index is None:
            raise GilError(f"required top-level field {number} was not found")
        fields[index] = fields[index].with_value(value)
        payload = rebuild_message(fields)
        header = GilHeader(
            len(payload) + 20,
            self.header.schema,
            self.header.head_tag,
            self.header.file_type,
            len(payload),
            self.header.tail_tag,
        )
        return GilDocument(header, payload, source_path=self.source_path)

    def scene_objects(self) -> list[SceneObject]:
        top5 = self.top_data(5)
        if top5 is None:
            return []
        objects: list[SceneObject] = []
        for index, field in enumerate(_parse(top5, "top-level field 5")):
            if field.number != 1 or field.wire_type != 2:
                continue
            fields = _parse(bytes(field.value), "scene entity")
            object_id = first_varint(fields, 1)
            if object_id is None:
                continue
            ref_data = first_bytes(fields, 2)
            ref_id = None
            if ref_data is not None:
                ref_id = first_varint(_parse(ref_data, "scene entity ref"), 1)
            objects.append(
                SceneObject(
                    index=index,
                    object_id=object_id,
                    ref_id=ref_id,
                    asset_id=first_varint(fields, 8),
                    name=_read_component_string(fields, 5, 1),
                    transform=_read_transform(fields, 6),
                    decoration_ids=_read_decoration_refs(fields),
                )
            )
        return objects

    def decorations(self) -> list[Decoration]:
        top27 = self.top_data(27)
        if top27 is None:
            return []
        decorations: list[Decoration] = []
        for index, field in enumerate(_parse(top27, "top-level field 27")):
            if field.number not in (1, 2) or field.wire_type != 2:
                continue
            fields = _parse(bytes(field.value), "decoration record")
            decoration_id = first_varint(fields, 1)
            if decoration_id is None:
                continue
            enabled, argb, rgb, opacity = _read_color(fields, 5)
            decorations.append(
                Decoration(
                    index=index,
                    store_field=field.number,
                    decoration_id=decoration_id,
                    asset_id=first_varint(fields, 2),
                    owner_id=_read_owner(fields),
                    name=_read_component_string(fields, 4, 1),
                    transform=_read_transform(fields, 5),
                    color_enabled=enabled,
                    argb=argb,
                    rgb=rgb,
                    opacity_percent=opacity,
                )
            )
        return decorations

    def scene_category_mapping_targets(self) -> tuple[int, ...]:
        top6 = self.top_data(6)
        return () if top6 is None else _top6_scene_mapping_targets(top6)

    def _object_ids_across_spaces(self) -> set[int]:
        result: set[int] = set()
        for top_number in (4, 5, 8):
            top_data = self.top_data(top_number)
            if top_data is None:
                continue
            for field in _parse(top_data, f"top-level field {top_number} ID scan"):
                if field.number != 1 or field.wire_type != 2:
                    continue
                object_id = first_varint(
                    _parse(bytes(field.value), f"top-level field {top_number} entry"),
                    1,
                )
                if object_id is not None:
                    result.add(object_id)
        return result

    def choose_decoration_parent(self, parent_id: int | None = None) -> SceneObject:
        objects = self.scene_objects()
        if parent_id is not None:
            matches = [obj for obj in objects if obj.object_id == parent_id]
            if not matches:
                raise GilError(f"scene parent ID {parent_id} was not found")
            if len(matches) != 1:
                raise GilError(f"scene parent ID {parent_id} is not unique")
            parent = matches[0]
            if not parent.decoration_ids:
                raise GilError(f"scene parent ID {parent_id} has no decoration references")
            return parent

        candidates = [
            obj
            for obj in objects
            if obj.asset_id == 10005018 and obj.decoration_ids
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            candidates = [obj for obj in objects if obj.decoration_ids]
        if len(candidates) != 1:
            ids = [obj.object_id for obj in candidates]
            raise GilError(
                "cannot choose a unique decoration parent; pass --parent-id "
                f"(candidates: {ids})"
            )
        return candidates[0]

    def linked_decorations(self, parent: SceneObject) -> list[Decoration]:
        if len(parent.decoration_ids) != len(set(parent.decoration_ids)):
            duplicates = sorted(
                item
                for item, count in Counter(parent.decoration_ids).items()
                if count > 1
            )
            raise GilError(
                f"parent {parent.object_id} packed decoration list contains duplicate IDs: "
                f"{duplicates[:10]}"
            )
        decorations = self.decorations()
        by_id = {item.decoration_id: item for item in decorations}
        linked: list[Decoration] = []
        missing: list[int] = []
        wrong_owner: list[int] = []
        wrong_store: list[int] = []
        for decoration_id in parent.decoration_ids:
            item = by_id.get(decoration_id)
            if item is None:
                missing.append(decoration_id)
                continue
            if item.owner_id != parent.object_id:
                wrong_owner.append(decoration_id)
            if item.store_field != 2:
                wrong_store.append(decoration_id)
            linked.append(item)
        if missing:
            raise GilError(f"parent references missing decoration IDs: {missing[:10]}")
        if wrong_owner:
            raise GilError(f"decoration owner mismatch for IDs: {wrong_owner[:10]}")
        if wrong_store:
            raise GilError(
                "image replacement currently requires scene decorations (top27 field2); "
                f"other records: {wrong_store[:10]}"
            )
        owned_not_referenced = [
            item.decoration_id
            for item in decorations
            if item.store_field == 2
            and item.owner_id == parent.object_id
            and item.decoration_id not in set(parent.decoration_ids)
        ]
        if owned_not_referenced:
            raise GilError(
                "parent owns scene decorations that are absent from its packed reference list: "
                f"{owned_not_referenced[:10]}"
            )
        return linked

    def replace_parent_decorations(
        self,
        specs: list[DecorationSpec],
        *,
        parent_id: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple["GilDocument", MutationSummary]:
        parent = self.choose_decoration_parent(parent_id)
        return self.replace_parent_decoration_groups(
            [
                DecorationGroupSpec(
                    position=parent.transform.position,
                    decorations=tuple(specs),
                    scale=parent.transform.scale,
                )
            ],
            parent_id=parent.object_id,
            progress_callback=progress_callback,
            _max_per_parent=None,
        )

    def replace_parent_decoration_groups(
        self,
        groups: list[DecorationGroupSpec],
        *,
        parent_id: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        _max_per_parent: int | None = 100,
    ) -> tuple["GilDocument", MutationSummary]:
        if not groups:
            raise GilError("image produced no non-empty decoration tiles")
        for group_index, group in enumerate(groups):
            _require_finite_vec3(group.position, f"parent group[{group_index}].position")
            _require_finite_vec3(group.scale, f"parent group[{group_index}].scale")
            if any(value <= 0 for value in group.scale.as_list()):
                raise GilError(
                    f"parent group[{group_index}] scale components must be positive"
                )
            if any(
                struct.unpack("<f", struct.pack("<f", value))[0] <= 0
                for value in group.scale.as_list()
            ):
                raise GilError(
                    f"parent group[{group_index}] scale is too small to remain "
                    "positive in float32"
                )
            if not group.decorations:
                raise GilError(f"parent group[{group_index}] has no decorations")
            if _max_per_parent is not None and len(group.decorations) > _max_per_parent:
                raise GilError(
                    f"parent group[{group_index}] needs {len(group.decorations)} decorations; "
                    f"the safe per-parent limit is {_max_per_parent}"
                )
            for decoration_index, spec in enumerate(group.decorations):
                _validate_decoration_spec(
                    spec,
                    sum(len(previous.decorations) for previous in groups[:group_index])
                    + decoration_index,
                )

        parent = self.choose_decoration_parent(parent_id)
        _require_finite_vec3(parent.transform.position, "target parent position")
        _require_finite_vec3(parent.transform.rotation, "target parent rotation")
        _require_finite_vec3(parent.transform.scale, "target parent scale")
        if any(
            abs(value) > 1e-6
            for value in parent.transform.rotation.as_list()
        ):
            raise GilError(
                "target parent rotation must be (0, 0, 0) for tiled image placement"
            )
        linked = self.linked_decorations(parent)
        if not linked:
            raise GilError("target parent has no reusable decoration template")

        top27 = self.top_data(27)
        top5 = self.top_data(5)
        top6 = self.top_data(6)
        if top27 is None or top5 is None or top6 is None:
            raise GilError("required top-level fields 5, 6 and 27 were not found")
        top27_fields = _parse(top27, "top-level field 27 mutation")
        top5_fields = _parse(top5, "top-level field 5 mutation")

        entry_data_by_id: dict[int, bytes] = {}
        all_ids: set[int] = set()
        for field in top27_fields:
            if field.number not in (1, 2) or field.wire_type != 2:
                continue
            entry = bytes(field.value)
            decoration_id = first_varint(_parse(entry, "top27 ID scan"), 1)
            if decoration_id is not None:
                if decoration_id in all_ids:
                    raise GilError(f"duplicate top27 decoration ID {decoration_id}")
                all_ids.add(decoration_id)
                entry_data_by_id[decoration_id] = entry

        old_ids = [item.decoration_id for item in linked]
        if len(old_ids) != len(set(old_ids)):
            raise GilError("target parent's existing decoration IDs are not unique")
        flat_specs = [spec for group in groups for spec in group.decorations]
        reused_count = min(len(old_ids), len(flat_specs))
        reused_ids = old_ids[:reused_count]
        removed_ids = old_ids[reused_count:]
        next_id = max(all_ids, default=1073741825) + 1
        allocated_ids: list[int] = []
        while len(reused_ids) + len(allocated_ids) < len(flat_specs):
            while next_id in all_ids:
                next_id += 1
            allocated_ids.append(next_id)
            all_ids.add(next_id)
            next_id += 1
        new_ids = reused_ids + allocated_ids
        if len(new_ids) != len(set(new_ids)) or len(new_ids) != len(flat_specs):
            raise GilError("failed to allocate a unique decoration ID for every spec")

        # GIL object IDs are split into semantic bands.  In this fixture the
        # user-created empty model lives in 0x404xxxxx, while the numerically
        # largest object (0x41400001) is the special level entity.  Taking the
        # global maximum would therefore clone empty models into the level
        # entity band; the editor silently ignores those clones.  Allocate in
        # the source parent's 20-bit band and still avoid every known object,
        # decoration, and category-mapping target.
        object_id_band_size = 1 << 20
        parent_band_start = (
            parent.object_id // object_id_band_size
        ) * object_id_band_size
        parent_band_end = parent_band_start + object_id_band_size
        used_object_ids = self._object_ids_across_spaces()
        used_object_ids.update(all_ids)
        used_object_ids.update(self.scene_category_mapping_targets())
        used_ids_in_parent_band = [
            object_id
            for object_id in used_object_ids
            if parent_band_start <= object_id < parent_band_end
        ]
        next_object_id = max(
            [parent.object_id, *used_ids_in_parent_band]
        ) + 1
        allocated_parent_ids: list[int] = []
        for _ in groups[1:]:
            while next_object_id in used_object_ids:
                next_object_id += 1
            if next_object_id >= parent_band_end:
                raise GilError(
                    "cannot allocate another scene parent ID in source parent "
                    f"band 0x{parent_band_start:X}..0x{parent_band_end - 1:X}"
                )
            allocated_parent_ids.append(next_object_id)
            used_object_ids.add(next_object_id)
            next_object_id += 1
        parent_ids = [parent.object_id, *allocated_parent_ids]
        if len(parent_ids) != len(set(parent_ids)):
            raise GilError("failed to allocate unique scene parent IDs")

        ids_by_parent: list[list[int]] = []
        cursor = 0
        for group in groups:
            next_cursor = cursor + len(group.decorations)
            ids_by_parent.append(new_ids[cursor:next_cursor])
            cursor = next_cursor

        template = entry_data_by_id[old_ids[0]]
        patched_by_id: dict[int, bytes] = {}
        flat_index = 0
        if progress_callback is not None:
            progress_callback(0, len(flat_specs))
        for group_index, group in enumerate(groups):
            owner_id = parent_ids[group_index]
            for spec in group.decorations:
                new_id = new_ids[flat_index]
                source = entry_data_by_id.get(new_id, template)
                patched_by_id[new_id] = _patch_decoration_entry(
                    source, new_id, owner_id, spec
                )
                flat_index += 1
                if (
                    progress_callback is not None
                    and flat_index < len(flat_specs)
                ):
                    progress_callback(flat_index, len(flat_specs))

        old_id_set = set(old_ids)
        retained_fields: list[WireField] = []
        insertion_index = 0
        for field in top27_fields:
            if field.number == 2 and field.wire_type == 2:
                entry_fields = _parse(bytes(field.value), "top27 linked record mutation")
                decoration_id = first_varint(entry_fields, 1)
                if decoration_id in old_id_set:
                    if decoration_id in patched_by_id:
                        retained_fields.append(
                            field.with_value(patched_by_id.pop(decoration_id))
                        )
                        insertion_index = len(retained_fields)
                    continue
            retained_fields.append(field)
            if field.number == 2:
                insertion_index = len(retained_fields)

        new_fields = [len_field(2, patched_by_id[item_id]) for item_id in allocated_ids]
        retained_fields[insertion_index:insertion_index] = new_fields
        next_top27 = rebuild_message(retained_fields)

        parent_entry: bytes | None = None
        for index, field in enumerate(top5_fields):
            if field.number != 1 or field.wire_type != 2:
                continue
            entry = bytes(field.value)
            entry_fields = _parse(entry, "top5 parent lookup")
            if first_varint(entry_fields, 1) != parent.object_id:
                continue
            parent_entry = entry
            top5_fields[index] = field.with_value(
                _patch_scene_parent(
                    entry,
                    object_id=parent.object_id,
                    position=groups[0].position,
                    scale=groups[0].scale,
                    decoration_ids=ids_by_parent[0],
                )
            )
            break
        if parent_entry is None:
            raise GilError("target parent disappeared while rebuilding top-level field 5")
        for group_index, object_id in enumerate(allocated_parent_ids, start=1):
            top5_fields.append(
                len_field(
                    1,
                    _patch_scene_parent(
                        parent_entry,
                        object_id=object_id,
                        position=groups[group_index].position,
                        scale=groups[group_index].scale,
                        decoration_ids=ids_by_parent[group_index],
                    ),
                )
            )

        next_document = self._replace_top_data(5, rebuild_message(top5_fields))
        if allocated_parent_ids:
            next_document = next_document._replace_top_data(
                6, _append_top6_scene_mappings(top6, allocated_parent_ids)
            )
        next_document = next_document._replace_top_data(27, next_top27)
        report = next_document.validate()
        if not report["ok"]:
            raise GilError(
                "generated GIL failed structural validation: "
                + "; ".join(report["errors"])
            )

        generated_objects = {
            item.object_id: item
            for item in next_document.scene_objects()
            if item.object_id in set(parent_ids)
        }
        if len(generated_objects) != len(groups):
            raise GilError("generated parent count does not match tile count")

        # Parse the generated decoration store only once.  Calling
        # linked_decorations() for every generated parent reparses all of
        # top-level field 27 on every call, turning the final verification into
        # O(parent count * decoration count) work for tiled images.
        generated_decorations = next_document.decorations()
        generated_decorations_by_id = {
            item.decoration_id: item for item in generated_decorations
        }
        owned_scene_ids_by_parent: dict[int, list[int]] = {}
        for item in generated_decorations:
            if item.store_field == 2 and item.owner_id is not None:
                owned_scene_ids_by_parent.setdefault(item.owner_id, []).append(
                    item.decoration_id
                )

        generated_ref_ids: list[int] = []
        for group_index, object_id in enumerate(parent_ids):
            generated_parent = generated_objects[object_id]
            expected_count = len(groups[group_index].decorations)
            expected_scale = groups[group_index].scale
            if any(
                not math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6)
                for actual, expected in zip(
                    generated_parent.transform.scale.as_list(),
                    expected_scale.as_list(),
                )
            ):
                raise GilError(
                    f"generated parent {object_id} scale does not match its group"
                )
            if len(generated_parent.decoration_ids) != expected_count:
                raise GilError(
                    f"generated parent {object_id} reference count does not match its tile"
                )
            if len(set(generated_parent.decoration_ids)) != expected_count:
                raise GilError(f"generated parent {object_id} contains duplicate references")

            missing: list[int] = []
            wrong_owner: list[int] = []
            wrong_store: list[int] = []
            for decoration_id in generated_parent.decoration_ids:
                item = generated_decorations_by_id.get(decoration_id)
                if item is None:
                    missing.append(decoration_id)
                    continue
                if item.owner_id != generated_parent.object_id:
                    wrong_owner.append(decoration_id)
                if item.store_field != 2:
                    wrong_store.append(decoration_id)
            if missing:
                raise GilError(
                    f"parent references missing decoration IDs: {missing[:10]}"
                )
            if wrong_owner:
                raise GilError(
                    f"decoration owner mismatch for IDs: {wrong_owner[:10]}"
                )
            if wrong_store:
                raise GilError(
                    "image replacement currently requires scene decorations "
                    "(top27 field2); "
                    f"other records: {wrong_store[:10]}"
                )

            referenced_ids = set(generated_parent.decoration_ids)
            owned_not_referenced = [
                decoration_id
                for decoration_id in owned_scene_ids_by_parent.get(object_id, ())
                if decoration_id not in referenced_ids
            ]
            if owned_not_referenced:
                raise GilError(
                    "parent owns scene decorations that are absent from its packed "
                    f"reference list: {owned_not_referenced[:10]}"
                )
            generated_ref_ids.extend(generated_parent.decoration_ids)
        if len(generated_ref_ids) != len(set(generated_ref_ids)):
            raise GilError("generated parents share duplicate decoration references")
        if len(generated_ref_ids) != len(flat_specs):
            raise GilError("generated reference count does not match decoration spec count")

        summary = MutationSummary(
            parent_id=parent.object_id,
            old_count=len(old_ids),
            new_count=len(new_ids),
            reused_ids=tuple(reused_ids),
            allocated_ids=tuple(allocated_ids),
            removed_ids=tuple(removed_ids),
            parent_ids=tuple(parent_ids),
            allocated_parent_ids=tuple(allocated_parent_ids),
            per_parent_counts=tuple(len(group.decorations) for group in groups),
            changed_top_fields=(5, 6, 27) if allocated_parent_ids else (5, 27),
        )
        if progress_callback is not None:
            progress_callback(len(flat_specs), len(flat_specs))
        return next_document, summary

    def validate(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        actual_payload_size = len(self.payload)
        if self.header.left_size != actual_payload_size + 20:
            errors.append(
                "header.leftSize does not equal protobuf payload size + 20"
            )
        if self.header.proto_size != actual_payload_size:
            errors.append("header.protoSize does not equal protobuf payload size")
        try:
            self.top_fields()
        except GilError as exc:
            errors.append(str(exc))
            return {"ok": False, "errors": errors, "warnings": warnings}

        try:
            objects = self.scene_objects()
            decorations = self.decorations()
            scene_mapping_targets = self.scene_category_mapping_targets()
        except GilError as exc:
            errors.append(str(exc))
            return {"ok": False, "errors": errors, "warnings": warnings}

        object_ids = [item.object_id for item in objects]
        duplicate_objects = sorted(
            item for item, count in Counter(object_ids).items() if count > 1
        )
        if duplicate_objects:
            errors.append(f"duplicate scene object IDs: {duplicate_objects[:10]}")

        duplicate_mappings = sorted(
            item
            for item, count in Counter(scene_mapping_targets).items()
            if count > 1
        )
        if duplicate_mappings:
            errors.append(
                f"duplicate top6 category-3 scene mappings: {duplicate_mappings[:10]}"
            )

        decoration_ids = [item.decoration_id for item in decorations]
        duplicate_decorations = sorted(
            item for item, count in Counter(decoration_ids).items() if count > 1
        )
        if duplicate_decorations:
            errors.append(
                f"duplicate top27 decoration IDs: {duplicate_decorations[:10]}"
            )

        object_id_set = set(object_ids)
        mapping_target_set = set(scene_mapping_targets)
        missing_mapping_objects = sorted(
            obj.object_id
            for obj in objects
            if obj.decoration_ids and obj.object_id not in mapping_target_set
        )
        if missing_mapping_objects:
            errors.append(
                "scene objects with decorations are missing category-3 mappings: "
                f"{missing_mapping_objects[:10]}"
            )
        missing_mapping_targets = sorted(mapping_target_set - object_id_set)
        if missing_mapping_targets:
            errors.append(
                f"top6 scene mappings target missing objects: {missing_mapping_targets[:10]}"
            )

        by_decoration_id = {item.decoration_id: item for item in decorations}
        referenced_ids: set[int] = set()
        for obj in objects:
            transform_values = (
                *obj.transform.position.as_list(),
                *obj.transform.rotation.as_list(),
                *obj.transform.scale.as_list(),
            )
            if any(not math.isfinite(value) for value in transform_values):
                errors.append(f"scene object {obj.object_id} has a non-finite transform")
            duplicate_refs = sorted(
                item
                for item, count in Counter(obj.decoration_ids).items()
                if count > 1
            )
            if duplicate_refs:
                errors.append(
                    f"scene object {obj.object_id} has duplicate packed decoration IDs: "
                    f"{duplicate_refs[:10]}"
                )
            for decoration_id in obj.decoration_ids:
                referenced_ids.add(decoration_id)
                decoration = by_decoration_id.get(decoration_id)
                if decoration is None:
                    errors.append(
                        f"scene object {obj.object_id} references missing decoration {decoration_id}"
                    )
                elif decoration.owner_id != obj.object_id:
                    errors.append(
                        f"decoration {decoration_id} owner {decoration.owner_id} does not match "
                        f"referencing object {obj.object_id}"
                    )
        for decoration in decorations:
            transform_values = (
                *decoration.transform.position.as_list(),
                *decoration.transform.rotation.as_list(),
                *decoration.transform.scale.as_list(),
            )
            if any(not math.isfinite(value) for value in transform_values):
                errors.append(
                    f"decoration {decoration.decoration_id} has a non-finite transform"
                )
            if (
                decoration.opacity_percent is not None
                and not math.isfinite(decoration.opacity_percent)
            ):
                errors.append(
                    f"decoration {decoration.decoration_id} has non-finite opacity"
                )
            if (
                decoration.store_field == 2
                and decoration.owner_id is not None
                and decoration.owner_id not in object_id_set
            ):
                errors.append(
                    f"scene decoration {decoration.decoration_id} has missing owner "
                    f"{decoration.owner_id}"
                )
            if decoration.store_field == 2 and decoration.decoration_id not in referenced_ids:
                warnings.append(
                    f"scene decoration {decoration.decoration_id} is not referenced by a scene object"
                )
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    def inspect(self, *, verbose: bool = False) -> dict[str, Any]:
        top_fields = self.top_fields()
        top_counter = Counter(field.number for field in top_fields)
        top_summary = []
        for number in sorted(top_counter):
            matching = [field for field in top_fields if field.number == number]
            top_summary.append(
                {
                    "number": number,
                    "name": KNOWN_TOP_LEVEL_FIELDS.get(number, "unknown"),
                    "occurrences": len(matching),
                    "wireTypes": sorted({field.wire_type for field in matching}),
                    "encodedBytes": sum(len(field.encoded()) for field in matching),
                }
            )

        objects = self.scene_objects()
        decorations = self.decorations()
        scene_mapping_targets = self.scene_category_mapping_targets()
        by_id = {item.decoration_id: item for item in decorations}
        object_summaries: list[dict[str, Any]] = []
        for obj in objects:
            linked = [by_id[item] for item in obj.decoration_ids if item in by_id]
            summary: dict[str, Any] = {
                "objectId": obj.object_id,
                "refId": obj.ref_id,
                "assetId": obj.asset_id,
                "name": obj.name,
                "position": obj.transform.position.as_list(),
                "rotation": obj.transform.rotation.as_list(),
                "scale": obj.transform.scale.as_list(),
                "decorationCount": len(obj.decoration_ids),
            }
            if linked:
                xs = [item.transform.position.x for item in linked]
                ys = [item.transform.position.y for item in linked]
                zs = [item.transform.position.z for item in linked]
                summary["decorationAssets"] = {
                    str(asset): count
                    for asset, count in sorted(
                        Counter(item.asset_id for item in linked).items(),
                        key=lambda pair: (pair[0] is None, pair[0]),
                    )
                }
                summary["customColorCount"] = sum(
                    1 for item in linked if item.color_enabled
                )
                summary["bounds"] = {
                    "x": [min(xs), max(xs)],
                    "y": [min(ys), max(ys)],
                    "z": [min(zs), max(zs)],
                }
                summary["uniqueCoordinates"] = {
                    "x": len({round(value, 7) for value in xs}),
                    "y": len({round(value, 7) for value in ys}),
                    "z": len({round(value, 7) for value in zs}),
                }
            if verbose:
                summary["decorationIds"] = list(obj.decoration_ids)
            object_summaries.append(summary)

        result: dict[str, Any] = {
            "format": "GIL",
            "fileBytes": len(self.build_bytes()),
            "sha256": self.sha256,
            "envelope": {
                "leftSize": self.header.left_size,
                "schema": self.header.schema,
                "headTag": self.header.head_tag,
                "fileType": self.header.file_type,
                "protoSize": self.header.proto_size,
                "tailTag": self.header.tail_tag,
            },
            "topLevelFields": top_summary,
            "sceneObjectCount": len(objects),
            "sceneCategoryMappingCount": len(scene_mapping_targets),
            "decorationRecordCount": len(decorations),
            "prefabDecorationCount": sum(
                1 for item in decorations if item.store_field == 1
            ),
            "sceneDecorationCount": sum(
                1 for item in decorations if item.store_field == 2
            ),
            "sceneObjects": object_summaries,
            "validation": self.validate(),
        }
        if verbose:
            result["sceneCategoryMappingTargets"] = list(scene_mapping_targets)
            result["decorations"] = [
                {
                    "decorationId": item.decoration_id,
                    "storeField": item.store_field,
                    "assetId": item.asset_id,
                    "ownerId": item.owner_id,
                    "name": item.name,
                    "position": item.transform.position.as_list(),
                    "rotation": item.transform.rotation.as_list(),
                    "scale": item.transform.scale.as_list(),
                    "colorEnabled": item.color_enabled,
                    "argb": item.argb,
                    "argbHex": None
                    if item.argb is None
                    else f"0x{item.argb & 0xFFFFFFFF:08X}",
                    "rgb": item.rgb,
                    "rgbHex": None
                    if item.rgb is None
                    else f"#{item.rgb & 0xFFFFFF:06X}",
                    "opacityPercent": item.opacity_percent,
                }
                for item in decorations
            ]
        return result
