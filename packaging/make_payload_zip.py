"""把 ``dist/AI社交秘书/`` 打成 LZMA 压缩的 ``payload.zip``（自研 onefile 安装器内嵌用）。

为什么压成 zip：
- PyInstaller onefile 内嵌 datas 用的是 zlib 级压缩；对 47MB onedir（含大量 Tcl/Tk 脚本、
  DLL）压完仍有 ~35MB。改用 ZIP+LZMA 预压缩后，安装器体积约减半（~20MB 量级），
  payload 在安装时由 ``installer.operations`` 用 ``zipfile`` 流式解压到安装目录。
- zip 内路径以 payload 根为基准（``SocialSecretary.exe``、``_internal/...``、``config/...``），
  解压到安装目录即得到与 onedir 完全一致的布局。

用法：
    .venv\\Scripts\\python.exe packaging\\make_payload_zip.py ^
        --src dist/AI社交秘书 --dst build/payload.zip [--level 9]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_zip(src: Path, dst: Path, level: int = 9) -> dict:
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    if not src.is_dir():
        raise SystemExit(f"[payload] 源目录不存在：{src}")
    exe = src / "SocialSecretary.exe"
    if not exe.is_file():
        raise SystemExit(f"[payload] 源目录缺少 SocialSecretary.exe：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()

    files = 0
    raw_bytes = 0
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_LZMA) as zf:
        for item in sorted(src.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(src).as_posix()
            info = zipfile.ZipInfo(rel)
            info.compress_type = zipfile.ZIP_LZMA
            info.external_attr = 0o600 << 16
            with item.open("rb") as fh:
                data = fh.read()
            zf.writestr(info, data, compresslevel=level)
            files += 1
            raw_bytes += len(data)

    digest = hashlib.sha256(dst.read_bytes()).hexdigest()
    summary = {
        "src": str(src),
        "dst": str(dst),
        "files": files,
        "raw_bytes": raw_bytes,
        "zip_bytes": dst.stat().st_size,
        "sha256": digest,
        "compression": "ZIP_LZMA",
        "level": int(level),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成自研安装器内嵌的 payload.zip（LZMA）")
    ap.add_argument("--src", default=str(ROOT / "dist" / "AI社交秘书"))
    ap.add_argument("--dst", default=str(ROOT / "build" / "payload.zip"))
    ap.add_argument("--level", type=int, default=9, help="LZMA 压缩级别（0-9，默认 9）")
    args = ap.parse_args(argv)
    try:
        build_zip(Path(args.src), Path(args.dst), args.level)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[payload] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
