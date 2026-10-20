# AstrBot ACM Assistant

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 ACM 助手插件，支持全部平台及单个平台的近期比赛查询与周赛管理。不包含机器人本体、定时推送、订阅或账号查询。

## 安装

推荐从 [Releases](https://github.com/mushanling/astrbot_plugin_acm_assistant/releases) 下载对应版本的 `astrbot_plugin_acm_assistant-vX.Y.Z.zip`，在 AstrBot 插件管理中使用本地 ZIP 上传安装，无需手动解压。仓库为私有时，下载需要登录有访问权限的 GitHub 账户；下载后上传 ZIP 不需要 AstrBot 连接私有仓库。

在 AstrBot 的插件管理页面，使用本仓库 URL 安装：

```text
https://github.com/mushanling/astrbot_plugin_acm_assistant
```

仓库当前为私有仓库，普通 URL 安装需要先具备访问权限。也可先在已登录 GitHub 的本机克隆，然后将插件目录复制到运行 AstrBot 的 `data/plugins/` 下，重启或重载插件。无需修改 AstrBot 本体。

需要 Python 3.10+、AstrBot 4.16+（4.x）。插件依赖见 `requirements.txt`，由 AstrBot 插件安装流程安装；手动复制时需在 AstrBot 使用的 Python 环境中安装这些依赖。

## 版本打包

每次更新 `metadata.yaml` 中的版本并推送同名 Git 标签（例如 `v0.1.1`），GitHub Actions 会自动打包并将 ZIP 附加到对应的 GitHub Release。标签必须与元数据版本一致，否则停止发布。可在仓库 Actions 页面查看执行状态或重跑失败任务。流程仅打包，不运行插件测试。

也可在本地直接生成当前版本安装包，无需额外安装打包依赖：

```powershell
py -3.12 scripts/package_plugin.py
```

输出在 `dist/astrbot_plugin_acm_assistant-vX.Y.Z.zip`。压缩包内只有一个 `astrbot_plugin_acm_assistant/` 目录，包含 `main.py`、`training.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` 和 `README.md`，不包含 Git 元数据、工作流、凭据、测试或 SQLite 数据库。后续添加运行所需文件时，应同步更新 `scripts/package_plugin.py` 的打包文件列表。

## 配置

登录 [CLIST API 文档页面](https://clist.by/api/v4/doc/) 获取 API Key，在 AstrBot 插件配置中填写 `clist_username` 与 `clist_api_key`，保存后重载插件。API Key 使用密码字段显示，配置由 AstrBot 保存在插件目录之外；不要将实际凭据提交到 GitHub。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `clist_username` | 空 | CLIST 用户名 |
| `clist_api_key` | 空 | CLIST API Key |
| `max_contests` | 10 | 每条指令显示最早的 1–20 场 |
| `cache_ttl_seconds` | 900 | 缓存 300–3600 秒 |
| `plugin_admins` | `[]` | 插件独立管理员的平台用户 ID 列表，如 `["123456789"]` |

没有 CLIST 凭据时仍可查询 Codeforces，AtCoder、牛客会明确提示未配置。

## 使用

| 指令 | 简写 | 查询范围 |
| --- | --- | --- |
| `/最近比赛` | — | 所有平台，未来 3 天 |
| `/牛客比赛` | `/牛客` | 牛客，未来 5 天 |
| `/atc比赛` | `/atc` | AtCoder，未来 5 天 |
| `/cf比赛` | `/cf` | Codeforces，未来 5 天 |

单平台指令仅请求该平台，不查询其他平台，也不会显示其他平台的错误提示。查询窗口固定为上表天数；旧版本的 `upcoming_days` 配置不再生效，升级后无需手动修改原配置文件。

通过 AstrBot 配置的命令唤醒前缀调用；默认前缀通常为 `/`。只查询尚未开始、开始时间位于未来指定天数内的比赛，不包含正在进行或已结束的比赛。三个平台合并、去重、按开始时间排序，显示平台、标题、北京时间（固定 UTC+8）、时长和比赛链接。命令直接回复文本，不使用 LLM。

## 周赛系统

```text
/本周周赛
/周赛 添加 https://atcoder.jp/contests/abc480
/周赛 添加 https://vjudge.net/contest/600000
/周赛 添加 https://ac.nowcoder.com/acm/contest/95323
/周赛 删除
/周赛 删除 1
```

`/本周周赛` 对所有用户开放，按北京时间（UTC+8）的自然周，即周一 00:00 至下周一 00:00，显示**开始时间在本周**的自动 ABC 与手动登记比赛，包含本周已结束的比赛，按开始时间排序。展示标题、平台、起止时间、链接及登记来源。跨周比赛按开始时间归属；登记未来周的比赛成功后会提示，并在开始当周显示。周赛日程在本插件所有群聊和私聊间共享。

默认按需从 AtCoder 官方比赛列表的即将开始、进行中、近期结束分区同步本周 ABC（`abc数字`），不需要 CLIST 凭据，也不依赖 `/最近比赛` 的查询天数或条数限制。沿用 `cache_ttl_seconds` 缓存时间，失败后至少等待 60 秒重试，最多采用 24 小时旧缓存并提示。同步失败不影响数据库内已登记周赛的显示；没有后台定时任务。

添加命令直接读取公开比赛页面，自动获取标题、开始和结束时间：AtCoder 使用带时区的比赛时间，VJudge 使用 `dataJson` 的 `begin/end`，牛客使用 `window.pageInfo` 的 `startTime/endTime`，后两者按毫秒时间戳解析。无需手动填写时间。链接只接受这三个域名的比赛主页；忽略查询参数和 `#overview` 等片段，统一为 HTTPS 并去重。无法访问、需要登录、时间未公布或解析失败时明确报错，不写入不完整记录；不会绕过登录或验证码。上游页面结构变化时需更新解析逻辑。

管理权限为 **AstrBot 超级管理员或 `plugin_admins` 白名单成员**。使用 AstrBot 事件的 `is_admin()` 识别超级管理员，普通群管理员不会自动获得管理权限。白名单只授予本插件的周赛添加、列出可删除比赛和删除权限，不修改 AstrBot 全局管理员配置。留空时仅超级管理员可以管理；修改白名单后保存并重载插件。

`/周赛 删除` 先同步本周 ABC，再列出所有周已登记且未删除的比赛及编号，自动 ABC 也可删除。编号列表仅对当前操作者、当前会话有效，5 分钟后或插件重载后失效；删除时再次检查权限和记录版本，避免其他管理员操作后误删。发送 `/周赛 删除 <编号>` 完成一次删除后，需重新获取列表。删除 ABC 会持久保留排除记录，刷新和重启不会将它自动加回；需要恢复时可使用添加命令重新登记同一链接。

升级时会在原有 `contests.sqlite3` 中自动新增 `training_contests` 表及索引，保留现有 `source_cache` 缓存。周赛表保存唯一链接、标题、平台、起止时间、自动/手动来源、登记者、删除标记和版本号；数据库仍位于 AstrBot 数据目录，更新插件不会覆盖。

## 数据来源

- **Codeforces**：[官方 `contest.list` API](https://codeforces.com/apiHelp/methods)，`gym=false`，仅采用 `phase=BEFORE` 且已公布开始时间的常规比赛，不需要 API Key。
- **AtCoder**：[CLIST v4 contest API](https://clist.by/api/v4/doc/)，按 `resource=atcoder.jp` 过滤。
- **牛客**：同一 CLIST API，按 `resource=ac.nowcoder.com` 过滤。调研未找到有公开文档保障的牛客竞赛列表 API；[牛客公开 API 文档](https://docs.nowcoder.com/)主要面向招聘笔试业务，因此本阶段使用 CLIST，不依赖网页内部接口。

CLIST 使用 Authorization 请求头鉴权，分页固定访问 CLIST 域名；不会记录凭据、完整异常消息或上游响应正文。CLIST 的收录范围与更新延迟会影响结果，尤其是牛客非公开或未收录比赛。比赛时间以平台最终公布为准。

## SQLite 缓存与失败处理

缓存位于 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_acm_assistant/contests.sqlite3`，不在插件源码内；插件更新不会覆盖缓存。路径通过当前 AstrBot 实际提供的 `astrbot.core.utils.astrbot_path.get_astrbot_data_path()` 获取。

按平台和查询天数分别保存成功结果、成功时间与最近尝试时间。首次调用或缓存到期时按需刷新，没有后台任务；缓存命中时不会请求上游。有效空列表也会缓存。CLIST 额外抓取一天用于覆盖缓存期间滚动的时间窗口，并按页读取；每个平台上限 1000 条，超出时视为刷新失败，不保存不完整快照。

每次 HTTP 请求超时 15 秒；失败后至少间隔 60 秒再尝试，避免群消息连续触发上游请求。同一实例中的命令使用锁串行刷新，各平台独立降级。刷新失败时仅使用距今不超过 24 小时的旧数据，并在回复中标明平台及缓存时间；已开始比赛会再次过滤。没有可用缓存时明确提示该平台不可用，不将查询失败说成“没有比赛”。缓存过期且 CLIST 多页请求时响应会更慢。

网络可使用运行进程的 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` 环境设置。未配置代理时直接访问 API。

## 验证范围

周赛回归测试使用临时 SQLite 数据库与模拟 AstrBot 事件，覆盖页面解析、北京时间周边界、管理员权限、删除编号隔离和失效、ABC 删除后不复活及网络失败降级。不启动 AstrBot、不接入 QQ、不发送机器人消息；真实 QQ 命令触发与实际消息展示需安装后联调。

开发验证：安装 `requirements.txt` 和 `pytest pytest-asyncio ruff` 后运行 `python -m pytest -q`、`ruff check .`、`ruff format --check .`。打包命令见上文。

## 更新记录

- **v0.2.1**：新增 `/牛客比赛`、`/atc比赛`、`/cf比赛`（简写 `/牛客`、`/atc`、`/cf`），查询指定平台未来 5 天的比赛；`/最近比赛` 改为查询所有平台未来 3 天的比赛，取代旧 `upcoming_days` 配置。保留比赛间空行及北京时间显示，并保留 v0.2.0 周赛功能。本次仅静态检查与打包，实际机器人联调待完成。
- **v0.2.0**：新增持久化周赛系统、自动 ABC、AtCoder/VJudge/牛客链接信息采集；管理权限支持 AstrBot 超级管理员与插件独立白名单；加入带有效期、操作者和会话隔离的编号删除流程。
- **v0.1.2**：简化比赛消息标题，移除“北京时间 UTC+8”和消息末尾的数据来源说明；标题与比赛、相邻比赛之间统一空一行。比赛时间仍按北京时间显示。
- **v0.1.1**：修复 AtCoder、牛客 CLIST 请求的排序参数：将无效的 `order_by=start,id` 改为 `order_by=start`，由 CLIST 自动补充 `id` 排序，避免排序字段无效导致的 HTTP 400。更新后保留原配置及 SQLite 缓存；若上次请求失败尚不足 60 秒，请稍后再调用命令。

