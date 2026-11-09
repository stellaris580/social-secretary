"""应用路径、常量与运行环境探测。所有模块统一从这里取路径，禁止散落硬编码。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .contracts import APP_ID, APP_NAME, APP_VERSION


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """源码运行时的仓库根目录（打包后为 exe 所在目录）。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    override = os.environ.get("SOCIAL_SECRETARY_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / APP_ID


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    db_path: Path
    logs_dir: Path
    exports_dir: Path
    config_path: Path

    @classmethod
    def create(cls, data_dir: Path | None = None) -> "AppPaths":
        root = Path(data_dir) if data_dir is not None else default_data_dir()
        paths = cls(
            data_dir=root,
            db_path=root / "data" / "secretary.db",
            logs_dir=root / "logs",
            exports_dir=root / "exports",
            config_path=root / "settings.json",
        )
        for d in (paths.data_dir, paths.db_path.parent, paths.logs_dir, paths.exports_dir):
            d.mkdir(parents=True, exist_ok=True)
        return paths


__all__ = [
    "APP_NAME",
    "APP_ID",
    "APP_VERSION",
    "AppPaths",
    "default_data_dir",
    "project_root",
]
