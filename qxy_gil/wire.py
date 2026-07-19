"""Small lossless protobuf wire helpers used by the GIL editor.

The game schema is only partially known.  A generated protobuf class would
discard or reorder data that it does not understand, so this module keeps the
original encoded bytes for every untouched field.  Only fields explicitly
replaced by an operation are encoded again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import struct
from typing import Iterable


class WireError(ValueError):
    """Raised when a protobuf wire message is malformed or unsupported."""


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise WireError("varints must be supplied as unsigned integers")
    if value > 0xFFFFFFFFFFFFFFFF:
        raise WireError("varint exceeds uint64")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def decode_varint(data: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    start = offset
    while offset < len(data) and offset - start < 10:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            if value > 0xFFFFFFFFFFFFFFFF:
                raise WireError(f"varint at offset {start} exceeds uint64")
            return value, offset
        shift += 7
    if offset >= len(data):
        raise WireError(f"truncated varint at offset {start}")
    raise WireError(f"varint at offset {start} is longer than 10 bytes")


@dataclass(frozen=True)
class WireField:
    number: int
    wire_type: int
    value: int | bytes
    raw: bytes | None = None

    def encoded(self) -> bytes:
        if self.raw is not None:
            return self.raw
        key = encode_varint((self.number << 3) | self.wire_type)
        if self.wire_type == 0:
            return key + encode_varint(int(self.value))
        if self.wire_type == 1:
            raw_value = bytes(self.value)
            if len(raw_value) != 8:
                raise WireError("fixed64 field must contain exactly 8 bytes")
            return key + raw_value
        if self.wire_type == 2:
            raw_value = bytes(self.value)
            return key + encode_varint(len(raw_value)) + raw_value
        if self.wire_type == 5:
            raw_value = bytes(self.value)
            if len(raw_value) != 4:
                raise WireError("fixed32 field must contain exactly 4 bytes")
            return key + raw_value
        raise WireError(f"unsupported wire type {self.wire_type}")

    def with_value(self, value: int | bytes) -> "WireField":
        return replace(self, value=value, raw=None)


def parse_fields(data: bytes, *, context: str = "protobuf message") -> list[WireField]:
    fields: list[WireField] = []
    offset = 0
    while offset < len(data):
        start = offset
        key, offset = decode_varint(data, offset)
        number = key >> 3
        wire_type = key & 7
        if number == 0:
            raise WireError(f"{context}: field number 0 at offset {start}")

        if wire_type == 0:
            value, offset = decode_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise WireError(f"{context}: truncated fixed64 field {number}")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise WireError(
                    f"{context}: field {number} length {length} exceeds remaining bytes"
                )
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise WireError(f"{context}: truncated fixed32 field {number}")
            value = data[offset:end]
            offset = end
        elif wire_type in (3, 4):
            raise WireError(
                f"{context}: legacy protobuf groups are not supported (field {number})"
            )
        else:
            raise WireError(f"{context}: invalid wire type {wire_type} for field {number}")

        fields.append(WireField(number, wire_type, value, data[start:offset]))
    return fields


def rebuild_message(fields: Iterable[WireField]) -> bytes:
    return b"".join(field.encoded() for field in fields)


def varint_field(number: int, value: int) -> WireField:
    return WireField(number, 0, value)


def len_field(number: int, value: bytes) -> WireField:
    return WireField(number, 2, value)


def fixed32_field(number: int, value: float) -> WireField:
    return WireField(number, 5, struct.pack("<f", float(value)))


def first_index(
    fields: list[WireField], number: int, wire_type: int, *, start: int = 0
) -> int | None:
    for index in range(start, len(fields)):
        field = fields[index]
        if field.number == number and field.wire_type == wire_type:
            return index
    return None


def first_field(fields: list[WireField], number: int, wire_type: int) -> WireField | None:
    index = first_index(fields, number, wire_type)
    return None if index is None else fields[index]


def first_varint(fields: list[WireField], number: int) -> int | None:
    field = first_field(fields, number, 0)
    return None if field is None else int(field.value)


def first_bytes(fields: list[WireField], number: int) -> bytes | None:
    field = first_field(fields, number, 2)
    return None if field is None else bytes(field.value)


def set_varint(fields: list[WireField], number: int, value: int) -> None:
    index = first_index(fields, number, 0)
    replacement = varint_field(number, value)
    if index is None:
        fields.append(replacement)
    else:
        fields[index] = fields[index].with_value(value)


def set_fixed32(fields: list[WireField], number: int, value: float) -> None:
    index = first_index(fields, number, 5)
    raw = struct.pack("<f", float(value))
    replacement = WireField(number, 5, raw)
    if index is None:
        fields.append(replacement)
    else:
        fields[index] = fields[index].with_value(raw)


def set_bytes(fields: list[WireField], number: int, value: bytes) -> None:
    index = first_index(fields, number, 2)
    replacement = len_field(number, value)
    if index is None:
        fields.append(replacement)
    else:
        fields[index] = fields[index].with_value(value)


def unpack_fixed32(field: WireField | None) -> float | None:
    if field is None or field.wire_type != 5:
        return None
    return struct.unpack("<f", bytes(field.value))[0]


def encode_packed_varints(values: Iterable[int]) -> bytes:
    return b"".join(encode_varint(value) for value in values)


def decode_packed_varints(data: bytes) -> list[int]:
    values: list[int] = []
    offset = 0
    while offset < len(data):
        value, next_offset = decode_varint(data, offset)
        if next_offset <= offset:
            raise WireError("packed varint decoder made no progress")
        values.append(value)
        offset = next_offset
    return values
