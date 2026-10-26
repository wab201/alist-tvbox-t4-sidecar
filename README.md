# AList-TVBox T4 Sidecar

这是一个独立的附属容器，用于把已授权的 AList-TVBox（AT）订阅和 ATVP 插件运行时适配成只读 T4 接口。它不修改 AT 的 JAR、数据库、原始插件或前端文件，适合与 AT 并行部署在 NAS、家庭服务器或独立 Linux 主机上。

## 项目说明

运行链路如下：

```text
T4 客户端
    │  导入 /t4/config.json → 各站点 type=4 API
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
- Web 上传标准 Python `Spider` 或 Sidecar JavaScript 插件，自动注册到 `local-uploaded`
- 本地插件列表：删除、重命名、上移/下移排序、刷新运行实例、首页冒烟、复制 FM 地址
- 订阅快取和并发单飞，避免每个 T4 请求重复拉取、解析 AT 订阅

内置源的列表、搜索、详情和播放复用 AT 原生服务端。Media 的追更动作通过 `/t4/api/action` 复用 AT `/play`。`csp_Push` 是 Android 客户端本地能力，`csp_WebHome` 和 `csp_Nostr` 是页面入口，能力快照会明确标记为非标准 T4，不伪造空 VOD 结果。

## Web 管理界面

Sidecar 自带独立的中文 Web 管理入口，不需要修改 AT 原页面：

```text
http://NAS_IP:8765/t4/
```

页面使用步骤：

1. 打开上面的地址，确认页面右上角健康状态正常。
2. 页面会自动读取容器中的 source 和 AT 订阅配置，不需要填写额外 Token。
3. AT 订阅的插件列表会自动读取。在下拉框选择需要的插件，例如 `csp_BiliBili`、`csp_Media`。
4. 上传的 Python/JS 文件直接显示在“本地插件”列表，每行都有管理按钮，悬停可查看名称。
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
页面型源、客户端本地能力不伪装成 type=4 站点，统一订阅会跳过 `csp_Push`、`csp_WebHome` 等条目。
某个已配置 AT source 拉取失败时，订阅明确返回 `502`，不会静默生成缺少站点的订阅。

统一订阅和各站点 API 均无需额外令牌、签名路径或认证请求头。AT 授权信息仅用于服务端访问 AT，不下发给影视。

验证边界：已完成官方 type=4 参数合同、PY/JS 三类 HTTP 传输及真实 AT 抽样；
不等同于所有插件在 Android/电视上的完整功能验收。现有详情/播放 opaque ID 映射只保存在服务进程内，
重启后旧历史条目的播放 ID 可能失效；跨重启历史恢复、插件局部代理和客户端动作仍需后续验证。

页面中的 `env-at` 是使用 `ALIST_TVBOX_SUBSCRIPTION_URL` 时自动生成的默认 source ID。页面只展示脱敏后的插件能力，不展示订阅令牌、插件 secret、keyring 或原始播放 ID。

健康检查地址为：

```text
http://NAS_IP:8765/t4/health
```

如果启用了可选的 Nginx profile，独立代理入口为 `http://NAS_IP:8766/t4/`；同源 AT 入口需要由现有反向代理转发到 Sidecar，仓库不会自动改动 AT 的反向代理配置。

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
| `T4_VERSION` | 否 | 健康检查显示的版本 |

真实订阅地址、插件 secret、Cookie 和 keyring 只能放在 NAS 的 `.env` 或 Docker Secret 中，不能提交到 Git，也不要写入 Dockerfile。

### 上传和自动注册插件

打开 Web 页面中的“上传本地插件”，选择 `.py` 或 `.js` 文件并上传。上传成功后会自动建立
`local-uploaded` source，并自动加入统一影视订阅，同时生成对应插件 API 地址。

- Python 文件需要包含标准 `Spider` 类，并实现 FongMi/ATVP 方法。
- JavaScript 文件使用 Sidecar 的 Node.js 适配合同，导出 `init`、`getName`、`homeContent`、
  `categoryContent`、`detailContent`、`searchContent`、`playerContent` 方法；需要追更或检查更新
  时可以额外导出 `action`。
- Python 插件可以按原版 ATVP 方式使用 `from base.spider import Spider`，Sidecar 会在导入前注入
  兼容基类。
- JavaScript 上传入口执行的是 Sidecar 的明确适配合同，不等同于对所有任意 Drpy/TVBox
  `rule` 文件提供通用兼容；页面会在插件执行失败时返回可定位的错误。
- 上传文件保存在 Docker volume 的 `/data/plugins`，容器重启后自动恢复注册。
- 上传入口等同于执行本地代码，只应在可信的家庭网络使用。需要远程访问时由现有 VPN 或反向代理控制访问。

### 本地插件管理

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
