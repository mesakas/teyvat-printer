"""Pure-Python helpers for safely inspecting and editing GIL files."""

from .document import (
    Decoration,
    DecorationGroupSpec,
    DecorationSpec,
    GilDocument,
    GilError,
    GilHeader,
    MutationSummary,
    SceneObject,
    Transform,
    Vec3,
)
from .pixel_art import image_to_decoration_groups, image_to_decoration_specs

__all__ = [
    "Decoration",
    "DecorationGroupSpec",
    "DecorationSpec",
    "GilDocument",
    "GilError",
    "GilHeader",
    "MutationSummary",
    "SceneObject",
    "Transform",
    "Vec3",
    "image_to_decoration_groups",
    "image_to_decoration_specs",
]
