"""脚本 / 入口文件的静态约定。

这两条都是踩过的坑，成本很高（会让用户侧的构建命令直接语法错误），所以固化成测试：

1. ``.ps1`` / ``.cmd`` 必须是**纯 ASCII**：Windows PowerShell 5.1 会把无 BOM 的 ``.ps1``
   按 ANSI 解码，中文注释一旦出现就变乱码，甚至吞掉换行把代码粘连到注释里；
2. ``build_desktop.ps1`` 不使用**反引号续行**：反引号后多一个空格、或文件被 ANSI 解码，
   都会让后续参数行变成新语句（报 "Missing expression after unary operator '--'"）；
   参数改用数组传递更稳。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: 需要保持纯 ASCII 的脚本
SCRIPTS = [
    ROOT / "run_web.cmd",
    ROOT / "run_desktop.cmd",
    ROOT / "run_web.ps1",
    ROOT / "build_desktop.ps1",
    ROOT / "scripts" / "make_shortcut.ps1",
]


def test_expected_scripts_exist() -> None:
    missing = [path.relative_to(ROOT).as_posix() for path in SCRIPTS if not path.is_file()]
    assert not missing, f"缺少脚本：{missing}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_ascii_only(path: Path) -> None:
    data = path.read_bytes()
    offenders = [byte for byte in data if byte > 127]
    assert not offenders, (
        f"{path.name} 含 {len(offenders)} 个非 ASCII 字节："
        "PowerShell 5.1 会按 ANSI 解码，中文注释会导致乱码甚至语法错误"
    )


def test_build_script_avoids_backtick_continuation() -> None:
    lines = (ROOT / "build_desktop.ps1").read_text(encoding="utf-8").splitlines()
    offenders = [index + 1 for index, line in enumerate(lines) if line.rstrip().endswith("`")]
    assert not offenders, f"build_desktop.ps1 第 {offenders} 行使用了反引号续行，请改用参数数组"


def test_desktop_entry_points_documented() -> None:
    """README 必须写清桌面端的启动方式（免打包），避免只有打包一条路可走。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for token in ("run_desktop.cmd", "python -m picsaver desktop", "make_shortcut.ps1", "--smoke"):
        assert token in readme, f"README 缺少桌面端启动说明：{token}"
