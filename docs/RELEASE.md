# 发布检查清单

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

## 发布

1. 确认 `pyproject.toml` 版本为 `0.1.0`。
2. 创建 tag：

```bash
git tag v0.1.0
```

3. 推送主干和 tag：

```bash
git push origin main
git push origin v0.1.0
```
