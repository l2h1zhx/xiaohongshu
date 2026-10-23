# 使用、接入与恢复

> 当前完整运行入口、共享状态、默认真实采集方式以 [system.md](system.md) 和 [automation.md](automation.md) 为准。下文保留早期本地/外部API适配方式。默认CLI已改为受控子进程，不需要用户启动原版API或填写本地API令牌。

## 本地运行

工作目录为项目 checkout。Python3.12，依赖用 `uv sync --frozen`。
没有 `.env` 也可运行登记/状态/导出；需要真实连接时，通过安全环境变量注入。
如使用本地 `.env`，可执行 `uv run --env-file .env --frozen koc ...`；程序不会假定 `.env` 自动加载。

```bash
uv run --frozen koc ingest --brand '品牌A' --text '用户提供的链接文本'
uv run --frozen koc ingest --brand '品牌A' --file /path/links.xlsx
uv run --frozen koc status
uv run --frozen koc issues
uv run --frozen koc report --month 2026-10 --brand '品牌A' --output var/reports/品牌A-2026-10.xlsx
```

输入文件支持 UTF-8 文本和 xlsx；xlsx 提取文本链接与单元格超链接，不执行公式。
重复 URL 不重复登记，短链解析后与长链按 Note ID 共用读数。未解析提交会出现在异常页，不能静默丢弃。
品牌可省略，系统根据正文/话题与图片OCR证据识别，证据不足或多个品牌时标待确认。任务要求可用 `--requirements` 保存；当前确定性检查覆盖字段缺失/数据变化，具体内容语义可通过智能体按任务要求审阅，图片OCR仅辅助品牌文字识别，不等于画面内容审核；不自动理解视频。

## 演示（独立数据库、虚构数据）

```bash
uv run --frozen koc --db var/new-demo.sqlite3 demo --output var/new-demo.xlsx
```

demo 拒绝使用非空业务库；不要把演示表当成真实报告。重复运行需换新的数据库和输出文件名，程序不会覆盖已有报表。

## 真实只读采集的门禁

固定版本的上游及独立虚拟环境现在保存在 `/workspace/dependencies/XHS-Downloader`，由 `bash scripts/install_xhs.sh` 可重复安装，保留上游锁文件哈希并从官方PyPI取包。
环境重建后 `/tmp` 可能清空，不能依赖早期PoC的 `/tmp/koc-xhs-upstream` 路径；当前安装脚本不会启动默认上游API。

启用前须完成 feasibility.md 中的上游 TLS/鉴权/绑定地址核查，并验证本机真实接口。
`XHS_READONLY_REVIEWED=true` 只是人工核验后的门禁，不会自动修复第三方程序。
API 必须仅监听127.0.0.1；使用本地生成的 XHS_API_TOKEN，不能在日志或聊天中展示。
本项目仅调用 POST /xhs/detail，强制 download=false、check_record=false，不提供 Cookie、代理、发布、点赞或媒体下载工具。

```bash
uv run --frozen koc tick --limit 50
uv run --frozen koc refresh <note_id> --request-id <本次操作UUID>
```

默认并发1、采集间隔至少5秒；超时/网络失败最多3次总尝试。
403/429/无法解释的空上游结果会暂停后续请求，先排查原因，再明确执行 `resume-after-review`，不能自动切换账号/代理。
重启恢复有租约超时，网络请求在进程崩溃时可能再次发生；仅保证已落库阶段快照和任务幂等，不声称外部 HTTP 恰好执行一次。

24h 任务从实际发布时间计算，窗口[24h,26h]；超过24h才首次提交为MISSED，晚执行保存LATE且不写准时KPI。
月结每次运行检查已到期月份，2号恢复也会补建1号漏掉的批次；次月迟交单独标LATE_ENROLLMENT。
所有快照实际时间不可改。不同任务、人工数据互不覆盖；月底笔记可能先月结后24h。

## 飞书

1. 创建自建应用，授予多维表格记录读取/写入与字段读取所需最小权限。
2. 在目标多维表格共享设置中允许应用访问，API scope不能代替文件权限。
3. 新建一个空白多维表格，并将App ID、Secret及base的app_token填写到环境安全设置。
4. `feishu-init-table` 创建系统表和字段并返回table_id；已有同名表则验证复用。将返回值配置为FEISHU_TABLE_NOTES_ID，无需手工创建几十列。
5. `feishu-diagnose` 先检查鉴权、分页和字段类型，再执行 `feishu-sync`。详细网页端步骤见feishu-setup.md。

