# 发布检查清单

## 版本判定

`v0.1.0` 只有在真实模型端到端通过后才能发布。仅文档提交、fake provider 冒烟或单元测试通过，都不能作为发布依据。

发布 tag 必须指向包含真实模型闭环的提交。旧 tag 如果指向早期文档基线，应删除后重新指向最新验收提交。

## 代码质量

1. 运行全量测试：

```bash
python -m unittest discover -s tests
```

2. 确认工作区干净：

```bash
git status --short
```

3. 确认提交历史按阶段可读：

```bash
git log --oneline
```

4. 运行密钥格式扫描：

```bash
git grep -nE "sk-[A-Za-z0-9]{16,}" -- .
```

5. 确认没有命中。

## 安全检查

1. 配置中没有明文 API Key。
2. 示例只引用 `api_key_env`。
3. 权限拒绝会被审计。
4. Hook 失败默认拒绝。
5. 文件和进程工具不能越过工作区边界。
6. 服务默认绑定 `127.0.0.1`。

## 文档检查

1. README 说明安装、配置、使用和状态。
2. 功能规格与实现一致。
3. 架构文档解释数据流。
4. 学习路线可执行。
5. 文档和注释均为中文。

## 真实端到端检查

按 [`docs/E2E_TESTING.md`](E2E_TESTING.md) 完成：

1. 真实模型 `ask`；
2. 真实模型 `chat` 流式多轮；
3. `workspace_read`；
4. `workspace_write`；
5. `read_only` 写入拒绝；
6. Hook 拒绝；
7. 外部 `docs.echo`；
8. 危险命令拒绝；
9. JSONL 事件；
10. 配置错误退出码；
11. CLI 退出无子进程泄漏警告。

当前记录：

```text
自动化测试：40 passed
真实 ask：passed
真实 chat：passed
workspace_read：passed
workspace_write：passed
read_only deny：passed
hook deny：passed
external docs.echo：passed
dangerous command deny：passed
secret scan：passed
```

## 发布

1. 确认 `pyproject.toml` 版本为 `0.1.0`。
2. 确认 tag 未指向旧文档提交；如果已指向旧提交，先删除：

```bash
git tag -d v0.1.0
```

3. 在最新验收提交上重建 tag：

```bash
git tag v0.1.0
```

4. 推送主干和 tag：

```bash
git push origin main
git push origin v0.1.0
```
