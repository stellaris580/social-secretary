"""pytest 全局配置。

Windows DSH 沙箱下有两个坑：
1. 默认临时目录在 C:\\Users\\...\\Temp，沙箱只允许写工作区 → 必须把 TMP/TEMP 提前指到工作区；
2. pytest 9 的 tmp_path 工厂用 ``mkdir(mode=0o700)`` 创建目录，在该沙箱下连创建者自己都
   PermissionError（WinError 5）→ 这里覆盖 ``tmp_path_factory`` / ``tmp_path``，改用普通 mkdir。

所有测试因此都使用工作区内、进程隔离的临时目录，绝不写用户真实数据目录。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_TMP = ROOT / ".tmp" / f"pytest-{os.getpid()}"
_WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = str(_WORKSPACE_TMP)
os.environ["TEMP"] = str(_WORKSPACE_TMP)

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class _PlainTempPathFactory:
    """不依赖 pytest 内建 0o700 mkdir 的临时目录工厂。"""

    def __init__(self, base: Path) -> None:
        self._base = base
        self._counter = 0

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        self._counter += 1
        name = f"{basename}-{self._counter}-{uuid.uuid4().hex[:8]}" if numbered else basename
        path = self._base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def getbasetemp(self) -> Path:
        return self._base


@pytest.fixture(scope="session")
def tmp_path_factory():
    return _PlainTempPathFactory(_WORKSPACE_TMP)


@pytest.fixture
def tmp_path(tmp_path_factory: _PlainTempPathFactory) -> Path:
    return tmp_path_factory.mktemp("case")


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch):
    """所有测试默认使用隔离的临时数据目录。"""
    data_dir = tmp_path / "app-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOCIAL_SECRETARY_DATA_DIR", str(data_dir))
    yield data_dir
