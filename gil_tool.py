"""Command-line GIL inspector and image-to-decoration editor.

All mutating commands are dry-run unless ``--write`` is supplied.  This tool
never permits the output path to equal the input path.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unicodedata

from qxy_gil import GilDocument, GilError, Vec3, image_to_decoration_groups


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from exc


def _positive_integer(value: str) -> int:
    parsed = _integer(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _print_json(value: dict[str, Any], *, stream: Any | None = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False),
        file=sys.stdout if stream is None else stream,
    )


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _display_width(value: str) -> int:
    """Return an adequate console-cell width for ASCII and CJK text."""

    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _truncate_display(value: str, maximum: int) -> str:
    """Truncate text without splitting a wide console character."""

    if _display_width(value) <= maximum:
        return value
    if maximum <= 3:
        return "." * maximum
    result: list[str] = []
    width = 0
    for character in value:
        character_width = _display_width(character)
        if width + character_width > maximum - 3:
            break
        result.append(character)
        width += character_width
    return "".join(result) + "..."


class _ProgressBar:
    """ANSI-free overall and per-stage progress display.

    An active stage is rewritten with ``\r`` and committed with a newline.
    Consequently this works in Windows PowerShell without cursor movement
    escape sequences, while completed stages remain visible as a short log.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        stream: Any | None = None,
        width: int = 16,
        overall_width: int = 10,
    ) -> None:
        self.enabled = enabled
        self.stream = sys.stderr if stream is None else stream
        self.width = width
        self.overall_width = overall_width
        self._last_length = 0
        self._started = False
        self._stage_active = False
        self._stage_index = 1
        self._stage_count = 1
        self._stage_label = ""
        self._last_detail = ""
        self._overall_start = 0.0
        self._overall_end = 100.0

    @staticmethod
    def _bar(percent: int, width: int) -> str:
        filled = round(width * percent / 100)
        return "#" * filled + "-" * (width - filled)

    def _render(self, stage_percent: int | float, detail: str | None = None) -> None:
        if not self.enabled:
            return
        child_value = max(0, min(100, round(stage_percent)))
        overall_value = round(
            self._overall_start
            + (self._overall_end - self._overall_start) * child_value / 100
        )
        overall_value = max(0, min(100, overall_value))
        label = _truncate_display(self._stage_label, 18)
        if detail is not None:
            self._last_detail = str(detail)
        rendered_detail = (
            _truncate_display(self._last_detail, 28) if self._last_detail else ""
        )
        line = (
            f"总计 [{self._bar(overall_value, self.overall_width)}] "
            f"{overall_value:3d}% | "
            f"{self._stage_index}/{self._stage_count} "
            f"[{self._bar(child_value, self.width)}] {child_value:3d}%  {label}"
        )
        if rendered_detail:
            line += f"  {rendered_detail}"
        line_width = _display_width(line)
        padding = " " * max(0, self._last_length - line_width)
        print(f"\r{line}{padding}", end="", file=self.stream, flush=True)
        self._last_length = line_width
        self._started = True

    def start_stage(
        self,
        label: str,
        *,
        index: int,
        count: int,
        overall_start: int | float,
        overall_end: int | float,
        detail: str = "",
    ) -> None:
        if not self.enabled:
            return
        if self._stage_active:
            self.complete_stage()
        self._stage_index = index
        self._stage_count = max(1, count)
        self._stage_label = label
        self._overall_start = float(overall_start)
        self._overall_end = float(overall_end)
        self._last_detail = str(detail)
        self._stage_active = True
        self._last_length = 0
        self._render(0, detail)

    def update_stage(
        self,
        completed: int | float,
        total: int | float = 100,
        detail: str | None = None,
    ) -> None:
        if not self.enabled or not self._stage_active:
            return
        percent = 100 if total <= 0 else 100 * completed / total
        self._render(percent, detail)

    def complete_stage(self, detail: str | None = None) -> None:
        if not self.enabled or not self._stage_active:
            return
        self._render(100, detail)
        print(file=self.stream, flush=True)
        self._stage_active = False
        self._started = False
        self._last_length = 0

    def update(self, percent: int | float, label: str) -> None:
        """Backward-compatible single-stage update used by older callers."""

        if not self.enabled:
            return
        if not self._stage_active:
            self._stage_active = True
        self._stage_label = label
        self._last_detail = ""
        self._overall_start = 0.0
        self._overall_end = 100.0
        self._render(percent)

    def finish(self) -> None:
        if not self.enabled:
            return
        if self._stage_active:
            self.complete_stage("完成")

    def stop(self) -> None:
        if self.enabled and self._started:
            print(file=self.stream, flush=True)
            self._started = False
            self._stage_active = False
            self._last_length = 0
            self._last_detail = ""


