# 可行性验证记录 — 2026-10-23（北京时间）

## 最新进展（以此节为准）

本轮已补齐统一真实工作进程、分析与审核留痕、每日备份和远程加密状态；后续使用CLI/MCP时不再依赖PoC脚本手工落库。
GitHub仓库确认public，已通过实际API初始化加密状态分支并完成恢复/刷新/同步/保存；当前云端.env启用共享模式。
Actions管理/密钥接口403，不能自动设置运行器Secrets；workflow通过actionlint但尚未在GitHub运行器执行。免费定时方案的源站可用性仍需验证。

当前小红书短链已可访问，飞书站点HTTP200。3条用户授权笔记通过**实际XHS-Downloader /xhs/detail路由连续两轮3/3成功**，上游请求均为HTTP200；第二轮每条记录了实际采集时间。新增媒体文件0。
PoC脚本为 `scripts/poc_xhs_real.py`：不修改上游源代码，使用系统CA恢复session证书校验，在实际请求包装层逐跳限制URL且禁止不受控重定向；不传Cookie/代理参数，download=false、check_record=false。仅调用原版详情路由，不启动对公网监听的服务。
真实结果和链接位于 `/workspace/private/koc-poc`，权限受限，不进入Git。已生成真实核对Excel；没有冒充24h或月结历史。
这验证了受控PoC路径，原版默认启动方式仍有下文所列问题，生产门禁保持关闭。飞书凭证已安全绑定，已自动创建base与40字段系统表，3条笔记已实际同步；免费无人值守尚未部署。

## 实际证据

| 检查 | 结果 |
|---|---|
| 项目初始状态 | 仓库仅 README/LICENSE，检出 ea31dbb23121d3dcf8f4c0a4c8aee4c67d5be4bb；未发现已有 AGENTS.md |
| Git 读取 | origin 与两个上游仓库 HTTPS 读取成功，未请求新 GitHub token |
| XHS-Downloader | 2.8，SHA `2ab2620b0dd35b04530e70f1e9c7ab3f78c5d6c5`，GPL-3.0，Python >=3.12 |
| 依赖 | 上游 uv.lock 固定的镜像域名被拒绝；用 `uv export --frozen --no-dev --no-emit-project` 保留原哈希，再由官方 PyPI 执行 `uv pip install --require-hashes`，安装成功；未改上游依赖或锁文件 |
| 本机 API 合约 | FastAPI TestClient 读取实际 OpenAPI；`/xhs/detail` 存在；无 token 返回403；合法本地 token + 无效链接返回200，JSON 键为 data/message/params，data=null |
| 真实笔记 | 首次代理403；最新复测两轮均3/3成功，返回作者、发布时间、标题、正文、话题和可用互动指标。曝光/阅读未取得，部分互动字段为未知 |
| 飞书官方 MCP | `larksuite/lark-openapi-mcp` SHA `21920354ec6e3966b52e89152620c5085e496b55`，包0.5.1，MIT，Node>=20；实际安装并通过 CLI `--help` 启动检查，未登录或读写真实飞书 |
| 官方 MCP 运行修复 | CLI 默认状态目录在当前沙箱不可写；设置 XDG_STATE_HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME 为 /workspace 下路径后成功，无需修改 HOME |
| 飞书认证 | 当前官方 MCP 工具目录仍有 auth.v3.auth.tenantAccessTokenInternal；采用该实际已列出的自建应用接口，不盲用原文档声称的新 OAuth 端点 |
| 自建 MCP | 使用官方 Python MCP SDK 通过独立 stdio 进程完成初始化、工具枚举、登记和安全门禁调用；不是只检查函数存在 |

本机 API 检查使用的是上游实际路由和源码，**不是成功笔记响应样本**。解析器测试数据明确为依据源码构造的合约样本，后续仍需真实脱敏 Fixture。

## 上游安全与能力缺口

- `source/module/manager.py` 的 request_client/download_client 默认 `verify=False`；`source/application/request.py` 的按请求 Cookie 路径也关闭证书校验。未经完整修复与复核不能启用真实采集。
- 本地合约探测在任何网络请求前把两个 session.verify 设置为 True，未使用 Cookie 路径，也没有访问小红书；不据此宣称所有路径已安全。
- API 默认 host=0.0.0.0，当前版本还新增 Bearer 鉴权。不能照旧文档直接启动并暴露端口。
- 上游可吞掉网络异常并返回 data=null，因此本适配器保守标记 UPSTREAM_EMPTY_UNCLASSIFIED 并暂停，不把空响应认作删除。
- 已检查的 Explore 输出四项互动、作者、标题、正文、话题和时间戳；没有真实曝光、阅读、图片数量和视频时长的已验证合约。这些字段不能假设可得。
- 公开安全 Issue 页面/API 在当前网络策略下尚未完成审查，不声称通过完整安全审计。

## 免费无人值守的实际边界

已读取 GitHub 官方 docs 源码：
- [触发事件](https://github.com/github/docs/blob/main/content/actions/reference/workflows-and-actions/events-that-trigger-workflows.md)：schedule 只在默认分支运行；公共仓库60天无活动可能停用。
- [延迟说明](https://github.com/github/docs/blob/main/data/reusables/actions/schedule-delay.md)：高负载可能延迟，甚至丢弃排队任务；避开整点可降低风险，不能消除。
- [费用](https://github.com/github/docs/blob/main/content/billing/concepts/product-billing/github-actions.md)：公共仓库标准运行器免费；私有仓库依账号配额，超额可能收费。当前未取得账号实际额度与预算配置。

候选频率：每小时错峰唤醒，到期任务按实际时间执行，窗口24–26h；每月1日上海09:00后创建月结。**尚未启用 schedule。**

必须先解决：
1. 运行器能否在正常访问权限下获取授权笔记；不能绕过平台风控。
2. SQLite 与备份在运行器之间可靠持久保存。Actions cache 可能被驱逐，artifact 有保留期限，二者不能无条件充当唯一数据库。
3. 智能体提交与定时机器共享同一业务状态及互斥，不能两个互不同步的 SQLite 各自运行。
4. 仓库可见性与隐私；不能把明文业务库、完整带参数链接或密钥提交到公共 Git 历史。
5. 启用前确认仅使用免费资源、免费额度和禁止超额支出的设置；未确认前不激活真实工作流。

当前 `.github/workflows/ci.yml` 只有手动测试触发器，不执行真实采集、不带 cron，尚未推送或在 GitHub 执行。

## 外部阻塞与下一步

- 网络域名草稿已保存；当前访问已成功，不能再把网络403列为当前阻塞。仍无代理发布工具，不声称已完成环境版本发布或新任务恢复验证。
- 当前只有 GH_TOKEN 名称存在，未发现 FEISHU_* 或 XHS_* 绑定；未输出任何变量值。
- 真实飞书需安全配置 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_BITABLE_APP_TOKEN、FEISHU_TABLE_NOTES_ID，完成应用 scope 与表格访问授权。
- 3条测试链接未注明业务品牌，本次只作授权PoC，核对表标为待确认品牌，未猜测品牌或登记为正式投放。
- 云端是否达到足够成功率须在放通网络并修复安全路径后测量；先3条连通，再20–30条授权样本，不能预先承诺成功率。
