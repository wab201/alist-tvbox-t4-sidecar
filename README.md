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

内置源的列表、搜索、详情和播放复用 AT 原生服务端。Media 的追更动作通过 `/t4/api/action` 复用 AT `/play`。`csp_Push` 是 Android 客户端本地能力，`csp_WebHome` 和 `csp_Nostr` 是页面入口，能力快照会明确标记为非标准 T4，不伪造空 VOD 结果。

## 配置变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `T4_IMAGE` | 否 | 镜像地址，默认 `ghcr.io/wab201/alist-tvbox-t4-sidecar:latest` |
| `T4_PORT` | 否 | NAS 对外端口，默认 `8765` |
| `T4_GATEWAY_TOKEN` | 是 | T4 API 访问令牌；健康检查除外 |
| `ALIST_TVBOX_SUBSCRIPTION_URL` | 推荐 | AT 生成的完整订阅地址，包含订阅所需授权信息 |
| `T4_SOURCES_FILE` | 否 | 容器内 source 配置文件路径，默认 `/app/config/sources.json` |
| `T4_WORKER_TIMEOUT` | 否 | 插件请求超时秒数，默认 `15` |
| `T4_SERVICE_NAME` | 否 | 健康检查显示的服务名 |
| `T4_VERSION` | 否 | 健康检查显示的版本 |

真实订阅地址、Gateway Token、插件 secret、Cookie 和 keyring 只能放在 NAS 的 `.env` 或 Docker Secret 中，不能提交到 Git，也不要写入 Dockerfile。

## NAS 部署

完整的中文步骤、GHCR 登录、环境变量、升级、回滚、同源代理和故障排查请看：

**[NAS_DEPLOYMENT.md](NAS_DEPLOYMENT.md)**

快速流程如下：

```bash
mkdir -p /volume1/docker/alist-tvbox-t4-sidecar
cd /volume1/docker/alist-tvbox-t4-sidecar
# 从本地仓库上传 docker-compose.yml 和 .env.example，再复制：
cp .env.example .env
# 编辑 .env，填写 T4_GATEWAY_TOKEN 和 ALIST_TVBOX_SUBSCRIPTION_URL
docker compose config --quiet
docker compose pull
docker compose up -d
```

当前仓库为私有仓库，不能匿名使用 `raw.githubusercontent.com` 下载文件。
也可以在已经完成 GitHub 认证的 NAS 上克隆仓库，再进入项目目录执行上述命令。

当前 GHCR 镜像：

```text
ghcr.io/wab201/alist-tvbox-t4-sidecar:latest
ghcr.io/wab201/alist-tvbox-t4-sidecar:sha-f39fe8d
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

请求头推荐使用 `X-T4-Token`，不能设置请求头的客户端可使用 `t4_token` 查询参数：

```text
/t4/api/home?source=env-at
/t4/api/category?source=env-at&t=all&pg=1
/t4/api/search?source=env-at&wd=关键词&pg=1
/t4/api/detail?source=env-at&ids=<opaque-id>
/t4/api/play?source=env-at&flag=<flag>&play=<opaque-id>
/t4/api/action?source=env-at&site=csp_Media&action=inspect&id=<opaque-id>
```

完整订阅 token、插件 secret、keyring 和原始播放 ID 不会出现在 T4 响应中。
