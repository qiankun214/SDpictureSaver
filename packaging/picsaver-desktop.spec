# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：pictureSaver2 桌面端（**单文件 windowed**）。

用法::

    python -m PyInstaller --noconfirm --clean packaging\\picsaver-desktop.spec

要点：

- 入口是 ``src/picsaver/desktop/__main__.py``（内部用绝对导入，可直接作为脚本入口）；
- 必须把 ``src/picsaver/web`` 放到包的 ``picsaver/web`` 下，冻结后
  ``api/app.py`` 里 `Path(__file__).parent.parent / "web"` 才能命中真实静态资源；
- uvicorn 的 loop / http / websockets / lifespan 实现是**动态导入**的，必须显式声明；
- PySide6 走官方 hook，另显式声明 QtWebEngine 相关模块以便收集
  ``QtWebEngineProcess.exe`` 与 ``qtwebengine_resources.pak`` 等资源。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
SRC = ROOT / "src"
ICON = SRC / "picsaver" / "desktop" / "icon.ico"

HIDDEN_IMPORTS = [
    # uvicorn 运行时动态导入的实现
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # SSE（后端 /api/jobs/{id}/events 使用）
    "sse_starlette.sse",
    # QtWebEngine 窗口
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]

EXCLUDES = [
    # 桌面端不需要的模块，减小体积
    "pytest",
    "unittest",
    "pydoc",
    "tkinter",
    "setuptools",
    "pip",
    "wheel",
]

a = Analysis(  # noqa: F821
    [str(SRC / "picsaver" / "desktop" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        (str(SRC / "picsaver" / "web"), "picsaver/web"),
        (str(ICON), "picsaver/desktop"),
    ],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pictureSaver2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 窗口模式：不弹控制台；排错看 %LOCALAPPDATA%\pictureSaver2\desktop.log
    # 不弹 PyInstaller 自带的错误对话框：自动化（--smoke）时它会挂住进程；
    # 异常由 desktop/main.py 顶层捕获后写日志 + 弹我们自己的中文对话框。
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)
