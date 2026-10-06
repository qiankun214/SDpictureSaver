"""pytest 全局配置：把 src 加入导入路径，使测试无需安装即可运行。"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
