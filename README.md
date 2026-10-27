# AList-TVBox T4 Sidecar

当前版本：**0.2.1**。[版本记录](CHANGELOG.md) · [订阅兼容门禁](COMPATIBILITY_GATE.md)。
本地实现与验证不代表 NAS 已升级。
真实订阅 178 条的检测结果和下一轮修复节点见 [兼容基线](COMPATIBILITY_BASELINE.md)；当前总门禁未放行。
已复现的缺依赖、模块加载、请求头及图片检测问题，见 [失败根因与修复方案](FAILURE_ANALYSIS.md)。
M16 已补齐依赖包上传、模块加载、Python 宿主代理、AES-128 检测及任务异常追溯。
第二轮 178 条全订阅报告仍未放行；最终源码的定向复测正常完成，不能替代全订阅重测、
Linux 镜像或 FM 真机验收，详见 [第二轮结果](FAILURE_ANALYSIS.md#第二轮全订阅结果及收尾修复)。

这是一个独立的附属容器，用于把已授权的 AList-TVBox（AT）订阅和 ATVP 插件运行时适配成只读 T4 接口。它不修改 AT 的 JAR、数据库、原始插件或前端文件，适合与 AT 并行部署在 NAS、家庭服务器或独立 Linux 主机上。

## 项目说明

运行链路如下：

```text
T4 客户端
    │  导入 /t4/config.json → type=4 API / 原生 AT 推送
    ▼
T4 Sidecar Gateway
    ├─ 读取 AT 订阅地址
    ├─ 运行 raw Python / secspider 插件
    ├─ 调用 AT 内置服务端接口
    └─ 隔离 Worker、规范化响应、保护 opaque 播放 ID
    │
    ▼
已授权的 AList-TVBox AT 服务
```

Sidecar 只负责 T4 转译和转发，不替代 AT 管理面。AT 登录、Cookie 二维码、媒体订阅管理、播放同步管理和 WebHome 页面能力仍由原版 AT 负责。
Sidecar 不增加额外鉴权：只配置 AT 完整订阅地址，打开 Web 或导入影视订阅即可使用。
AT 订阅和上游接口原有授权照常执行，不会因为取消 Sidecar 鉴权而被跳过。

## 当前能力

- `GET /t4/health`：健康检查，不需要令牌
- `GET /t4/`：中文 Web 管理入口
- `GET /t4/config.json`：统一影视订阅，自动汇总可执行的 AT 和本地 Python/JS 插件
- `GET|POST /t4/fm/{source}/{plugin}/`：影视 type=4 单地址参数入口
- `GET /t4/api/sources`：读取 source 能力快照
- `GET|POST /t4/api/{home|category|search|detail|play|action}`：显式模块 API
- 从 `ALIST_TVBOX_SUBSCRIPTION_URL` 读取 AT 完整订阅并自动建立 `env-at` source
- ATVP raw Python 插件
- 官方格式 `secspider/1` 插件
- 自定义 `secspider/1` 动态 loader 和只读 keyring 回退
- AT 内置源原生桥接：Bilibili、我的追更、AList、小雅、片单、直播、Emby、Jellyfin、飞牛、盘搜、Telegram 豆瓣、Telegram 频道、Telegram 网页
- source 级 Worker 会话、超时回收和 opaque 详情/播放 ID
- Web 上传标准 Python `Spider`、Sidecar JavaScript 插件或包含配套文件的 ZIP，自动注册到 `local-uploaded`
- 本地插件列表：删除、重命名、上移/下移排序、刷新运行实例、首页冒烟、复制 FM 地址
- 订阅快取和并发单飞，避免每个 T4 请求重复拉取、解析 AT 订阅
- 播放链路诊断：请求、AT、Worker、解析、DASH 清单及媒体 Range 转发的关联日志与导出
- 未声明客户端身份的播放请求使用普通视频模式，不再默认冒充支持 DASH 的 FongMi
- 全订阅兼容门禁：插件分类、首页、分类、搜索、详情、图片、解析及媒体字节检测，支持进度、停止和报告导出

内置源的列表、搜索、详情和播放复用 AT 原生服务端。Media 的追更动作通过 `/t4/api/action` 复用 AT `/play`。
片单导航保留标题搜片行为；AT 推送通过统一订阅保留原版客户端实现，不伪装成服务端 T4。
`csp_WebHome` 和 `csp_Nostr` 仍是页面入口，不伪造空 VOD 结果。

## Web 管理界面

Sidecar 自带独立的中文 Web 管理入口，不需要修改 AT 原页面：

```text
http://NAS_IP:8765/t4/
```

页面使用步骤：

1. 打开上面的地址，确认页面右上角健康状态正常。
2. 页面会自动读取容器中的 source 和 AT 订阅配置，不需要填写额外 Token。
3. AT 订阅的插件列表会自动读取。在下拉框选择需要的插件，例如 `csp_BiliBili`、`csp_Media`。
4. 上传的 Python/JS 文件或 ZIP 插件包直接显示在“本地插件”列表，每行都有管理按钮，悬停可查看名称。
5. 直接点击页面顶部的“复制影视订阅地址”，在 FM 影视的点播配置中导入一次，即可显示所有可执行插件。
6. 逐插件的“首页冒烟”和“复制插件 API”用于诊断或已有订阅配置的 `api` 字段，不是整包订阅。

### 在 FM 影视中使用

1. 用 NAS 或电脑的局域网地址打开管理台，例如 `http://NAS_IP:8765/t4/`。
2. 点击顶部的“复制影视订阅地址”。
3. 在影视的“设置 → 配置 → 点播”中粘贴并保存；不同版本菜单名称可能略有不同。
4. 导入后可在站点列表选择 Bilibili、我的追更或已上传的插件，不需要手工填写 source、site 或 JSON。

统一订阅地址固定为：

```text
http://NAS_IP:8765/t4/config.json
```

电视上不能使用电脑的 `127.0.0.1`、`localhost`；这些地址指向电视自己。请通过局域网 IP 打开管理台再复制。
订阅按 AT source 顺序及插件原顺序汇总；本地插件使用已保存的名称和排序。插件 key 与名称无关，
重命名或排序不改变站点身份；新增、删除或改名后在影视中刷新配置即可同步。
统一订阅以 type=4 站点为主；`csp_Push` 作为原生 type=3 站点保留，WebHome 等页面入口仍不导出。
某个已配置 AT source 拉取失败时，订阅明确返回 `502`，不会静默生成缺少站点的订阅。

统一订阅和各站点 API 均无需额外令牌、签名路径或认证请求头。
type=4 站点不下发 AT 原始 ext；原生 AT 推送例外，必须将原订阅的 `jar/ext` 交给影视运行，
其中可能包含 AT 原有授权信息。管理页面和能力快照不展示这些值，完整订阅只应提供给可信客户端。

### 片单导航与 AT 推送

仍然只导入同一个 `/t4/config.json`，无需单独添加推送配置或填写 Token。

| 内置源 | 使用方式 | 实现边界 |
| --- | --- | --- |
| 片单导航 | 浏览豆瓣/TMDB 分类和推荐；FongMi 按标题搜索；忽略 `indexs` 的客户端由 Sidecar 检索其他源并汇总真实播放组 | 自动设置 `indexs=1`；卡片本身不是媒体，播放委托给实际命中的片源 |
| AT 推送 | 在原版 AT 的设备管理中选择已连接的影视设备，沿用原来的推送操作 | 保留 `push_agent/type=3/csp_Push` 和原始 `jar/ext`，由客户端加载 AT `spring.jar` |

更新 Sidecar 后，在影视中刷新一次点播配置即可。Web 插件列表中的 AT 推送标记为“原生推送”，
复制按钮提供完整订阅，不提供伪造的单插件 T4 API，也不执行服务端首页冒烟。
AT 设备发现、配对、推送入口及网盘授权仍由原版 AT 和影视负责；Sidecar 不另建一套设备管理。

影视设备需要同时能够访问 Sidecar 和原订阅中的 AT 地址，才能下载 JAR、调用 AT 原有接口。
只接受纯 type=4、不能执行 JAR 的客户端不支持这项原生推送；直接调用其服务端 T4 路由返回 `501`。
站点未单独提供 JAR 时自动使用原订阅的全局 `spider`；两者均缺失时不会导出不可运行的推送站点。
有多个 AT source 时只保留顺序最前的可用 `push_agent`，避免客户端固定推送 key 冲突。

### 播放失败与链路日志

1. 升级后刷新影视配置，重新打开影片详情，避免使用服务重启前的旧播放 ID。
2. 在 Web 底部“播放链路诊断”选择订阅、插件并输入片名，点击“检查”。
   依次执行搜索、详情、首个普通分集解析与媒体首段探测，不自动执行收藏、删除等操作。
3. 勾选“实时日志”，再在手机或电视复现问题；可查看 AT 状态、耗时、Worker 错误类型、
   解析地址类型，以及 HTTP DASH 音视频请求是否进入 Sidecar。
4. 点击“导出脱敏日志”。日志中的链路 ID 自动关联列表、详情、播放和媒体转发，
   无需复制 AT 凭证或原始播放地址；留空链路 ID 则显示最近记录。

“媒体字节通过”只表示探测请求拿到可识别的数据，不代表手机已解码或长播成功。
只有清单可达、未检查实际媒体的 HLS/外部 MPD 会单独标记“清单可达 · 媒体待验证”。
普通 MP4/HLS 直连时，客户端媒体流量不经过 Sidecar，因此没有 `media.*` 日志是正常情况；
`probe.*` 是独立诊断探测，不是客户端播放事件。`at.response` 的 HTTP 状态可用于区分 AT
请求失败；`worker.failed/timeout` 用于定位插件运行问题；`media.upstream/stream_failed`
用于定位 DASH 转发、Range 或传输中断。

Bilibili 的普通客户端优先沿用 AT 返回的 HTTPS 视频；显式 DASH 客户端或 AT 强制 DASH 时，
Sidecar 将内嵌 `data:application/dash+xml` 改为 HTTP `.mpd`，保留独立音视频、清晰度、
Range 和所需请求头。它只转发、不转码，不会把无声视频轨冒充完整影片。
HTTP DASH 仍要求播放器支持 DASH，不等于原生仅支持 HLS 的播放器自动获得 DASH 能力。

片单详情会按当前订阅顺序检索前 6 个可搜索且能执行的源，最多 3 路并发，
只合并同名且年份不冲突的结果，然后委托原源解析；未找到时明确提示，
不把豆瓣/TMDB ID 当作播放地址。FongMi 原本的全局标题搜索行为保持不变。

容器默认保存 `/data/logs/playback.jsonl`，单文件约 2 MB、保留两份轮转备份，
内存保留最近 2000 条并可在重启后恢复。插件 stderr 持续读取但仅保留长度、级别和指纹，
不保存原始文本；每个 Worker 每秒最多记录 20 条，避免大量打印拖慢播放。
日志不记录完整 URL、查询参数、响应正文、Cookie、AT 令牌或 ext。
Docker 默认关闭包含原始查询参数的 Uvicorn 访问日志，改用上述结构化日志。

诊断接口（均不增加额外鉴权）：

```text
GET  /t4/api/diagnostics/events?trace=<链路ID>
GET  /t4/api/diagnostics/export?trace=<链路ID>
POST /t4/api/diagnostics/check
GET|HEAD /t4/media/<临时资源ID>.mpd
GET|HEAD /t4/media/<临时资源ID>.mp4
```

媒体 ID 是内部映射，不是额外访问令牌。缓存最多 512 个资源，闲置 2 小时失效，
活跃请求会续期；容器重启后重新解析即可。媒体转发只访问 AT 返回并已登记的地址，
不接收任意 URL 参数。通过公网使用时，反代也需要保留 `/t4/media/` 路径与 Range 头。

维护者可在已设置 AT 订阅环境变量、安装 FFmpeg 的开发机上执行真实验证：

```bash
python -m scripts.verify_live_playback --report tests/reports/release/playback-verification.json
```

该脚本启动并自动关闭隔离测试服务，不改动 AT；检查普通视频、HTTP DASH、
音视频识别、5 秒实际解码和片单委托链。报告仅保存脱敏结果与源码哈希。

验证边界：已完成官方 type=4 参数合同、PY/JS 三类 HTTP 传输及真实 AT 抽样；
不等同于所有插件在 Android/电视上的完整功能验收。现有详情/播放 opaque ID 映射只保存在服务进程内，
重启后旧历史条目的播放 ID 可能失效；跨重启历史恢复、插件局部代理和客户端动作仍需后续验证。

页面中的 `env-at` 是使用 `ALIST_TVBOX_SUBSCRIPTION_URL` 时自动生成的默认 source ID。页面只展示脱敏后的插件能力，不展示订阅令牌、插件 secret、keyring 或原始播放 ID。

健康检查地址为：

```text
http://NAS_IP:8765/t4/health
```

如果启用了可选的 Nginx profile，独立代理入口为 `http://NAS_IP:8766/t4/`；同源 AT 入口需要由现有反向代理转发到 Sidecar，仓库不会自动改动 AT 的反向代理配置。

### 反向代理路径拼接规则

Sidecar 的所有公开路由都带有 `/t4` 前缀，例如：

```text
/t4/
/t4/health
/t4/config.json
/t4/api/sources
/t4/fm/{source}/{plugin}/
```

因此，推荐让反向代理保留客户端请求路径，只把请求转发到 Sidecar 的主机和端口：

```text
公网入口：https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/
后端地址：http://<NAS_IP>:8765
```

请求路径应当这样传递：

```text
客户端请求 /t4/health
        ↓
后端请求   /t4/health
```

不要把后端地址配置成 `http://<NAS_IP>:8765/t4` 后，再让反代继续追加客户端的 `/t4` 前缀。某些反代实现会产生重复路径：

```text
客户端请求 /t4/api/sources
        ↓
错误后端  /t4/t4/api/sources
        ↓
结果      HTTP 404
```

如果希望用户直接访问域名根路径 `/`，应配置根路径重定向到 `/t4/`，而不是把整个反代的后端地址写成带 `/t4` 的地址。FM 影视订阅也应使用：

```text
https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/config.json
```

反代修改后至少检查以下三个地址：

```text
https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/health
https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/api/sources
https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/config.json
```

前两个地址应返回 `200`。`config.json` 如果返回 `502`，通常表示 AT 订阅或上游不可用；如果返回 `404`，优先检查反代是否重复拼接了 `/t4`，以及 NAS 是否仍在运行旧镜像。

HTTPS 反代还需保留外部 `Host`（包含非标准端口），并传递 `X-Forwarded-Proto: https`。
容器默认通过 `FORWARDED_ALLOW_IPS=*` 读取反代协议头，无需用户额外填写域名。
公网通过 HTTPS 读取 `/t4/config.json` 后，其中 type=4 站点的 `api` 也应保持
`https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/fm/.../`。
如果订阅能导入但切源出现 `DioException`、`statusCode: null`，先核对该 `api` 是否错误使用了
`http://` 或内网地址。修正后在影视中刷新完整订阅，更新已缓存的站点地址。
局域网 HTTP 直连仍生成 HTTP 地址。具体配置见 [NAS 反代指南](NAS_DEPLOYMENT.md#八可选同源-t4-入口)。

## 配置变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `T4_IMAGE` | 否 | 镜像地址，默认 `ghcr.io/wab201/alist-tvbox-t4-sidecar:latest` |
| `T4_PORT` | 否 | NAS 对外端口，默认 `8765` |
| `ALIST_TVBOX_SUBSCRIPTION_URL` | 推荐 | AT 生成的完整订阅地址，包含订阅所需授权信息 |
| `T4_SOURCES_FILE` | 否 | 容器内 source 配置文件路径，默认 `/app/config/sources.json` |
| `T4_WORKER_TIMEOUT` | 否 | 插件请求超时秒数，默认 `15` |
| `T4_SUBSCRIPTION_CACHE_TTL` | 否 | AT 订阅快取秒数，默认 `30` |
| `T4_PLUGIN_DIR` | 否 | 上传插件持久化目录，默认 `/data/plugins` |
| `T4_UPLOAD_MAX_BYTES` | 否 | 单个上传文件上限，默认 `2000000` |
| `T4_SERVICE_NAME` | 否 | 健康检查显示的服务名 |
| `T4_COMPATIBILITY_REPORT` | 否 | 容器默认 `/data/reports/compatibility-latest.json`，保存最近兼容报告 |

真实订阅地址、插件 secret、Cookie 和 keyring 只能放在 NAS 的 `.env` 或 Docker Secret 中，不能提交到 Git，也不要写入 Dockerfile。

### 上传和自动注册插件

打开 Web 页面中的“上传本地插件”，选择 `.py`、`.js` 或 `.zip` 文件并上传。上传成功后会自动建立
`local-uploaded` source，并自动加入统一影视订阅，同时生成对应插件 API 地址。

- Python 文件需要包含标准 `Spider` 类，并实现 FongMi/ATVP 方法。
- JavaScript 文件使用 Sidecar 的 Node.js 适配合同，导出 `init`、`getName`、`homeContent`、
  `categoryContent`、`detailContent`、`searchContent`、`playerContent` 方法；需要追更或检查更新
  时可以额外导出 `action`。
- Python 插件可以按原版 ATVP 方式使用 `from base.spider import Spider`，Sidecar 会在导入前注入
  兼容基类。
- 有配套模块时上传一个 ZIP，根目录使用 `main.py`、`main.js` 或 `index.js` 作为入口，
  配套文件与主脚本一起保存。例如 `main.py` 可直接 `from helper import ...`，
  ZIP 内同时放入 `helper.py`；不同插件使用独立目录和 Worker。
- 非默认入口可在包根放置 `plugin.json`，内容例如 `{"entry":"crawler/main.py"}`。
  无配置且只有一个顶层定义 `Spider` 的 Python 文件时可自动识别。依赖文件应放在入口同目录
  或使用正常的 Python 包导入；不会自动执行 `requirements.txt` 或安装未知来源依赖。
- 容器预装已核实的第三方基础依赖。维护者已有的可信公共源码可放在 `/data/plugins/common/`，
  Worker 自动识别，无需填写新路径。缺少项目专属模块时会在 API、日志及检测证据中显示模块名，
  不能把不完整的单文件插件当作已安装成功。
- JavaScript 上传入口执行的是 Sidecar 的明确适配合同，不等同于对所有任意 Drpy/TVBox
  `rule` 文件提供通用兼容；页面会在插件执行失败时返回可定位的错误。
- 上传文件保存在 Docker volume 的 `/data/plugins`，容器重启后自动恢复注册。
- ZIP 压缩文件沿用单次上传大小上限，最多 128 个条目，解压内容不超过上传上限的 8 倍。
  刷新、删除作用于整个插件包，重复上传同一包不新增条目。
- 上传入口等同于执行本地代码，只应在可信的家庭网络使用。需要远程访问时由现有 VPN 或反向代理控制访问。

### 本地插件管理

Python 插件的宿主代理已接入：`getProxyUrl()`、运行时 `siteKey`、
`localProxy()` 及 `getCache/setCache/delCache` 自动提供，不需要额外端口、Token 或手动地址。
插件返回的代理封面/清单地址会变成当前 T4 域名下的 `/t4/proxy/<会话标识>`；
反代继续完整转发 `/t4/` 即可，不要把路径改成 AT 自身的地址。

`localProxy` 支持 `[状态码, MIME, 内容]` 和带响应头的四元素结果。
内容可以是文本、字节或二进制流；二进制不放进 JSON，流式转发不会整段缓存视频。
已处理的 `206/Content-Range` 原样透传；普通完整字节结果可自动响应单段 Range。
流对象的定位/Range 仍由插件实现，不承诺任意流都支持快进。
缓存仅属于当前插件 Worker，最多 512 项，不跨容器重启保存。

刷新、删除或重启对应 Worker 后，旧代理地址返回 `410`，重新打开分类/详情即可取得新地址。
这里的会话标识用于找到运行实例，不是新增鉴权，也不需要复制或填写。
只给出客户端硬编码代理地址、未实现 `localProxy`、缺少配套源码的插件仍需单独适配。

| 操作 | 效果 |
| --- | --- |
| 重命名 | 保存 1-80 字符名称，拒绝空白、控制字符和同名；插件 ID、FM 地址不变 |
| 上移 / 下移 | 立即保存列表顺序；重启后仍然保留，能力快照使用相同顺序 |
| 刷新运行实例 | 校验当前文件，回收旧 Worker 和初始化状态；下次请求从当前文件重新加载 |
| 删除 | 二次确认后移除注册和本地文件，回收运行实例；最后一个插件删除后移除本地 source |
| 刷新本地列表 | 重新读取已持久化的管理列表，不重启插件 |
| 首页冒烟 / 复制插件 API | 验证所选插件首页，或复制其 type=4 API，不是完整点播订阅 |

重复上传同一内容不会新增条目，也不会覆盖已保存的名称和顺序；不同内容但同名的文件自动追加
`(2)` 等后缀。删除后重新上传同一内容会重新初始化运行实例。本地插件保存在 `/data/plugins`，
请保留 Docker volume。管理存储按单个 Gateway 进程使用，不要通过多个 Uvicorn worker 共享该目录。

AT 订阅保持只读，以上删除、排序、重命名操作只作用于 Sidecar 上传插件。AT 原始插件请在原版
AT 中管理，再点击“重新读取 AT 订阅”强制绕过订阅缓存。该按钮刷新订阅描述，不代表批量重启所有 AT 插件。

管理接口（无需额外鉴权）：

```text
GET    /t4/api/plugins
PATCH  /t4/api/plugins/{id}           JSON: {"name":"新名称"}
POST   /t4/api/plugins/reorder        JSON: {"plugin_ids":["id2","id1"]}
POST   /t4/api/plugins/{id}/refresh
DELETE /t4/api/plugins/{id}
GET    /t4/api/sources/{source}/subscription?refresh=true
```

## NAS 部署

完整的中文步骤、GHCR 登录、环境变量、升级、回滚、同源代理和故障排查请看：

**[NAS_DEPLOYMENT.md](NAS_DEPLOYMENT.md)**

快速流程如下：

```bash
mkdir -p /volume1/docker/alist-tvbox-t4-sidecar
cd /volume1/docker/alist-tvbox-t4-sidecar
# 从本地仓库上传 docker-compose.yml 和 .env.example，再复制：
cp .env.example .env
# 编辑 .env，填写 ALIST_TVBOX_SUBSCRIPTION_URL
docker compose config --quiet
docker compose pull
docker compose up -d
```

当前仓库为私有仓库，不能匿名使用 `raw.githubusercontent.com` 下载文件。
也可以在已经完成 GitHub 认证的 NAS 上克隆仓库，再进入项目目录执行上述命令。

当前 GHCR 镜像：

```text
ghcr.io/wab201/alist-tvbox-t4-sidecar:latest
ghcr.io/wab201/alist-tvbox-t4-sidecar:sha-<提交短 SHA>
```

私有 GHCR 包需要在 NAS 单独执行 `docker login ghcr.io`。登录令牌只用于 Docker 凭证存储，不放入项目 `.env`。

## 本地开发

```powershell
python -m pytest -q
python -m uvicorn sidecar.gateway.main:app --host 127.0.0.1 --port 8765
```

开发容器：

```powershell
Copy-Item .env.example .env
docker compose up --build -d t4-sidecar
```

## T4 调用示例

所有接口均无需额外认证请求头。下面示例中的
`<T4地址>` 是 `http://NAS_IP:8765`，`source=env-at` 是环境变量订阅的默认 source ID。
当一个 AT 订阅包含多个插件时，必须通过 `site` 指定插件；下面以 `csp_BiliBili` 为例。

```text
GET <T4地址>/t4/api/home?source=env-at&site=csp_BiliBili
GET <T4地址>/t4/api/category?source=env-at&site=csp_BiliBili&t=all&pg=1
GET <T4地址>/t4/api/search?source=env-at&site=csp_BiliBili&wd=关键词&pg=1
GET <T4地址>/t4/api/detail?source=env-at&site=csp_BiliBili&ids=<opaque-id>
GET <T4地址>/t4/api/play?source=env-at&site=csp_BiliBili&flag=<flag>&play=<opaque-id>
POST <T4地址>/t4/api/action?source=env-at&site=csp_Media&action=inspect&id=<opaque-id>
```

影视请优先导入 Web 顶部生成的统一订阅。每个 `sites` 条目的 `api` 已绑定 source 和插件，不需要客户端额外填写
`source`、`site` 或任何额外令牌。
独立插件 API 支持客户端向同一个地址传参：

```text
GET <插件API>?filter=true
GET <插件API>?ac=detail&t=分类ID&pg=1&ext=<URL-safe Base64 JSON筛选>
GET <插件API>?wd=关键词&quick=false&pg=1
GET <插件API>?ac=detail&ids=<opaque-id>
GET <插件API>?play=<opaque-id>&flag=播放源
```

同时支持 JSON POST 和表单 POST。客户端分类请求的 `ac=detail&t=...` 会被正确识别为分类，
不会误判为详情；筛选 `ext` 解码后传入插件或 AT 原生筛选参数。`/t4/api/{module}` 保留为显式模块调用入口。

完整订阅 token、插件 secret、keyring 和原始播放 ID 不会出现在 T4 响应中。
