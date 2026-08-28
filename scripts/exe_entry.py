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
    # 管道输入按 UTF-8 解码；否则按系统 ANSI 代码页解码会产生代理字符，
    # 写入会话账本时直接崩溃。
    if sys.stdin is not None and hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

from conti_agent.cli import main


if __name__ == "__main__":
    main()
