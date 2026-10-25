#!/usr/bin/env python3
"""Generate Tokifactor emoji resources from Aki's per-profile indexes."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "assets" / "emoji"
DEFAULT_TOKIFACTOR = ROOT.parent / "tokifactor"
RESOURCE_SUFFIX = Path("shared/src/commonMain/composeResources/files/emoji")
MAX_DIMENSION = 512


def _load_index(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"{path} is not a JSON array")
    entries: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError(f"{path} contains a non-object entry")
        name = str(item.get("name") or "").strip()
        description = " ".join(str(item.get("desc") or "").split())
        filename = str(item.get("file") or "").strip()
        if not name or not description or not filename:
            raise RuntimeError(f"{path} contains an incomplete entry: {item!r}")
        if Path(filename).name != filename:
            raise RuntimeError(f"invalid emoji filename in {path}: {filename!r}")
        if Path(filename).stem != name:
            raise RuntimeError(
                f"emoji name and filename differ in {path}: {name!r} != {filename!r}"
            )
        entries.append(
            {"name": name, "description": description, "filename": filename}
        )
    return entries


def _write_image(source: Path, destination: Path, max_dimension: int) -> None:
    suffix = source.suffix.casefold()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise RuntimeError(f"unsupported emoji image type: {source}")
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            if suffix == ".png":
                if image.mode not in ("RGBA", "LA"):
                    image = image.convert("RGBA")
                image.save(destination, format="PNG", optimize=True)
            else:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(
                    destination,
                    format="JPEG",
                    quality=90,
                    optimize=True,
                    progressive=True,
                )
    except OSError as exc:
        raise RuntimeError(f"cannot convert {source}: {exc}") from exc


def sync_emoji_assets(
    source_root: Path,
    tokifactor_root: Path,
    *,
    max_dimension: int = MAX_DIMENSION,
) -> dict[str, object]:
    if max_dimension <= 0:
        raise RuntimeError("max_dimension must be positive")
    target = tokifactor_root / RESOURCE_SUFFIX
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".emoji-", dir=target.parent))
    catalog: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        for profile_dir in sorted(source_root.iterdir()):
            if not profile_dir.is_dir():
                continue
            index_path = profile_dir / "index.json"
            if not index_path.is_file():
                continue
            profile = profile_dir.name
            entries = _load_index(index_path)
            output_dir = staging / profile
            output_dir.mkdir(parents=True)
            for entry in entries:
                name = entry["name"]
                filename = entry["filename"]
                if name in seen:
                    raise RuntimeError(f"duplicate emoji name across profiles: {name}")
                source = profile_dir / filename
                if not source.is_file():
                    raise RuntimeError(f"missing emoji file: {source}")
                destination = output_dir / filename
                _write_image(source, destination, max_dimension)
                seen.add(name)
                catalog.append(
                    {
                        "name": name,
                        "file": f"emoji/{profile}/{filename}",
                        "desc": entry["description"],
                    }
                )
        (staging / "catalog.json").write_text(
            json.dumps({"version": 1, "emojis": catalog}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"count": len(catalog), "target": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--tokifactor", type=Path, default=DEFAULT_TOKIFACTOR)
    parser.add_argument("--max-dimension", type=int, default=MAX_DIMENSION)
    args = parser.parse_args()
    try:
        result = sync_emoji_assets(
            args.source.expanduser().resolve(),
            args.tokifactor.expanduser().resolve(),
            max_dimension=args.max_dimension,
        )
    except RuntimeError as exc:
        parser.exit(1, f"sync-emoji-assets: {exc}\n")
    print(f"synced {result['count']} emoji assets to {result['target']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
