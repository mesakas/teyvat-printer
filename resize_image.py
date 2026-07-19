#!/usr/bin/env python3
"""Resize an image using one or two target dimensions.

The module can be used from the command line or imported by later image-to-block
tooling.  If only one dimension is supplied, the other is calculated from the
source image's aspect ratio.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError


RESAMPLING_FILTERS = {
    "nearest": Image.Resampling.NEAREST,
    "box": Image.Resampling.BOX,
    "bilinear": Image.Resampling.BILINEAR,
    "bicubic": Image.Resampling.BICUBIC,
    "lanczos": Image.Resampling.LANCZOS,
}

SUPPORTED_OUTPUT_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}


def parse_size(value: str) -> tuple[int, int]:
    """Parse a size such as ``24x24`` into ``(24, 24)``."""
    normalized = value.lower().replace("×", "x").strip()
    try:
        width_text, height_text = normalized.split("x", maxsplit=1)
        width = int(width_text)
        height = int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "分辨率格式应为 宽x高，例如 24x24"
        ) from exc

    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("宽度和高度必须大于 0")
    return width, height


def parse_positive_int(value: str) -> int:
    """Parse a strictly positive integer for a command-line dimension."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("像素值必须是整数") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("像素值必须大于 0")
    return number


def parse_rgba(value: str) -> tuple[int, int, int, int]:
    """Parse a Pillow-compatible color into an RGBA tuple."""
    try:
        return ImageColor.getcolor(value, "RGBA")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "背景色格式无效，请使用名称或 #RRGGBB / #RRGGBBAA"
        ) from exc


def default_output_path(input_path: Path, size: tuple[int, int]) -> Path:
    """Return a non-destructive PNG output path next to the source image."""
    width, height = size
    return input_path.with_name(f"{input_path.stem}_{width}x{height}.png")


