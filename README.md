# AList-TVBox T4 Sidecar

这是一个独立的附属容器，用于把已授权的 AList-TVBox（AT）订阅和 ATVP 插件运行时适配成只读 T4 接口。它不修改 AT 的 JAR、数据库、原始插件或前端文件，适合与 AT 并行部署在 NAS、家庭服务器或独立 Linux 主机上。

## 项目说明

运行链路如下：

```text
T4 客户端
    │  /t4/api/*
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

## 当前能力

- `GET /t4/health`：健康检查，不需要令牌
- `GET /t4/`：中文 Web 管理入口
- `GET /t4/api/sources`：读取 source 能力快照
- `GET|POST /t4/api/{home|category|search|detail|play|action}`：T4 标准模块
- 从 `ALIST_TVBOX_SUBSCRIPTION_URL` 读取 AT 完整订阅并自动建立 `env-at` source
- ATVP raw Python 插件
- 官方格式 `secspider/1` 插件
- 自定义 `secspider/1` 动态 loader 和只读 keyring 回退
- AT 内置源原生桥接：Bilibili、我的追更、AList、小雅、片单、直播、Emby、Jellyfin、飞牛、盘搜、Telegram 豆瓣、Telegram 频道、Telegram 网页
- source 级 Worker 会话、超时回收和 opaque 详情/播放 ID
- Web 上传标准 Python `Spider` 或 Sidecar JavaScript 插件，自动注册到 `local-uploaded`
- 订阅快取和并发单飞，避免每个 T4 请求重复拉取、解析 AT 订阅

内置源的列表、搜索、详情和播放复用 AT 原生服务端。Media 的追更动作通过 `/t4/api/action` 复用 AT `/play`。`csp_Push` 是 Android 客户端本地能力，`csp_WebHome` 和 `csp_Nostr` 是页面入口，能力快照会明确标记为非标准 T4，不伪造空 VOD 结果。

## Web 管理界面

Sidecar 自带独立的中文 Web 管理入口，不需要修改 AT 原页面：

```text
http://NAS_IP:8765/t4/
```

页面使用步骤：

1. 打开上面的地址，确认页面右上角健康状态正常。
2. 页面会自动读取容器中的 source 和 AT 订阅配置；默认本地模式不需要填写 Token。
3. 在对应 source 中点击“读取能力”，查看订阅解析出的插件列表。
4. 选择要使用的插件 `site`。多插件订阅必须选择，例如 `csp_BiliBili`、`csp_Media`。
5. 点击“首页冒烟”验证插件；验证通过后点击“复制 FM 地址”，即可把该插件的兼容入口添加到 FM 影视。

页面中的 `env-at` 是使用 `ALIST_TVBOX_SUBSCRIPTION_URL` 时自动生成的默认 source ID。页面只展示脱敏后的插件能力，不展示订阅令牌、插件 secret、keyring 或原始播放 ID。本地模式无需 Gateway Token；保护模式下 Token 仅临时保存在当前浏览器标签页，生产环境应通过 `X-T4-Token` 请求头传递，不要把令牌写进公开 URL。

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
| `T4_GATEWAY_TOKEN` | 否 | 可选保护令牌；留空时为本地直连模式 |
| `ALIST_TVBOX_SUBSCRIPTION_URL` | 推荐 | AT 生成的完整订阅地址，包含订阅所需授权信息 |
| `T4_SOURCES_FILE` | 否 | 容器内 source 配置文件路径，默认 `/app/config/sources.json` |
| `T4_WORKER_TIMEOUT` | 否 | 插件请求超时秒数，默认 `15` |
| `T4_SUBSCRIPTION_CACHE_TTL` | 否 | AT 订阅快取秒数，默认 `30` |
| `T4_PLUGIN_DIR` | 否 | 上传插件持久化目录，默认 `/data/plugins` |
| `T4_UPLOAD_MAX_BYTES` | 否 | 单个上传文件上限，默认 `2000000` |
| `T4_SERVICE_NAME` | 否 | 健康检查显示的服务名 |
| `T4_VERSION` | 否 | 健康检查显示的版本 |

真实订阅地址、Gateway Token、插件 secret、Cookie 和 keyring 只能放在 NAS 的 `.env` 或 Docker Secret 中，不能提交到 Git，也不要写入 Dockerfile。

### 上传和自动注册插件

打开 Web 页面中的“上传本地插件”，选择 `.py` 或 `.js` 文件并上传。上传成功后会自动建立
`local-uploaded` source，并在插件能力中生成对应 FM 兼容地址。

- Python 文件需要包含标准 `Spider` 类，并实现 FongMi/ATVP 方法。
- JavaScript 文件使用 Sidecar 的 Node.js 适配合同，导出 `init`、`getName`、`homeContent`、
  `categoryContent`、`detailContent`、`searchContent`、`playerContent` 方法；需要追更或检查更新
  时可以额外导出 `action`。
- Python 插件可以按原版 ATVP 方式使用 `from base.spider import Spider`，Sidecar 会在导入前注入
  兼容基类。
- JavaScript 上传入口执行的是 Sidecar 的明确适配合同，不等同于对所有任意 Drpy/TVBox
  `rule` 文件提供通用兼容；页面会在插件执行失败时返回可定位的错误。
- 上传文件保存在 Docker volume 的 `/data/plugins`，容器重启后自动恢复注册。
- 上传入口等同于执行本地代码，只应在可信的家庭网络或已启用保护模式的管理面使用。

## NAS 部署

完整的中文步骤、GHCR 登录、环境变量、升级、回滚、同源代理和故障排查请看：

**[NAS_DEPLOYMENT.md](NAS_DEPLOYMENT.md)**

快速流程如下：

```bash
mkdir -p /volume1/docker/alist-tvbox-t4-sidecar
cd /volume1/docker/alist-tvbox-t4-sidecar
# 从本地仓库上传 docker-compose.yml 和 .env.example，再复制：
cp .env.example .env
# 编辑 .env，填写 ALIST_TVBOX_SUBSCRIPTION_URL；本地模式无需填写 Gateway Token
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

如果配置了可选的 `T4_GATEWAY_TOKEN`，请求头使用 `X-T4-Token`；未配置时无需请求头。下面示例中的
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

请求头：

```http
X-T4-Token: 你的T4_GATEWAY_TOKEN
```

不能设置请求头的客户端可以使用 Web 页面生成的 FM 兼容地址。该地址已经绑定 source 和插件，不需要客户端额外填写
`source`、`site` 或 Gateway Token。未配置 Gateway Token 时，FM 地址是本地直连地址；启用保护模式时，页面会自动生成插件级访问钥匙。
直接调用底层 T4 API 时，保护模式仍可使用 `t4_token` 查询参数，但它可能进入浏览器历史、反向代理日志和访问日志，不建议用于公网地址。

完整订阅 token、插件 secret、keyring 和原始播放 ID 不会出现在 T4 响应中。
