"""PyInstaller 入口；必须通过绝对导入加载包。"""

import sys


# Windows 控制台默认代码页通常不是 UTF-8，打包后中文输出必须显式固定。
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

from conti_agent.cli import main


if __name__ == "__main__":
    main()
