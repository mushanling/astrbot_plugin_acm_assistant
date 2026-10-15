# AstrBot ACM Assistant

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件接口的第一阶段原型，仅实现 `/最近比赛`。不包含机器人本体、定时推送、订阅或账号查询。

## 安装

在 AstrBot 的插件管理页面，使用本仓库 URL 安装：

```text
https://github.com/mushanling/astrbot_plugin_acm_assistant
```

仓库当前为私有仓库，普通 URL 安装需要先具备访问权限。也可先在已登录 GitHub 的本机克隆，然后将插件目录复制到运行 AstrBot 的 `data/plugins/` 下，重启或重载插件。无需修改 AstrBot 本体。

需要 Python 3.10+、AstrBot 4.16+（4.x）。插件依赖见 `requirements.txt`，由 AstrBot 插件安装流程安装；手动复制时需在 AstrBot 使用的 Python 环境中安装这些依赖。

## 配置

登录 [CLIST API 文档页面](https://clist.by/api/v4/doc/) 获取 API Key，在 AstrBot 插件配置中填写 `clist_username` 与 `clist_api_key`，保存后重载插件。API Key 使用密码字段显示，配置由 AstrBot 保存在插件目录之外；不要将实际凭据提交到 GitHub。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `clist_username` | 空 | CLIST 用户名 |
| `clist_api_key` | 空 | CLIST API Key |
| `upcoming_days` | 7 | 未来 1–30 天 |
| `max_contests` | 10 | 合并后显示最早的 1–20 场 |
| `cache_ttl_seconds` | 900 | 缓存 300–3600 秒 |

没有 CLIST 凭据时仍可查询 Codeforces，AtCoder、牛客会明确提示未配置。

## 使用

```text
/最近比赛
```

通过 AstrBot 配置的命令唤醒前缀调用；默认前缀通常为 `/`。只查询尚未开始、开始时间位于未来指定天数内的比赛，不包含正在进行或已结束的比赛。三个平台合并、去重、按开始时间排序，显示平台、标题、北京时间（固定 UTC+8）、时长和比赛链接。命令直接回复文本，不使用 LLM。

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

按需求不提供或运行测试，不启动 AstrBot、不接入 QQ、不发送机器人消息。仅进行 Ruff 格式化与静态检查、Git 提交内容审阅；真实 API 凭据、QQ 命令触发与实际消息展示需安装后联调。
