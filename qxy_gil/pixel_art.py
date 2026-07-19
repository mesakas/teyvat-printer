"""Convert a raster image into decoration specifications."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .document import DecorationGroupSpec, DecorationSpec, GilError, Vec3


RESAMPLING_FILTERS: dict[str, Image.Resampling] = {
    "nearest": Image.Resampling.NEAREST,
    "box": Image.Resampling.BOX,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}

GROUP_LAYOUTS = ("tiled", "batched", "single-parent")
PLANE_DECORATION_ASSET_IDS = frozenset({10009003})
DEFAULT_PLANE_ROTATION = Vec3(90.0, 0.0, 0.0)

StageProgressCallback = Callable[[str, int, int, str], None]


@dataclass(frozen=True)
class ColorRectangle:
    row: int
    column: int
    width: int
    height: int
    rgba: tuple[int, int, int, int]


def _stable_float(value: float) -> float:
    rounded = round(value, 9)
    return 0.0 if rounded == 0.0 else rounded


def _resolve_decoration_geometry(
    asset_id: int,
    decoration_rotation: Vec3 | None,
) -> tuple[bool, Vec3 | None, str]:
    is_plane = asset_id in PLANE_DECORATION_ASSET_IDS
    if decoration_rotation is None:
        if is_plane:
            return True, DEFAULT_PLANE_ROTATION, "plane-default"
        return False, None, "template-preserved"

    try:
        finite = all(math.isfinite(value) for value in decoration_rotation.as_list())
    except (AttributeError, TypeError) as exc:
        raise GilError("decoration rotation must contain three numbers") from exc
    if not finite:
        raise GilError("decoration rotation coordinates must be finite")
    return is_plane, decoration_rotation, "explicit"


def _decoration_scale(
    rectangle: ColorRectangle,
    pixel_size: float,
    *,
    is_plane: bool,
) -> Vec3:
    width = _stable_float(rectangle.width * pixel_size)
    height = _stable_float(rectangle.height * pixel_size)
    if is_plane:
        # Asset 10009003 is a local XZ plane.  After an X-axis rotation its
        # local Z dimension becomes the vertical extent of the image.
        return Vec3(width, 1.0, height)
    return Vec3(width, height, _stable_float(pixel_size))


def _target_size(
    source_width: int,
    source_height: int,
    width: int | None,
    height: int | None,
    *,
    max_pixels: int | None = None,
) -> tuple[int, int]:
    if width is not None and width <= 0:
        raise GilError("image width must be positive")
    if height is not None and height <= 0:
        raise GilError("image height must be positive")
    if max_pixels is not None and (
        not isinstance(max_pixels, int)
        or isinstance(max_pixels, bool)
        or max_pixels <= 0
    ):
        raise GilError("automatic image pixel limit must be a positive integer")
    if width is None and height is None:
        if max_pixels is None or source_width * source_height <= max_pixels:
            return source_width, source_height

        # Search along the source image's long edge.  The short edge uses the
        # same Python-round rule as an explicitly supplied width or height, so
        # automatic sizing and manual one-dimensional sizing stay consistent.
        # Since the rounded short edge never decreases as the long edge grows,
        # the pixel area is monotonic and can be searched exactly.
        primary_is_width = source_width >= source_height
        source_primary, source_secondary = (
            (source_width, source_height)
            if primary_is_width
            else (source_height, source_width)
        )
        low = 1
        high = min(source_primary, max_pixels)
        best = (1, 1)
        while low <= high:
            primary = (low + high) // 2
            secondary = max(
                1,
                round(source_secondary * primary / source_primary),
            )
            candidate = (
                (primary, secondary)
                if primary_is_width
                else (secondary, primary)
            )
            if candidate[0] * candidate[1] <= max_pixels:
                best = candidate
                low = primary + 1
            else:
                high = primary - 1
        return best
    if width is None:
        width = max(1, round(source_width * height / source_height))
    elif height is None:
        height = max(1, round(source_height * width / source_width))
    return width, height


def _quantize_without_dither(image: Image.Image, colors: int) -> Image.Image:
    if not 2 <= colors <= 256:
        raise GilError("--colors must be between 2 and 256")
    alpha = image.getchannel("A")
    quantized_rgb = image.convert("RGB").quantize(
        colors=colors,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    ).convert("RGB")
    quantized = quantized_rgb.convert("RGBA")
    quantized.putalpha(alpha)
    return quantized


def _report_stage(
    callback: StageProgressCallback | None,
    stage: str,
    completed: int,
    total: int,
    detail: str,
) -> None:
    if callback is not None:
        callback(stage, completed, total, detail)


def _prepare_image(
    image_path: str | Path,
    *,
    width: int | None,
    height: int | None,
    auto_size_max_pixels: int | None,
    colors: int | None,
    resampling_filter: str,
    stage_progress_callback: StageProgressCallback | None,
) -> tuple[Image.Image, tuple[int, int], tuple[int, int]]:
    if resampling_filter not in RESAMPLING_FILTERS:
        choices = ", ".join(RESAMPLING_FILTERS)
        raise GilError(
            f"--filter must be one of: {choices} (got {resampling_filter!r})"
        )

    resolved_path = Path(image_path)
    _report_stage(
        stage_progress_callback,
        "load",
        0,
        1,
        f"reading {resolved_path}",
    )
    try:
        with Image.open(resolved_path) as source:
            source.load()
            original_size = source.size
            image = source.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise GilError(f"failed to load image {resolved_path}: {exc}") from exc
    _report_stage(
        stage_progress_callback,
        "load",
        1,
        1,
        f"loaded {original_size[0]}x{original_size[1]} RGBA",
    )

    target_size = _target_size(
        image.width,
        image.height,
        width,
        height,
        max_pixels=auto_size_max_pixels,
    )
    resize_detail = (
        f"{original_size[0]}x{original_size[1]} -> "
        f"{target_size[0]}x{target_size[1]} ({resampling_filter})"
    )
    _report_stage(
        stage_progress_callback,
        "resize",
        0,
        1,
        resize_detail,
    )
    if image.size != target_size:
        image = image.resize(
            target_size,
            resample=RESAMPLING_FILTERS[resampling_filter],
        )
    _report_stage(
        stage_progress_callback,
        "resize",
        1,
        1,
        resize_detail,
    )

    quantize_detail = (
        "preserving source colors"
        if colors is None
        else f"quantizing to at most {colors} RGB colors without dithering"
    )
    _report_stage(
        stage_progress_callback,
        "quantize",
        0,
        1,
        quantize_detail,
    )
    if colors is not None:
        image = _quantize_without_dither(image, colors)
    _report_stage(
        stage_progress_callback,
        "quantize",
        1,
        1,
        quantize_detail,
    )
    return image, original_size, target_size


def _pixel_rectangles(
    image: Image.Image, alpha_threshold: int
) -> list[ColorRectangle]:
    width, height = image.size
    pixels = image.load()
    return [
        ColorRectangle(row, column, 1, 1, tuple(pixels[column, row]))
        for row in range(height)
        for column in range(width)
        if pixels[column, row][3] > alpha_threshold
    ]


def _merge_same_color_rectangles(
    image: Image.Image, alpha_threshold: int
) -> list[ColorRectangle]:
    """Greedily tile visible equal-color pixels with deterministic rectangles.

    This is intentionally simple and reproducible rather than globally optimal:
    for each unclaimed pixel it takes the widest run and then extends that run
    downward for as many identical rows as possible.
    """

    width, height = image.size
    pixels = image.load()
    used = [[False] * width for _ in range(height)]
    rectangles: list[ColorRectangle] = []
    for row in range(height):
        for column in range(width):
            if used[row][column]:
                continue
            rgba = tuple(pixels[column, row])
            if rgba[3] <= alpha_threshold:
                used[row][column] = True
                continue

            rect_width = 1
            while column + rect_width < width:
                next_column = column + rect_width
                if used[row][next_column] or tuple(pixels[next_column, row]) != rgba:
                    break
                rect_width += 1

            rect_height = 1
            while row + rect_height < height:
                next_row = row + rect_height
                if any(
                    used[next_row][x] or tuple(pixels[x, next_row]) != rgba
                    for x in range(column, column + rect_width)
                ):
                    break
                rect_height += 1

            for y in range(row, row + rect_height):
                for x in range(column, column + rect_width):
                    used[y][x] = True
            rectangles.append(
                ColorRectangle(
                    row=row,
                    column=column,
                    width=rect_width,
                    height=rect_height,
                    rgba=rgba,
                )
            )
    return rectangles


def image_to_decoration_specs(
    image_path: str | Path,
    *,
    asset_id: int,
    pixel_size: float = 0.1,
    width: int | None = None,
    height: int | None = None,
    resampling_filter: str = "nearest",
    colors: int | None = None,
    merge_same_color: bool = False,
    alpha_threshold: int = 0,
    z: float | None = None,
    decoration_rotation: Vec3 | None = None,
    max_decorations: int = 10_000,
    auto_size_max_pixels: int | None = None,
    stage_progress_callback: StageProgressCallback | None = None,
) -> tuple[list[DecorationSpec], dict[str, Any]]:
    if asset_id <= 0:
        raise GilError("decoration asset ID must be positive")
    if not math.isfinite(pixel_size) or pixel_size <= 0:
        raise GilError("pixel size must be finite and positive")
    if z is not None and not math.isfinite(z):
        raise GilError("z must be finite")
    if not 0 <= alpha_threshold <= 254:
        raise GilError("alpha threshold must be in the range 0..254")
    if max_decorations <= 0:
        raise GilError("max decorations must be positive")
    is_plane, resolved_rotation, rotation_mode = _resolve_decoration_geometry(
        asset_id,
        decoration_rotation,
    )

    image_path = Path(image_path)
    image, original_size, target_size = _prepare_image(
        image_path,
        width=width,
        height=height,
        auto_size_max_pixels=auto_size_max_pixels,
        colors=colors,
        resampling_filter=resampling_filter,
        stage_progress_callback=stage_progress_callback,
    )

    width_px, height_px = image.size
    pixels = list(image.getdata())
    visible_pixels = sum(1 for pixel in pixels if pixel[3] > alpha_threshold)
    visible_colors = len(
        {tuple(pixel) for pixel in pixels if pixel[3] > alpha_threshold}
    )
    rectangles = (
        _merge_same_color_rectangles(image, alpha_threshold)
        if merge_same_color
        else _pixel_rectangles(image, alpha_threshold)
    )
    if not rectangles:
        raise GilError("image has no pixels above the alpha threshold")
    if len(rectangles) > max_decorations:
        raise GilError(
            f"image needs {len(rectangles)} decorations, exceeding the configured "
            f"limit of {max_decorations}"
        )

    depth = _stable_float(
        (0.0 if is_plane else 0.5 - pixel_size / 2.0) if z is None else z
    )
    specs: list[DecorationSpec] = []
    for index, rectangle in enumerate(rectangles, start=1):
        center_column = rectangle.column + (rectangle.width - 1) / 2.0
        center_row = rectangle.row + (rectangle.height - 1) / 2.0
        # Coordinates are matched to the manually-authored 10x10 fixture:
        # left is +X, top is +Y, and the image is centred around (0, 0.5).
        x = _stable_float(((width_px - 1) / 2.0 - center_column) * pixel_size)
        y = _stable_float(
            0.5 + ((height_px - 1) / 2.0 - center_row) * pixel_size
        )
        specs.append(
            DecorationSpec(
                name=f"pixel_{index}",
                asset_id=asset_id,
                position=Vec3(x, y, depth),
                scale=_decoration_scale(
                    rectangle,
                    pixel_size,
                    is_plane=is_plane,
                ),
                rgba=rectangle.rgba,
                rotation=resolved_rotation,
            )
        )

    summary: dict[str, Any] = {
        "image": str(image_path),
        "originalSize": [original_size[0], original_size[1]],
        "originalWidth": original_size[0],
        "originalHeight": original_size[1],
        "targetSize": [target_size[0], target_size[1]],
        "sizeMode": (
            "auto-limit"
            if width is None
            and height is None
            and auto_size_max_pixels is not None
            else ("explicit" if width is not None or height is not None else "source")
        ),
        "autoSizePixelLimit": (
            auto_size_max_pixels
            if width is None and height is None
            else None
        ),
        "autoSized": (
            width is None
            and height is None
            and auto_size_max_pixels is not None
            and original_size != target_size
        ),
        "resamplingFilter": resampling_filter,
        "resized": original_size != target_size,
        "width": width_px,
        "height": height_px,
        "sourcePixels": width_px * height_px,
        "visiblePixels": visible_pixels,
        "visibleColorCount": visible_colors,
        "quantizedColors": colors,
        "mergeSameColor": merge_same_color,
        "decorationCount": len(specs),
        "decorationAssetId": asset_id,
        "decorationGeometry": "plane" if is_plane else "solid",
        "decorationRotation": (
            None if resolved_rotation is None else resolved_rotation.as_list()
        ),
        "decorationRotationMode": rotation_mode,
        "pixelSize": pixel_size,
        "z": depth,
        "boundsFormula": {
            "x": "((W-1)/2-column)*pixelSize",
            "y": "0.5+((H-1)/2-row)*pixelSize",
            "z": (
                ("0" if is_plane else "0.5-pixelSize/2")
                if z is None
                else "explicit"
            ),
        },
    }
    return specs, summary


def image_to_decoration_groups(
    image_path: str | Path,
    *,
    asset_id: int,
    pixel_size: float = 0.1,
    tile_size: int = 10,
    width: int | None = None,
    height: int | None = None,
    resampling_filter: str = "nearest",
    colors: int | None = None,
    merge_same_color: bool = False,
    alpha_threshold: int = 0,
    z: float | None = None,
    decoration_rotation: Vec3 | None = None,
    origin: Vec3 | None = None,
    parent_scale: Vec3 | None = None,
    layout: str = "tiled",
    max_per_parent: int = 999,
    max_decorations: int = 10_000,
    auto_size_max_pixels: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    stage_progress_callback: StageProgressCallback | None = None,
) -> tuple[list[DecorationGroupSpec], dict[str, Any]]:
    """Build capture-8-compatible local decorations.

    ``tiled`` preserves the original behavior: each non-empty spatial tile gets
    one parent and ``origin`` is the centred image reference.  ``batched`` and
    ``single-parent`` both scan/merge the whole image and interpret ``origin``
    as the desired world-space centre of the bottom-most, then left-most,
    visible decoration.  ``batched`` creates another co-located parent after
    ``max_per_parent`` decorations, while ``single-parent`` keeps every
    decoration in one experimental parent.  ``parent_scale`` is applied to
    every generated parent; group positions account for that scale so the
    requested origin remains a stable world-space anchor.  Decoration-local
    coordinates retain the captured centred coordinate convention in every
    layout.
    """

    if asset_id <= 0:
        raise GilError("decoration asset ID must be positive")
    if not math.isfinite(pixel_size) or pixel_size <= 0:
        raise GilError("pixel size must be finite and positive")
    if not isinstance(tile_size, int) or tile_size <= 0:
        raise GilError("tile size must be a positive integer")
    if layout not in GROUP_LAYOUTS:
        choices = ", ".join(GROUP_LAYOUTS)
        raise GilError(f"layout must be one of: {choices} (got {layout!r})")
    if not isinstance(max_per_parent, int) or max_per_parent <= 0:
        raise GilError("max decorations per parent must be positive")
    if layout == "tiled" and tile_size * pixel_size > 1.0 + 1e-9:
        raise GilError("tile-size * pixel-size must be <= 1 for a 1x1 parent face")
    if z is not None and not math.isfinite(z):
        raise GilError("z must be finite")
    resolved_origin = Vec3(0.0, 0.0, 0.0) if origin is None else origin
    if any(
        not math.isfinite(value)
        for value in resolved_origin.as_list()
    ):
        raise GilError("origin coordinates must be finite")
    resolved_parent_scale = (
        Vec3(1.0, 1.0, 1.0) if parent_scale is None else parent_scale
    )
    try:
        parent_scale_values = resolved_parent_scale.as_list()
    except (AttributeError, TypeError) as exc:
        raise GilError("parent scale must contain three numbers") from exc
    if any(not math.isfinite(value) for value in parent_scale_values):
        raise GilError("parent scale coordinates must be finite")
    if any(value <= 0 for value in parent_scale_values):
        raise GilError("parent scale coordinates must be positive")
    if not 0 <= alpha_threshold <= 254:
        raise GilError("alpha threshold must be in the range 0..254")
    if max_decorations <= 0:
        raise GilError("max decorations must be positive")
    is_plane, resolved_rotation, rotation_mode = _resolve_decoration_geometry(
        asset_id,
        decoration_rotation,
    )

    image_path = Path(image_path)
    image, original_size, target_size = _prepare_image(
        image_path,
        width=width,
        height=height,
        auto_size_max_pixels=auto_size_max_pixels,
        colors=colors,
        resampling_filter=resampling_filter,
        stage_progress_callback=stage_progress_callback,
    )

    width_px, height_px = image.size
    pixels = list(image.getdata())
    visible_pixels = sum(1 for pixel in pixels if pixel[3] > alpha_threshold)
    visible_colors = len(
        {tuple(pixel) for pixel in pixels if pixel[3] > alpha_threshold}
    )
    local_z = _stable_float(
        (0.0 if is_plane else 0.5 - pixel_size / 2.0) if z is None else z
    )
    default_z_formula = "0" if is_plane else "0.5-pixelSize/2"
    groups: list[DecorationGroupSpec]
    tile_summaries: list[dict[str, Any]]
    total_decorations: int
    progress_total = width_px * height_px
    if progress_callback is not None:
        progress_callback(0, progress_total)

    if layout in {"batched", "single-parent"}:
        rectangles = (
            _merge_same_color_rectangles(image, alpha_threshold)
            if merge_same_color
            else _pixel_rectangles(image, alpha_threshold)
        )
        if not rectangles:
            raise GilError("image has no pixels above the alpha threshold")
        total_decorations = len(rectangles)
        if total_decorations > max_decorations:
            raise GilError(
                f"image needs {total_decorations} decorations, exceeding the "
                f"configured limit of {max_decorations}"
            )

        # Source rows grow downward.  Prefer the decoration reaching the
        # lowest visible source row, then the one whose left edge is farthest
        # left.  This selects the rectangle containing the bottom-left pixel
        # when that pixel was merged into a larger decoration.
        anchor_index = min(
            range(len(rectangles)),
            key=lambda index: (
                -(rectangles[index].row + rectangles[index].height - 1),
                rectangles[index].column,
                rectangles[index].row,
            ),
        )
        single_specs: list[DecorationSpec] = []
        for index, rectangle in enumerate(rectangles, start=1):
            center_column = rectangle.column + (rectangle.width - 1) / 2.0
            center_row = rectangle.row + (rectangle.height - 1) / 2.0
            single_specs.append(
                DecorationSpec(
                    name=f"pixel_{index}",
                    asset_id=asset_id,
                    position=Vec3(
                        _stable_float(
                            ((width_px - 1) / 2.0 - center_column) * pixel_size
                        ),
                        _stable_float(
                            0.5
                            + ((height_px - 1) / 2.0 - center_row) * pixel_size
                        ),
                        local_z,
                    ),
                    scale=_decoration_scale(
                        rectangle,
                        pixel_size,
                        is_plane=is_plane,
                    ),
                    rgba=rectangle.rgba,
                    rotation=resolved_rotation,
                )
            )

        anchor_local = single_specs[anchor_index].position
        parent_position = Vec3(
            _stable_float(
                resolved_origin.x - resolved_parent_scale.x * anchor_local.x
            ),
            _stable_float(
                resolved_origin.y - resolved_parent_scale.y * anchor_local.y
            ),
            _stable_float(
                resolved_origin.z - resolved_parent_scale.z * anchor_local.z
            ),
        )
        group_size = (
            len(single_specs)
            if layout == "single-parent"
            else max_per_parent
        )
        groups = []
        tile_summaries = []
        for batch_index, start in enumerate(
            range(0, len(single_specs), group_size)
        ):
            batch_specs = tuple(single_specs[start : start + group_size])
            groups.append(
                DecorationGroupSpec(
                    position=parent_position,
                    decorations=batch_specs,
                    tile_row=0,
                    tile_column=batch_index,
                    scale=resolved_parent_scale,
                )
            )
            tile_summaries.append(
                {
                    "batchIndex": batch_index,
                    "sourceBounds": [0, 0, width_px, height_px],
                    "parentPosition": parent_position.as_list(),
                    "decorationStart": start,
                    "decorationCount": len(batch_specs),
                }
            )
        tile_rows = 1
        tile_columns = 1
        spatial_tile_count = 1
        merge_region_count = 1
        anchor_mode = "bottom-left-decoration"
        summary_parent_position: list[float] | None = parent_position.as_list()
        world_bounds_formula = {
            "x": "parentX+parentScaleX*((W-1)/2-rectangleCenterColumn)*pixelSize",
            "y": "parentY+parentScaleY*(0.5+((H-1)/2-rectangleCenterRow)*pixelSize)",
            "z": (
                f"parentZ+parentScaleZ*({default_z_formula})"
                if z is None
                else "parentZ+parentScaleZ*explicit"
            ),
        }
        if progress_callback is not None:
            progress_callback(progress_total, progress_total)
    else:
        groups = []
        tile_summaries = []
        decoration_index = 1
        total_decorations = 0
        tile_rows = (height_px + tile_size - 1) // tile_size
        tile_columns = (width_px + tile_size - 1) // tile_size
        progress_completed = 0
        for tile_row in range(tile_rows):
            # Capture 8 creates parents from the bottom of the image upward.
            # Convert that bottom-up index back to source row bounds here.
            end_row = height_px - tile_row * tile_size
            start_row = max(0, end_row - tile_size)
            for tile_column in range(tile_columns):
                start_column = tile_column * tile_size
                end_column = min(start_column + tile_size, width_px)
                tile_image = image.crop(
                    (start_column, start_row, end_column, end_row)
                )
                rectangles = (
                    _merge_same_color_rectangles(tile_image, alpha_threshold)
                    if merge_same_color
                    else _pixel_rectangles(tile_image, alpha_threshold)
                )
                if not rectangles:
                    progress_completed += tile_image.width * tile_image.height
                    if progress_callback is not None:
                        progress_callback(progress_completed, progress_total)
                    continue
                if len(rectangles) > 100:
                    raise GilError(
                        f"tile ({tile_row}, {tile_column}) needs {len(rectangles)} "
                        "decorations; use --tile-size 10 or smaller, quantization, "
                        "or --merge-same-color so every parent stays at or below 100"
                    )
                total_decorations += len(rectangles)
                if total_decorations > max_decorations:
                    raise GilError(
                        f"image needs {total_decorations} decorations, exceeding the "
                        f"configured limit of {max_decorations}"
                    )

                parent_position = Vec3(
                    _stable_float(
                        resolved_origin.x
                        + resolved_parent_scale.x
                        * (
                            width_px * pixel_size / 2.0
                            - 0.5
                            - tile_column * tile_size * pixel_size
                        )
                    ),
                    _stable_float(
                        resolved_origin.y
                        + resolved_parent_scale.y
                        * (
                            0.5
                            - height_px * pixel_size / 2.0
                            + tile_row * tile_size * pixel_size
                        )
                    ),
                    _stable_float(resolved_origin.z),
                )
                tile_specs: list[DecorationSpec] = []
                for rectangle in rectangles:
                    center_column = rectangle.column + (rectangle.width - 1) / 2.0
                    center_row = rectangle.row + (rectangle.height - 1) / 2.0
                    tile_specs.append(
                        DecorationSpec(
                            name=f"pixel_{decoration_index}",
                            asset_id=asset_id,
                            position=Vec3(
                                _stable_float(
                                    0.5
                                    - pixel_size / 2.0
                                    - center_column * pixel_size
                                ),
                                _stable_float(
                                    pixel_size / 2.0
                                    + (tile_image.height - 1 - center_row)
                                    * pixel_size
                                ),
                                local_z,
                            ),
                            scale=_decoration_scale(
                                rectangle,
                                pixel_size,
                                is_plane=is_plane,
                            ),
                            rgba=rectangle.rgba,
                            rotation=resolved_rotation,
                        )
                    )
                    decoration_index += 1
                groups.append(
                    DecorationGroupSpec(
                        position=parent_position,
                        decorations=tuple(tile_specs),
                        tile_row=tile_row,
                        tile_column=tile_column,
                        scale=resolved_parent_scale,
                    )
                )
                tile_summaries.append(
                    {
                        "tileRow": tile_row,
                        "tileRowFromBottom": tile_row,
                        "tileColumn": tile_column,
                        "sourceBounds": [
                            start_column,
                            start_row,
                            end_column,
                            end_row,
                        ],
                        "parentPosition": parent_position.as_list(),
                        "decorationCount": len(tile_specs),
                    }
                )
                progress_completed += tile_image.width * tile_image.height
                if progress_callback is not None:
                    progress_callback(progress_completed, progress_total)

        spatial_tile_count = tile_rows * tile_columns
        merge_region_count = spatial_tile_count
        anchor_mode = "center-reference"
        summary_parent_position = None
        world_bounds_formula = {
            "x": "originX+parentScaleX*((W-1)/2-column)*pixelSize",
            "y": "originY+parentScaleY*(0.5+((H-1)/2-row)*pixelSize)",
            "z": (
                f"originZ+parentScaleZ*({default_z_formula})"
                if z is None
                else "originZ+parentScaleZ*explicit"
            ),
        }

    if not groups:
        raise GilError("image has no pixels above the alpha threshold")
    summary: dict[str, Any] = {
        "image": str(image_path),
        "originalSize": [original_size[0], original_size[1]],
        "originalWidth": original_size[0],
        "originalHeight": original_size[1],
        "targetSize": [target_size[0], target_size[1]],
        "sizeMode": (
            "auto-limit"
            if width is None
            and height is None
            and auto_size_max_pixels is not None
            else ("explicit" if width is not None or height is not None else "source")
        ),
        "autoSizePixelLimit": (
            auto_size_max_pixels
            if width is None and height is None
            else None
        ),
        "autoSized": (
            width is None
            and height is None
            and auto_size_max_pixels is not None
            and original_size != target_size
        ),
        "resamplingFilter": resampling_filter,
        "resized": original_size != target_size,
        "width": width_px,
        "height": height_px,
        "sourcePixels": width_px * height_px,
        "visiblePixels": visible_pixels,
        "visibleColorCount": visible_colors,
        "quantizedColors": colors,
        "mergeSameColor": merge_same_color,
        "mergeScope": (
            ("whole-image" if layout in {"batched", "single-parent"} else "within-tile")
            if merge_same_color
            else None
        ),
        "layout": layout,
        "maxPerParent": max_per_parent if layout == "batched" else None,
        "batchCount": len(groups) if layout == "batched" else None,
        "tileSize": tile_size,
        "tileRows": tile_rows,
        "tileColumns": tile_columns,
        "spatialTileCount": spatial_tile_count,
        "mergeRegionCount": merge_region_count,
        "parentCount": len(groups),
        "decorationCount": total_decorations,
        "decorationAssetId": asset_id,
        "decorationGeometry": "plane" if is_plane else "solid",
        "decorationRotation": (
            None if resolved_rotation is None else resolved_rotation.as_list()
        ),
        "decorationRotationMode": rotation_mode,
        "maxDecorationsPerParent": max(
            len(group.decorations) for group in groups
        ),
        "pixelSize": pixel_size,
        "parentScale": resolved_parent_scale.as_list(),
        "worldPixelSpacing": [
            _stable_float(pixel_size * resolved_parent_scale.x),
            _stable_float(pixel_size * resolved_parent_scale.y),
        ],
        "z": local_z,
        "origin": resolved_origin.as_list(),
        "anchorMode": anchor_mode,
        "anchorPosition": resolved_origin.as_list(),
        "parentPosition": summary_parent_position,
        "tiles": tile_summaries,
        "worldBoundsFormula": world_bounds_formula,
    }
    return groups, summary
