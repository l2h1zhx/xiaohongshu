# KOC Insight

个人小红书种草运营助手：在Codex等智能体中提交链接，自动登记、检查和跟踪；飞书查看，Excel交付。
真实采集、飞书读写和跨进程加密状态恢复已验证。**Actions定时配置已准备，当前连接无Actions管理权限，尚未启用或在GitHub运行器验证。**

已实现：批量文本/Excel登记、多品牌去重、不可变快照、24–26h观察、月结补偿、手动刷新、重试熔断、飞书outbox、四页Excel和原文链接、stdio MCP、分析留痕、审核记录、阶段排名、每日备份、加密共享状态和CAS租约。
曝光/阅读仅保存有来源的数据；当前公开来源不提供时留空，支持授权人工补录。

```bash
uv sync --frozen
bash scripts/install_xhs.sh
bash scripts/check.sh
uv run --frozen koc --db var/demo-首次.sqlite3 demo --output var/demo-首次.xlsx
uv run --frozen koc --help
```

演示始终使用独立本地数据库，不与真实库混用。正常模式从环境变量及忽略的`.env`读取配置，已有环境变量优先；共享模式统一使用远程密文，不再操作另一份本地业务库。

```bash
uv run --frozen koc ingest --brand '品牌A' --text '用户提供的授权链接'
uv run --frozen koc run-once
uv run --frozen koc report --month 2026-10 --output var/reports/品牌A-10月.xlsx
```

- [完整使用说明](docs/system.md)
- [无人值守与加密状态](docs/automation.md)
- [流程与职责](docs/flow.md)
- [可行性证据、外部阻塞及免费定时方案](docs/feasibility.md)
- [操作、飞书字段、MCP接入、备份恢复](docs/operations.md)
- [实施阶段](IMPLEMENTATION_PLAN.md)

3条用户授权笔记已通过XHS-Downloader实际详情接口连续两轮全部读取成功，未下载媒体；PoC脚本见 scripts/poc_xhs_real.py。这不代表无人值守已上线。
飞书应用凭证已绑定，台账已升级至53个系统字段，已完成真实笔记同步和读回。准备与复用步骤见 [飞书准备](docs/feishu-setup.md)。未开启收费资源，未激活GitHub定时工作流。
第三方项目未嵌入本仓库：XHS-Downloader (GPL-3.0)，飞书官方MCP (MIT)，版本证据见可行性文档。
