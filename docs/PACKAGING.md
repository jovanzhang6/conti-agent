# conti-agent Windows exe 打包

## 1. 目标

发布版只要求用户拿到一个可执行文件和一个本地配置：

```text
workspace/
  conti-agent.exe
  .conti/
    config.toml
```

不需要安装 Python、创建 venv、设置 `PYTHONPATH` 或运行包管理器。

## 2. 构建

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1
```

脚本会：

1. 自动查找当前 venv、项目上级 venv，或读取 `PYTHON` 指定的 Python；
2. 安装 `.[tui]` 和 PyInstaller；
3. 运行全量测试；
4. 构建单文件控制台程序；
5. 输出 `dist\conti-agent.exe`。

## 3. 使用

### 最简对话

```powershell
cd D:\path\to\workspace
.\conti-agent.exe
```

前提是当前目录存在 `.conti/config.toml`。

### 显式配置

```powershell
.\conti-agent.exe --config .\.conti\config.toml ask "检查项目"
```

### 行式兼容

```powershell
.\conti-agent.exe --config .\.conti\config.toml chat --line
```

## 4. 编码

`scripts/exe_entry.py` 会把 Windows 控制台输入/输出代码页设置为 UTF-8，并对 stdout/stderr reconfigure。这样中文回答不会因 OEM 代码页乱码。

## 5. 安全说明

1. exe 只打包运行时代码，不打包 API Key；
2. Key 仍必须通过 `api_key_env` 指向环境变量；
3. 当前 exe 未签名，SmartScreen 可能提示；
4. 官方发布应附 SHA-256 校验值。
