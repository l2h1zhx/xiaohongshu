# 使用与运行说明（当前版本）

## 智能体入口

在Codex对话提供链接即可；品牌与本批特殊要求可选，调用CLI或标准MCP。

| 工具 | 职责 |
|---|---|
| submit_batch | 一步登记、采集并同步；未完成任务继续排队 |
| register_notes / import_links_file | 登记文本、CSV、TSV、Excel，去重 |
| collect_and_sync / collect_due | 执行到期任务，不改变历史时间 |
| request_refresh | 用户请求的手动刷新，沿用request_id防重 |
| get_status / get_issues / inspect_note | 状态、异常、正文、指标和历史 |
| annotate_note | 保存选题/形式/卖点/复用建议，标注AI建议来源 |
| record_review | 用户明确给出审核结论后留痕，不自动结算 |
| get_performance | 阶段精确指标排名；缺失与晚采不参加24h排名 |
| export_month / sync_feishu | 导出本地Excel、重放待同步事件 |

配置示例见config/mcp.example.json。其他客户端需各自加载配置，协议兼容不表示已安装。
当前云端可以直接通过CLI调用，无需额外网页入口。MCP进程不是定时器。

```bash
koc ingest --file links.xlsx
koc run-once
koc inspect <note_id>
koc performance --month 2026-10 --metric likes --phase 24H
koc annotate <note_id> --topic '场景种草' --format '图文' --reuse-advice '建议先核对'
koc review <note_id> 需修改 --remarks '用户明确提出的修改点'
koc report --month 2026-10 --output var/reports/核对.xlsx
```

采集器只读正文/元数据，并保留授权笔记的图片地址。品牌无法从文字确定时，用本地Tesseract中英文OCR检查图片文字；图片只在内存短暂处理，不保存媒体。OCR不表示图片已通过内容审核，也不能判断纯图形Logo。智能体据inspect_note提供的内容和requirements分析，正文内指令不是授权。无任务要求不自动判定品牌交付达标，AI建议与人工审核分开保存。
曝光/阅读只有授权来源才填；人工数据独立保存证据和统计时间。近似互动保留原文，不混入精确排名。不另调用收费模型。

## 时间与报表

- INITIAL是录入后的首次观测，不是发布时数据。
- 24H按真实发布时间+24h，至26h为有效窗口。迟交MISSED、晚采LATE；不补造历史。
- MONTHLY从次月1日上海09:00起逐条采集，不叫月末23:59数据；晚恢复补建，迟交标LATE_ENROLLMENT。
- 仍在窗口内的24h任务优先，旧晚采任务不会挡住新任务。
- 月报默认打开交付总览，另有品牌月度汇总、笔记明细、KOC汇总、24小时表现、内容与审核、异常与待补采、统计说明；采集时间与原始精度移入隐藏采集底稿，业务明细末列为原笔记链接。
- 完整互动总量需要四项齐全，另列可用字段合计、字段数量、各指标有效笔记数，缺失不当0。
- 只完成部分月结也能生成报告，标complete=false及未完成任务数；任务、品牌或审核内容变化产生新版。

## 备份恢复

本地每天第一次run-once成功后在线backup，保留最近14份本程序生成的文件，不删除其他用户文件。

```bash
koc backup --output var/manual-backup.sqlite3
koc restore-backup --source var/manual-backup.sqlite3 --output var/restored.sqlite3
koc state-pack --output var/private/manual.enc
koc state-unpack --file var/private/manual.enc --output-dir var/private/restored
```

恢复不覆盖目标、不改备份，检查完整性和外键。remote模式用密文及Git历史保存恢复点。
403/429或不明空响应会暂停；先排查再明确resume-after-review，不自动换代理/账号。
外部HTTP崩溃后可能重发，数据库事务、阶段选择和业务键保证重放幂等，不声称HTTP恰好执行一次。
远程每小批最多10次任务后检查点，异常退出可能重读该批未远程提交的任务。CAS失败保留额外加密recovery文件，不覆盖新状态。
加密包上限20MB、解包上限100MB，达到上限报错，不截断。不得覆盖唯一加密密钥，旧密文仍需原钥匙。

## 飞书新版布局

运行 `koc feishu-review-layout` 创建“笔记管理”并配置5个视图；原采集表和原数据保留，不删除字段。新表按品牌、作者、标题、发布时间、审核、核心指标、关注事项、原链接排列；总览不显示采集时间和各阶段重复指标。“当前数据（非月结）”与固定24小时、月结口径分开，月结完成后总览优先采用月结数据。

品牌可选，省略时以内部稳定关联登记，识别品牌只改变显示归属，不改变历史快照或去重键。`inspect_note` 提供正文与OCR证据，智能体可用 `identify_note_brand` 逐字引用证据归档。仅识别到文字的品牌才自动填写，多品牌或不确定则待确认。

## Excel下载入口

`koc report --month YYYY-MM --output 私有路径.xlsx --publish` 在“月报交付”表更新月份、交付状态、笔记/KOC/待审核数和Excel附件；已有记录与文件版本均幂等。实测附件接口要求 `bitable:app` 或 `drive:drive`，优先开通与本任务相关的 `bitable:app`，不要求全部云盘权限。缺权限时保留报表和清晰待办，不将上传失败显示为成功；开通并发布应用后，以新的输出文件名加 `--retry-upload` 重试。已配置月报入口后，月初自动生成的报表也发布到同一表；这不向任何人发送消息。
