# conti-agent Windows exe 打包

## 0. 打包模式决策（已定，长期有效）

**正式发布一律 onefile 单文件**（`dist\conti-agent.exe`）。曾试验过
onedir（启动稍快、杀毒误报少），但单文件才是产品形态：一个 exe 拷到
任何工作目录即可运行，不要求用户搬运目录结构。onedir 已从构建脚本移除，
不要再改回。若未来因启动性能重新评估，必须先更新本节和 IMPLEMENTATION.md。

## 1. 目标

发布版只要求用户拿到一个可执行文件。配置二选一：

```text
# 方式 A（推荐）：全局配置，任何目录都能运行
~/.conti-agent/config.toml        # providers / runtime / profiles
~/.conti-agent/config.local.toml  # 可选：密钥等本地覆盖

# 方式 B：项目级配置（可覆盖全局）
workspace/
  conti-agent.exe
  .conti/
    config.toml
    config.local.toml   # 密钥放这里，会覆盖全局同名 provider
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
5. 输出 `dist\conti-agent.exe`；
6. 自动复制一份到项目根目录（dist 的上一级）`conti-agent.exe`，
   已加入 .gitignore 不入库；根目录副本被占用时仅警告、不阻断构建。

## 3. 使用

### 最简对话

```powershell
cd D:\path\to\workspace
.\conti-agent.exe
```

配置解析顺序（后者覆盖前者）：`~/.conti-agent/config.toml` → `~/.conti-agent/config.local.toml` → `.conti/config.toml` → `.conti/config.local.toml`。全局配置存在时，无需任何项目级 `.conti`。

本地直用配置：

```toml
[[provider]]
name = "deepseek"
protocol = "openai-compat"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
api_key = "你的本地 API Key"

[runtime]
permission_mode = "workspace"
```

`.conti/config.local.toml` 已被 `.gitignore` 排除。

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
2. Git 内示例仍使用 `api_key_env`；Git 外本地配置可使用 `api_key`；
3. 当前 exe 未签名，SmartScreen 可能提示；
4. 官方发布应附 SHA-256 校验值。
