# 免费无人值守：部署与边界

## 当前验证

仓库已确认公开，标准ubuntu-latest运行器的公共仓库任务免费，不用付费大规格运行器。
已通过GitHub API建立koc-state分支、加密保存真实台账；当前云端使用共享状态完成恢复、真实刷新、飞书同步和再次保存。
用户已在仓库设置保存两个 Actions Secrets。2026-10-23，GitHub 标准运行器上的核心检查全部通过；真实自动化验证运行39943476863成功，授权笔记补采任务成功生成第6次快照、同步飞书并保存远程密文。随后从云工作区独立恢复核对任务 SUCCEEDED、53个飞书字段正常、outbox为0。
当前 GitHub 连接仍不能管理 Secrets/Variables；运行器可正常读取用户配置的 Secrets。首次验证失败后留下的运行锁已在确认任务结束后清理；重跑验证成功。

## 数据流

```mermaid
flowchart TD
    A[智能体提交或手动调用] --> L[共享CAS租约]
    T[GitHub每小时17分] --> L
    L --> D[解密SQLite与报表]
    D --> P[执行到期任务]
    P --> X[隔离XHS只读进程]
    X --> F[事务保存并同步飞书]
    F --> R[生成到期月报]
    R --> G[加密并CAS保存远程状态]
```

独立随机32字节密钥，AES-256-GCM，不复用飞书凭证。代码分支无数据库、报表或密钥；业务状态在koc-state仅为密文。租约只含随机owner与到期时间。
CAS竞争或租约丢失停止覆盖，提交失败留加密recovery文件。空闲时不反复提交大密文，仍会有小型租约提交。
旧密文保留在Git历史，需要定期检查仓库体积，密钥独立备份。加密键不可随意重新生成覆盖。

## GitHub启用设置

仓库Settings → Secrets and variables → Actions：

| 类型 | 名称 | 内容 |
|---|---|---|
| Secret | FEISHU_APP_SECRET | 同一个飞书应用的真实App Secret |
| Secret | KOC_STATE_KEY | 当前私有密钥文件中的单行内容 |
| Variable（可选） | KOC_AUTOMATION_ENABLED | false 暂停定时任务；未设置时默认启用 |

云环境Secret不自动复制到GitHub，不要上传代理占位符或在聊天发送密钥。
App ID/台账标识已在加密库，恢复后读取；GitHub自带github.token处理同仓库状态，无需新建PAT。
代码已合入默认分支，手动运行KOC scheduled collection已验证源站、同步和状态。手动触发不受定时开关限制；schedule默认启用，可将启用变量设为false暂停。缺少凭证会在采集前失败。
每小时17分检查到期任务，避开整点；GitHub可能延迟/丢弃，按真实时间处理晚采。
job上限55分钟，采集循环45分钟，每轮最多200项，未完下轮继续。500条不是同一时刻完成。

## 云端与回退

忽略的.env保存非秘密路由：KOC_REMOTE_STATE、KOC_GITHUB_REPO、KOC_STATE_KEY_FILE、KOC_UPSTREAM_ROOT、FEISHU_APP_SECRET_FILE。密钥在/workspace/private，不提交Git。

飞书支持通过 FEISHU_APP_SECRET_FILE 读取私有凭证文件；显式设置该路径后优先使用文件，文件不存在、不可读或内容为空时直接报错，不退回其他凭证。当前云工作区已配置私有文件并通过真实飞书验证（53个字段）；这不代表 GitHub Actions Secrets 已配置。GitHub 的 KOC_STATE_KEY 必须填写密钥文件里的单行内容，不能填写云工作区文件路径。
安装用uv sync --frozen和bash scripts/install_xhs.sh；运行用uv run --frozen koc run-once。
安装器支持KOC_UPSTREAM_ROOT，GitHub运行器不需要/workspace或管理员权限。
恢复时若远程状态缺失会报错，不创建空库冒充成功。设置启用变量为false或在Actions页面停用workflow可停止后续job。
私有仓库会被费用前置检查拒绝，避免未经验证的配额/超额费用。
若运行器不能正常访问小红书，保留错误及当前云端手动触发路线，不声称无人值守已上线。