class _StageProgress:
    """Map named work stages onto one overall progress range."""

    def __init__(
        self,
        stages: list[tuple[str, str]],
        *,
        enabled: bool,
        stream: Any | None = None,
    ) -> None:
        self.stages = stages
        self._positions = {key: index for index, (key, _) in enumerate(stages)}
        self._bar = _ProgressBar(enabled=enabled, stream=stream)
        self._current = -1

    def update(
        self,
        key: str,
        completed: int | float = 0,
        total: int | float = 100,
        detail: str = "",
    ) -> None:
        if key not in self._positions:
            return
        target = self._positions[key]
        if target < self._current:
            return
        stage_count = len(self.stages)
        if target > self._current:
            if self._current >= 0:
                self._bar.complete_stage()
            for skipped in range(self._current + 1, target):
                _, skipped_label = self.stages[skipped]
                self._bar.start_stage(
                    skipped_label,
                    index=skipped + 1,
                    count=stage_count,
                    overall_start=100 * skipped / stage_count,
                    overall_end=100 * (skipped + 1) / stage_count,
                )
                self._bar.complete_stage()
            _, label = self.stages[target]
            self._bar.start_stage(
                label,
                index=target + 1,
                count=stage_count,
                overall_start=100 * target / stage_count,
                overall_end=100 * (target + 1) / stage_count,
                detail=detail,
            )
            self._current = target
        self._bar.update_stage(completed, total, detail)

    def finish(self) -> None:
        if not self.stages:
            return
        last_key = self.stages[-1][0]
        self.update(last_key, 100, 100)
        self._bar.complete_stage("完成")

    def stop(self) -> None:
        self._bar.stop()


def _print_map_summary(result: dict[str, Any]) -> None:
    image = result["image"]
    mutation = result["mutation"]
    validation = result["output"]["validation"]
    warning_count = len(validation["warnings"])
    validation_text = "校验通过"
    if warning_count:
        validation_text += f"，{warning_count} 条警告"

    if result["written"]:
        print(f"生成完成（{validation_text}）")
        output_path = Path(result["output"]["path"]).resolve(strict=False)
        print(f"保存：{output_path}")
    else:
        print(f"预演完成（未创建或修改文件，{validation_text}）")
    original_width = image.get("originalWidth", image["width"])
    original_height = image.get("originalHeight", image["height"])
    resampling_filter = image.get("resamplingFilter", "nearest")
    if (original_width, original_height) == (image["width"], image["height"]):
        dimensions = f"{image['width']:,} × {image['height']:,}"
    else:
        dimensions = (
            f"{original_width:,} × {original_height:,} → "
            f"{image['width']:,} × {image['height']:,}"
        )
    print(
        f"图片：{dimensions} = "
        f"{image['sourcePixels']:,} 像素"
        f"（"
        + (
            f"自动尺寸上限 {image['autoSizePixelLimit']:,} 像素，"
            if image.get("autoSizePixelLimit") is not None
            else ""
        )
        + f"采样 {resampling_filter}，可见 {image['visiblePixels']:,}，"
        f"颜色 {image['visibleColorCount']:,}）"
    )
    layout = image.get("layout", "tiled")
    decoration_label = (
        "平面" if image.get("decorationGeometry") == "plane" else "装饰物"
    )
    if layout == "batched":
        limit = image.get("maxPerParent", 999)
        capacity_summary = (
            f"每父上限 {limit:,}，"
            f"实际最多 {image['maxDecorationsPerParent']:,}"
        )
    else:
        capacity_summary = f"单父最多 {image['maxDecorationsPerParent']:,}"
    scene_summary = (
        f"场景：布局 {layout}，{mutation['parentCount']:,} 个父节点，"
        f"{image['decorationCount']:,} 个{decoration_label}"
        f"（{capacity_summary}）"
    )
    rotation = image.get("decorationRotation")
    if rotation is not None:
        rotation_text = ",".join(f"{value:g}" for value in rotation)
        scene_summary += f"，装饰旋转：({rotation_text})"
    parent_scale = image.get("parentScale")
    if parent_scale is not None and any(
        abs(value - 1.0) > 1e-9 for value in parent_scale
    ):
        parent_scale_text = ",".join(f"{value:g}" for value in parent_scale)
        scene_summary += f"，父级缩放：({parent_scale_text})"
    if layout in {"batched", "single-parent"}:
        anchor_position = image.get("anchorPosition")
        if anchor_position is not None:
            coordinates = ",".join(
                f"{0.0 if value == 0 else value:g}" for value in anchor_position
            )
            scene_summary += f"，左下角装饰物中心：({coordinates})"
    print(scene_summary)


