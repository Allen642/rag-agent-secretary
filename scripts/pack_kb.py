"""知识包打包与查看 CLI。

运行：
    python scripts/pack_kb.py pack --src pack/example --out pack/example.zip
    python scripts/pack_kb.py inspect --path pack/example.zip
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.rag.chunking import is_supported_file


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _resolve(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def pack(src: Path, out: Path) -> int:
    if not src.is_dir():
        print(f"源目录不存在：{src}")
        return 2

    files = sorted(
        path for path in src.rglob("*") if path.is_file() and is_supported_file(path)
    )
    if not files:
        print("源目录里没有可打包的 .md/.markdown/.txt 文件。")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = src / "manifest.json"
        if manifest.exists():
            archive.write(manifest, "manifest.json")
        for path in files:
            archive.write(path, path.relative_to(src).as_posix())

    print(f"已打包 {len(files)} 个文件 -> {out}")
    return 0


def inspect(pack_path: Path) -> int:
    if not pack_path.is_file():
        print(f"知识包不存在：{pack_path}")
        return 2

    with zipfile.ZipFile(pack_path) as archive:
        names = archive.namelist()
        if "manifest.json" in names:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            print(f"manifest: {json.dumps(manifest, ensure_ascii=False)}")
        supported = [
            name
            for name in names
            if Path(name).suffix.lower() in {".md", ".markdown", ".txt"}
        ]
        print(f"压缩包共 {len(names)} 个条目，可导入文档 {len(supported)} 个：")
        for name in supported:
            print(f"  - {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="知识包打包/查看")
    sub = parser.add_subparsers(dest="command", required=True)

    pack_cmd = sub.add_parser("pack", help="把目录打包成 zip")
    pack_cmd.add_argument("--src", required=True)
    pack_cmd.add_argument("--out", required=True)

    inspect_cmd = sub.add_parser("inspect", help="查看 zip 内容")
    inspect_cmd.add_argument("--path", required=True)

    args = parser.parse_args(argv)
    if args.command == "pack":
        return pack(_resolve(args.src), _resolve(args.out))
    return inspect(_resolve(args.path))


if __name__ == "__main__":
    raise SystemExit(main())
