# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：pictureSaver2 桌面端（单文件 windowed）。

用法::

    python -m PyInstaller --noconfirm --clean packaging\\picsaver-desktop.spec
    # 或直接：.\\build_desktop.ps1

只保留必需项：

- 入口 ``src/picsaver/desktop/__main__.py``（内部用绝对导入，可直接作为脚本入口）；
- ``src/picsaver/web`` 必须放进包的 ``picsaver/web``，冻结后
  ``api/app.py`` 的 `Path(__file__).parent.parent / "web"` 才能命中真实静态资源；
- uvicorn 的 loop / http / websockets / lifespan 实现是**动态导入**的，必须显式声明；
- PySide6 / QtWebEngine 交给官方 hook 自动收集（不额外声明，减少配置面）。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
SRC = ROOT / "src"
ICON = SRC / "picsaver" / "desktop" / "icon.ico"

HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "sse_starlette.sse",
]

a = Analysis(  # noqa: F821
    [str(SRC / "picsaver" / "desktop" / "__main__.py")],
    pathex=[str(SRC)],
    datas=[
        (str(SRC / "picsaver" / "web"), "picsaver/web"),
        (str(ICON), "picsaver/desktop"),
    ],
    hiddenimports=HIDDEN_IMPORTS,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pictureSaver2",
    console=False,  # 窗口模式：不弹控制台；排错看 %LOCALAPPDATA%\pictureSaver2\desktop.log
    # 不弹 PyInstaller 自带的错误对话框：自动化（--smoke）时它会挂住进程；
    # 异常由 desktop/main.py 顶层捕获后写日志 + 弹我们自己的中文对话框。
    disable_windowed_traceback=True,
    icon=str(ICON),
    upx=False,
)
