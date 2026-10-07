"""``python -m picsaver.desktop``：桌面端入口。

这里用**绝对导入**：既能在包内以 ``-m`` 运行，也能直接作为 PyInstaller 的入口脚本。
"""

import sys

from picsaver.desktop.main import main

if __name__ == "__main__":
    sys.exit(main())
