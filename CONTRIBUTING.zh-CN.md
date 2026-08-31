[English](CONTRIBUTING.md) · **简体中文**

# 参与贡献

感谢你关注 SpecStamp。

## 开始之前

请先阅读项目 README、许可证和安全说明，确认提交内容可以公开发布，并且不包含账号、令牌、私钥、真实业务资料或内部验收材料。

先根据内容选择入口：

- 使用问题、流程想法和经验交流：进入 [GitHub Discussions](https://github.com/muyuqingqiu/specstamp/discussions)。
- 可以稳定复现的功能、安装或文档问题：提交 [Issue](https://github.com/muyuqingqiu/specstamp/issues/new/choose)。
- 涉及安全影响的问题：使用 [私密漏洞报告](https://github.com/muyuqingqiu/specstamp/security/advisories/new)。

第一次参与开源项目，可以从带有 [`good first issue`](https://github.com/muyuqingqiu/specstamp/labels/good%20first%20issue) 或 [`help wanted`](https://github.com/muyuqingqiu/specstamp/labels/help%20wanted) 标签的问题开始。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
```

需要生成本机覆盖率报告时执行：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  --cov=codex_sdlc \
  --cov-report=term-missing \
  --cov-report=html
```

完整测试耗时较长。GitHub Actions 每周和手动运行时会把全部测试文件分成四组并行执行，再合并覆盖率数据；普通 Pull Request 仍运行发布安装和公开入口检查，避免每次小改动都等待整套长测试。

如果本地环境没有 `.venv` 或开发依赖，先重新执行安装命令，不要把本机生成的 `.venv`、`.codex-sdlc`、`tmp` 和备份目录提交到仓库。

## 提交代码

- 一个提交只处理一个清晰的改动目标。
- 新增或修改行为时，同时补充对应测试和文档。
- 修改 Schema、CLI 合同或技能时，说明兼容影响。
- 不要提交本机绝对路径、内部任务证据、客户资料或真实秘密。
- 提交前运行完整测试、差异检查和公开测试。

## Pull Request

Pull Request 请说明：

1. 改动解决的问题；
2. 影响的模块、命令或 Schema；
3. 执行过的验证命令和结果；
4. 是否包含用户可见行为变化；
5. 是否需要同步更新文档、技能或变更记录。

## 贡献授权

提交 Pull Request 即表示你有权提交相关内容，并同意按照仓库许可证授权项目使用你的贡献。若你的组织有额外要求，请在提交前先说明。