只写 schema 中的系统字段，人工审核列保持独立。schema变化会停止同步。成功采集先落SQLite再进入outbox，飞书失败不重复抓小红书。
出站同步最多3次失败后阻塞，恢复权限/网络后由 `resume-after-review` 显式重置。写入超时重放先查业务键，且发送稳定client_token。
历史快照当前只持久保存在SQLite；飞书展示当前阶段值，不能把飞书主表当完整历史备份。
已通过应用身份自动创建线上base和40字段系统表，3条用户授权笔记已写入且读回。没有给KOC或他人发送消息。
实际飞书接口要求client_token为UUID4，现为每个业务键生成并持久保存UUID4，重放沿用；数值字段读回可能是数字字符串，验证时按精确数值比较，缺失仍单独处理。

## MCP接入

`uv run --frozen koc mcp` 是标准stdio服务器，无公网端口。不同工具需要各自加载配置，不会自动安装到所有客户端。
Codex配置示例（按实际路径调整，放到其MCP配置中；不包含密钥）：

```toml
[mcp_servers.koc_insight]
command = "/workspace/xiaohongshu/.venv/bin/koc"
args = ["--db", "/workspace/xiaohongshu/var/koc.sqlite3", "mcp"]
[mcp_servers.koc_insight.env]
KOC_REPORT_DIR = "/workspace/xiaohongshu/var/reports"
```

通用JSON客户端：

```json
{"mcpServers":{"koc_insight":{"command":"/workspace/xiaohongshu/.venv/bin/koc","args":["--db","/workspace/xiaohongshu/var/koc.sqlite3","mcp"],"env":{"KOC_REPORT_DIR":"/workspace/xiaohongshu/var/reports"}}}}
```

工具：register_notes、get_status、get_issues、collect_due、request_refresh、export_month、sync_feishu。
标准客户端握手已测试，但未宣称当前Codex界面或DeepSeek/WorkBuddy已实际加载；本对话也可通过CLI调用同一核心。
不同机器上的MCP必须共享可靠的业务状态，不能把上述本地路径当作远程共享存储。

飞书官方MCP为可选辅助，不承担本系统任务持久化。其CLI可用以下环境路径避免沙箱外写入：
`XDG_STATE_HOME=/workspace/.local/state XDG_CONFIG_HOME=/workspace/.local/config XDG_CACHE_HOME=/workspace/.local/cache`。
上游包0.5.1仅测试启动帮助，实际认证需单独验证；不要把密钥放在命令行参数或可共享配置中。

## 人工曝光/阅读

使用 `import-manual --file <本地JSON>`。只支持可追溯的授权数据；captured_at为指标统计时间，evidence描述后台导出/截图及其时间。
示例中的数字仅为结构演示：

```json
{"request_id":"唯一操作编号","captured_at":"2026-10-20T10:00:00+08:00","detail":{"note_id":"aaaaaaaaaaaaaaaaaaaaaaaa","source":"manual","counts":{"exposure":123,"reads":100},"evidence":"授权后台导出，统计截至上述时间"}}
```

人工数据独立列出，不冒充24h/月结公开采集值。截图OCR、图片/视频语义识别未实现，也未调用任何收费模型。

## 备份与恢复

```bash
uv run --frozen koc backup --output var/backups/koc-唯一时间.sqlite3
```

使用 SQLite 在线backup API，含完整性检查，不能直接复制运行中的单个.db文件而漏掉WAL。
停止本系统写入后，可用 `koc --db <备份文件> status` 读取并验证备份；恢复到独立位置再次检查后再切换 KOC_DB。
恢复演练已在测试中从备份打开并续跑，验证不产生重复快照。生产定时备份、保留周期和异地存储尚需随真实运行位置落地。
没有外部常驻监控时，机器完全停机无法主动报警；当前心跳和异常在CLI/MCP中查看。
数据库和Excel可能包含项目资料与带参数链接，应限制访问；不上传公共Git，不把密钥或原始完整响应写日志。