def resolve_target_size(
    source_size: tuple[int, int],
    requested_size: tuple[int | None, int | None],
) -> tuple[int, int]:
    """Resolve a possibly partial target size while preserving aspect ratio."""
    source_width, source_height = source_size
    width, height = requested_size

    if width is None and height is None:
        raise ValueError("必须至少指定宽度或高度中的一个")
    if width is not None and width <= 0:
        raise ValueError("宽度必须大于 0")
    if height is not None and height <= 0:
        raise ValueError("高度必须大于 0")

    # Integer arithmetic gives conventional half-up rounding and avoids
    # floating-point drift for very large source images.
    if width is None:
        assert height is not None
        width = max(1, (source_width * height + source_height // 2) // source_height)
    elif height is None:
        height = max(1, (source_height * width + source_width // 2) // source_width)

    return width, height


def count_pixels(image: Image.Image) -> tuple[int, int]:
    """Return ``(total_pixels, non_fully_transparent_pixels)`` for an image."""
    total_pixels = image.width * image.height
    if "A" not in image.getbands():
        return total_pixels, total_pixels

    alpha_histogram = image.getchannel("A").histogram()
    fully_transparent_pixels = alpha_histogram[0]
    return total_pixels, total_pixels - fully_transparent_pixels


def _resize_rgba(
    source: Image.Image,
    size: tuple[int, int],
    mode: str,
    resample: Image.Resampling,
    background: tuple[int, int, int, int],
) -> Image.Image:
    source_rgba = source.convert("RGBA")

    if mode == "stretch":
        return source_rgba.resize(size, resample=resample)

    if mode == "cover":
        return ImageOps.fit(
            source_rgba,
            size,
            method=resample,
            centering=(0.5, 0.5),
        )

    contained = ImageOps.contain(source_rgba, size, method=resample)
    canvas = Image.new("RGBA", size, background)
    offset = (
        (size[0] - contained.width) // 2,
        (size[1] - contained.height) // 2,
    )
    canvas.alpha_composite(contained, dest=offset)
    return canvas


def _save_image(
    image: Image.Image,
    output_path: Path,
    background: tuple[int, int, int, int],
    quality: int,
) -> None:
    suffix = output_path.suffix.lower()
    if suffix not in SUPPORTED_OUTPUT_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_OUTPUT_SUFFIXES))
        raise ValueError(f"不支持输出格式 {suffix or '(无扩展名)'}；可用格式：{supported}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if suffix in {".jpg", ".jpeg"}:
        # JPEG has no alpha channel. Transparent areas are placed on white;
        # an explicitly opaque --background color is respected.
        jpeg_background = background[:3] if background[3] else (255, 255, 255)
        flattened = Image.new("RGB", image.size, jpeg_background)
        flattened.paste(image, mask=image.getchannel("A"))
        flattened.save(output_path, quality=quality, optimize=True)
    elif suffix == ".webp":
        image.save(output_path, quality=quality, method=6)
    elif suffix == ".png":
        image.save(output_path, optimize=True)
    else:
        image.save(output_path)


def resize_image(
    input_path: str | Path,
    size: tuple[int | None, int | None],
    output_path: str | Path | None = None,
    *,
    mode: str = "stretch",
    resampling_filter: str = "lanczos",
    background: tuple[int, int, int, int] = (0, 0, 0, 0),
    quality: int = 95,
) -> Path:
    """Resize *input_path* and return the path of the generated image.

    Set one item of ``size`` to ``None`` to calculate that dimension from the
    source image. For example, ``(24, None)`` preserves the aspect ratio using
    a width of 24 pixels, while ``(24, 24)`` produces exactly 24 by 24 pixels.

    Modes:
        ``stretch`` changes the aspect ratio if necessary.
        ``contain`` preserves the aspect ratio and adds padding.
        ``cover`` preserves the aspect ratio and crops overflowing edges.
    """
    source_path = Path(input_path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到输入图片：{source_path}")
    if mode not in {"stretch", "contain", "cover"}:
        raise ValueError(f"未知缩放模式：{mode}")
    if resampling_filter not in RESAMPLING_FILTERS:
        raise ValueError(f"未知采样算法：{resampling_filter}")
    if size[0] is None and size[1] is None:
        raise ValueError("必须至少指定宽度或高度中的一个")
    if size[0] is not None and size[0] <= 0:
        raise ValueError("宽度必须大于 0")
    if size[1] is not None and size[1] <= 0:
        raise ValueError("高度必须大于 0")
    if not 1 <= quality <= 100:
        raise ValueError("图片质量必须在 1 到 100 之间")

    requested_destination = Path(output_path).expanduser() if output_path else None
    if (
        requested_destination is not None
        and requested_destination.resolve() == source_path.resolve()
    ):
        raise ValueError("输出路径不能与输入图片相同，以免覆盖原图")

    try:
        with Image.open(source_path) as opened:
            oriented = ImageOps.exif_transpose(opened)
            target_size = resolve_target_size(oriented.size, size)
            destination = requested_destination or default_output_path(
                source_path, target_size
            )
            if not destination.suffix:
                destination = destination.with_suffix(".png")
            if destination.resolve() == source_path.resolve():
                raise ValueError("输出路径不能与输入图片相同，以免覆盖原图")
            resized = _resize_rgba(
                oriented,
                target_size,
                mode,
                RESAMPLING_FILTERS[resampling_filter],
                background,
            )
            _save_image(resized, destination, background, quality)
    except UnidentifiedImageError as exc:
        raise ValueError(f"无法识别图片格式：{source_path}") from exc

    return destination.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将图片缩放为指定分辨率。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="输入图片路径")
    parser.add_argument(
        "-s",
        "--size",
        type=parse_size,
        metavar="宽x高",
        help="同时指定宽高，例如 24x24；不能与 --width/--height 同时使用",
    )
    parser.add_argument(
        "-W",
        "--width",
        type=parse_positive_int,
        metavar="像素",
        help="目标宽度；未指定高度时会按原图比例计算",
    )
    parser.add_argument(
        "-H",
        "--height",
        type=parse_positive_int,
        metavar="像素",
        help="目标高度；未指定宽度时会按原图比例计算",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出路径；省略时在输入图片旁生成 PNG",
    )
    parser.add_argument(
        "--mode",
        choices=("stretch", "contain", "cover"),
        default="stretch",
        help="stretch=拉伸，contain=保持比例并留边，cover=保持比例并裁剪",
    )
    parser.add_argument(
        "--filter",
        dest="resampling_filter",
        choices=tuple(RESAMPLING_FILTERS),
        default="lanczos",
        help="缩放采样算法；像素画可选 nearest",
    )
    parser.add_argument(
        "--background",
        type=parse_rgba,
        default=(0, 0, 0, 0),
        metavar="颜色",
        help="contain 模式的留边颜色，例如 #FFFFFF 或 #00000000",
    )
    parser.add_argument(
        "--quality",
        type=int,
        choices=range(1, 101),
        default=95,
        metavar="1-100",
        help="JPEG/WebP 输出质量",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.size is not None and (args.width is not None or args.height is not None):
        parser.error("--size 不能与 --width 或 --height 同时使用")
    if args.size is not None:
        requested_size: tuple[int | None, int | None] = args.size
    elif args.width is not None or args.height is not None:
        requested_size = (args.width, args.height)
    else:
        parser.error("必须提供 --width、--height 或 --size")

    try:
        output = resize_image(
            args.input,
            requested_size,
            args.output,
            mode=args.mode,
            resampling_filter=args.resampling_filter,
            background=args.background,
            quality=args.quality,
        )
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    with Image.open(output) as generated:
        output_size = generated.size
        total_pixels, used_pixels = count_pixels(generated)
    print(f"已生成：{output}")
    print(f"输出分辨率：{output_size[0]}x{output_size[1]}")
    print(f"总像素数：{total_pixels:,}")
    print(f"有效像素数（非完全透明）：{used_pixels:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