def _safe_write(
    document: GilDocument,
    input_path: Path,
    output_path: Path,
    *,
    force: bool,
) -> None:
    source = input_path.resolve(strict=False)
    destination = output_path.resolve(strict=False)
    if source == destination:
        raise GilError("refusing to overwrite the input GIL; choose a different output path")
    if output_path.exists() and not force:
        raise GilError(
            f"output already exists: {output_path} (pass --force to replace that output)"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(document.build_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _handle_inspect(args: argparse.Namespace) -> int:
    document = GilDocument.load(args.input)
    result = document.inspect(verbose=args.verbose)
    result["command"] = "inspect"
    result["input"] = str(Path(args.input))
    _print_json(result)
    return 0 if result["validation"]["ok"] else 1


def _handle_validate(args: argparse.Namespace) -> int:
    document = GilDocument.load(args.input)
    validation = document.validate()
    result = {
        "command": "validate",
        "input": str(Path(args.input)),
        "sha256": document.sha256,
        **validation,
    }
    _print_json(result)
    return 0 if validation["ok"] else 1


def _handle_map_image(args: argparse.Namespace) -> int:
    stage_definitions = [
        ("read_gil", "读取 GIL 模板"),
        ("validate_template", "校验模板"),
        ("load", "读取图片"),
        ("resize", "缩放采样"),
        ("quantize", "颜色量化"),
        ("scan", "扫描与合并"),
        ("build", "构建场景"),
        ("validate", "校验生成结果"),
    ]
    if args.write:
        stage_definitions.extend(
            [
                ("write", "写入 GIL"),
                ("readback", "回读并复验"),
            ]
        )
    progress = _StageProgress(
        stage_definitions,
        enabled=(
            not args.json
            and not args.no_progress
            and _is_tty(sys.stderr)
        )
    )
    try:
        input_path = Path(args.input)
        progress.update("read_gil", 0, 1, input_path.name)
        document = GilDocument.load(input_path)
        progress.update("read_gil", 1, 1, f"{len(document.build_bytes()):,} 字节")

        progress.update("validate_template", 0, 1, "检查结构和绘图基底")
        input_validation = document.validate()
        if not input_validation["ok"]:
            raise GilError(
                "input GIL failed validation: "
                + "; ".join(input_validation["errors"])
            )

        parent = document.choose_decoration_parent(args.parent_id)
        linked = document.linked_decorations(parent)
        if args.asset_id is None:
            available_assets = [item.asset_id for item in linked if item.asset_id]
            if not available_assets:
                raise GilError("cannot infer a decoration asset ID from the target parent")
            asset_id = Counter(available_assets).most_common(1)[0][0]
        else:
            asset_id = args.asset_id
        if args.origin is None:
            origin = (
                Vec3(0.0, 0.0, 0.0)
                if args.layout in {"batched", "single-parent"}
                else parent.transform.position
            )
        else:
            origin = Vec3(*args.origin)
        progress.update(
            "validate_template",
            1,
            1,
            f"父节点 {parent.object_id}",
        )

        progress.update("load", 0, 1, Path(args.image).name)

        def report_image_stage(
            stage: str,
            completed: int,
            total: int,
            detail: str,
        ) -> None:
            key = {
                "load": "load",
                "resize": "resize",
                "quantize": "quantize",
                "scan": "scan",
                "merge": "scan",
            }.get(stage)
            if key is not None:
                if stage == "load":
                    translated_detail = (
                        Path(args.image).name
                        if completed < total
                        else f"已读取 {detail.removeprefix('loaded ')}"
                    )
                elif stage == "resize":
                    translated_detail = detail.replace(" -> ", " → ")
                elif stage == "quantize":
                    translated_detail = (
                        "保留原始颜色"
                        if args.colors is None
                        else f"最多 {args.colors} 色（无抖动）"
                    )
                else:
                    translated_detail = detail or ""
                progress.update(key, completed, total, translated_detail)

        def report_tiles(completed: int, total: int) -> None:
            progress.update(
                "scan",
                completed,
                total,
                f"处理像素 {completed:,}/{total:,}",
            )

        groups, image_summary = image_to_decoration_groups(
            args.image,
            asset_id=asset_id,
            pixel_size=args.pixel_size,
            tile_size=args.tile_size,
            width=args.width,
            height=args.height,
            resampling_filter=args.filter,
            colors=args.colors,
            merge_same_color=args.merge_same_color,
            alpha_threshold=args.alpha_threshold,
            z=args.z,
            decoration_rotation=(
                None
                if args.decoration_rotation is None
                else Vec3(*args.decoration_rotation)
            ),
            origin=origin,
            parent_scale=(
                None
                if args.parent_scale is None
                else Vec3(*args.parent_scale)
            ),
            layout=args.layout,
            max_per_parent=args.max_per_parent,
            max_decorations=args.max_decorations,
            auto_size_max_pixels=(
                args.max_decorations
                if args.width is None and args.height is None
                else None
            ),
            progress_callback=report_tiles,
            stage_progress_callback=report_image_stage,
        )
        image_summary.setdefault("resamplingFilter", args.filter)
        image_summary.setdefault("layout", args.layout)
        if args.layout == "batched":
            image_summary.setdefault("maxPerParent", args.max_per_parent)
        progress.update(
            "scan",
            image_summary["sourcePixels"],
            image_summary["sourcePixels"],
            f"{image_summary['decorationCount']:,} 个装饰物",
        )

        progress.update("build", 0, max(1, len(groups)), "准备场景对象")

        def report_scene_progress(*values: Any) -> None:
            if len(values) == 4:
                _, completed, total, detail = values
            elif len(values) == 3:
                completed, total, detail = values
            elif len(values) == 2:
                completed, total = values
                detail = f"装饰物 {completed:,}/{total:,}"
            else:
                return
            progress.update("build", completed, total, str(detail or ""))

        if args.layout == "batched":
            safe_max_per_parent: int | None = args.max_per_parent
        elif args.layout == "tiled":
            safe_max_per_parent = 100
        else:
            safe_max_per_parent = None
        next_document, mutation = document.replace_parent_decoration_groups(
            groups,
            parent_id=parent.object_id,
            progress_callback=report_scene_progress,
            _max_per_parent=safe_max_per_parent,
        )
        progress.update("build", len(groups), max(1, len(groups)), "场景已构建")
        count_changed = mutation.old_count != mutation.new_count
        parent_count_changed = len(mutation.parent_ids) != 1
        structural_change = count_changed or parent_count_changed
        if args.write and structural_change and not args.allow_count_change:
            raise GilError(
                "operation changes structure "
                f"(decorations {mutation.old_count}->{mutation.new_count}, "
                f"parents 1->{len(mutation.parent_ids)}); inspect the dry-run, then "
                "pass --allow-count-change"
            )

        progress.update("validate", 0, 1, "检查 ID、引用和文件结构")
        output_validation = next_document.validate()
        if not output_validation["ok"]:
            raise GilError(
                "generated output failed validation: "
                + "; ".join(output_validation["errors"])
            )
        output_path = Path(args.output) if args.output else None
        if args.write and output_path is None:
            raise GilError("--output is required when --write is supplied")
        progress.update("validate", 1, 1, "校验通过")
        if args.write:
            assert output_path is not None
            progress.update("write", 0, 1, output_path.name)
            _safe_write(
                next_document,
                input_path,
                output_path,
                force=args.force,
            )
            progress.update("write", 1, 1, f"{output_path.stat().st_size:,} 字节")
            progress.update("readback", 0, 2, "回读文件")
            written_document = GilDocument.load(output_path)
            progress.update("readback", 1, 2, "复验结构")
            written_validation = written_document.validate()
            if not written_validation["ok"]:
                raise GilError(
                    "written output failed read-back validation: "
                    + "; ".join(written_validation["errors"])
                )
            progress.update("readback", 2, 2, "复验通过")

        result = {
            "command": "map-image",
            "status": "written" if args.write else "dry-run",
            "written": bool(args.write),
            "important": (
                f"NEW GIL WRITTEN: {output_path}"
                if args.write
                else (
                    "NO FILE WAS CREATED OR CHANGED. This was only a dry-run; "
                    "re-run with --output, --write, and (when the plan changes "
                    "object counts) --allow-count-change."
                )
            ),
            "input": {
                "path": str(input_path),
                "bytes": len(document.build_bytes()),
                "sha256": document.sha256,
            },
            "output": {
                "path": None if output_path is None else str(output_path),
                "bytes": len(next_document.build_bytes()),
                "sha256": next_document.sha256,
                "validation": output_validation,
            },
            "image": image_summary,
            "assetId": asset_id,
            "mutation": mutation.as_dict(include_ids=args.verbose_ids),
            "decorationCountChanged": count_changed,
            "parentCountChanged": parent_count_changed,
            "countChangeRequiresConfirmation": structural_change,
            "safety": {
                "inputOverwriteAllowed": False,
                "unknownTopLevelFieldsPreserved": True,
                "recommendedTemplate": (
                    "one clean empty-model parent with reusable decorations"
                ),
                "envelopeTagsPreserved": (
                    document.header.schema == next_document.header.schema
                    and document.header.head_tag == next_document.header.head_tag
                    and document.header.file_type == next_document.header.file_type
                    and document.header.tail_tag == next_document.header.tail_tag
                ),
            },
        }
        progress.finish()
    except BaseException:
        progress.stop()
        raise

    if args.json:
        _print_json(result)
    else:
        _print_map_summary(result)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect, validate, and safely create edited copies of GIL files."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="show the understood envelope, entities, and decorations"
    )
    inspect_parser.add_argument("--input", required=True, help="input .gil file")
    inspect_parser.add_argument(
        "--verbose", action="store_true", help="include every decoration record"
    )
    inspect_parser.set_defaults(handler=_handle_inspect)

    validate_parser = subparsers.add_parser(
        "validate", help="perform envelope, protobuf, ID, owner, and reference checks"
    )
    validate_parser.add_argument("--input", required=True, help="input .gil file")
    validate_parser.set_defaults(handler=_handle_validate)

    map_parser = subparsers.add_parser(
        "map-image",
        help="map an image onto one parent or across tiled empty-model parents",
    )
    map_parser.add_argument("--input", required=True, help="input .gil template")
    map_parser.add_argument("--image", required=True, help="PNG or other Pillow image")
    map_parser.add_argument(
        "--output", help="new .gil path (required only when --write is used)"
    )
    map_parser.add_argument(
        "--parent-id",
        type=_integer,
        help="target scene empty-model ID; auto-detected when unique",
    )
    map_parser.add_argument(
        "--asset-id",
        type=_integer,
        help="decoration model asset ID; defaults to the template's common asset",
    )
    layout_group = map_parser.add_mutually_exclusive_group()
    layout_group.add_argument(
        "--layout",
        choices=("batched", "tiled", "single-parent"),
        help=(
            "scene layout (default: batched); batched splits globally merged "
            "decorations into bounded parents, tiled uses spatial 10x10 parents, "
            "and single-parent is experimental"
        ),
    )
    layout_group.add_argument(
        "--single-parent",
        dest="layout",
        action="store_const",
        const="single-parent",
        help=(
            "shortcut for --layout single-parent; dangerous experimental mode "
            "with no per-parent limit"
        ),
    )
    map_parser.set_defaults(layout="batched")
    map_parser.add_argument(
        "--max-per-parent",
        type=_positive_integer,
        default=999,
        help="maximum decorations per parent in batched layout (default: 999)",
    )
    map_parser.add_argument("--pixel-size", type=float, default=0.1)
    map_parser.add_argument(
        "--tile-size",
        type=int,
        default=10,
        help="spatial pixels per empty-model parent (default: 10)",
    )
    map_parser.add_argument(
        "--width",
        type=int,
        help=(
            "optional target pixel width; if both dimensions are omitted, "
            "auto-fit to --max-decorations"
        ),
    )
    map_parser.add_argument(
        "--height",
        type=int,
        help=(
            "optional target pixel height; if both dimensions are omitted, "
            "auto-fit to --max-decorations"
        ),
    )
    map_parser.add_argument(
        "--filter",
        choices=("nearest", "box", "bilinear", "bicubic", "lanczos"),
        default="nearest",
        help="image resampling filter used while resizing (default: nearest)",
    )
    map_parser.add_argument(
        "--colors",
        type=int,
        help="quantize to 2..256 colors without dithering before creating objects",
    )
    map_parser.add_argument(
        "--merge-same-color",
        action="store_true",
        help="merge adjacent equal-color pixels into rectangular decorations",
    )
    map_parser.add_argument(
        "--alpha-threshold",
        type=int,
        default=0,
        help="skip pixels with alpha at or below this value (default: 0)",
    )
    map_parser.add_argument(
        "--z",
        type=float,
        help=(
            "explicit local Z centre; plane default is 0, other models default "
            "to 0.5-pixelSize/2"
        ),
    )
    map_parser.add_argument(
        "--decoration-rotation",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        help=(
            "Euler rotation in degrees applied to every decoration; plane asset "
            "10009003 defaults to 90 0 0, while other assets preserve template "
            "rotation"
        ),
    )
    map_parser.add_argument(
        "--origin",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        help=(
            "batched/single-parent: lower-left visible block centre "
            "(default: 0 0 0); "
            "tiled: image origin (default: selected parent position)"
        ),
    )
    map_parser.add_argument(
        "--parent-scale",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        help=(
            "scale applied to every generated parent and its decoration hierarchy "
            "(default: 1 1 1); the requested origin remains fixed"
        ),
    )
    map_parser.add_argument(
        "--max-decorations",
        type=_positive_integer,
        default=10_000,
        help=(
            "total object limit and automatic pixel budget when width/height "
            "are omitted (default: 10000)"
        ),
    )
    map_parser.add_argument(
        "--write",
        action="store_true",
        help="write the planned output; without this flag the command is dry-run",
    )
    map_parser.add_argument(
        "--allow-count-change",
        action="store_true",
        help="confirm adding/removing decorations or parent objects when writing",
    )
    map_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file (the input can never be replaced)",
    )
    map_parser.add_argument(
        "--verbose-ids",
        action="store_true",
        help="include every reused/allocated/removed decoration ID with --json",
    )
    map_parser.add_argument(
        "--json",
        action="store_true",
        help="print the full machine-readable JSON report instead of a short summary",
    )
    map_parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the interactive progress bar",
    )
    map_parser.set_defaults(handler=_handle_map_image)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (GilError, OSError, ValueError) as exc:
        if args.command == "map-image" and not getattr(args, "json", False):
            print(f"错误：{exc}", file=sys.stderr)
        else:
            _print_json(
                {
                    "command": getattr(args, "command", None),
                    "ok": False,
                    "error": str(exc),
                },
                stream=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
