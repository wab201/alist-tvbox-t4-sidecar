# AList-TVBox T4 Sidecar

这是一个独立附属容器，用于把已授权的 AList-TVBox 订阅和 ATVP 插件运行时适配成只读 T4 接口。

## 当前能力

- `GET /t4/health`
- `GET /t4/`
- `GET /t4/api/sources`
- `GET|POST /t4/api/{home|category|search|detail|play|action}`
- AT `/sub/{token}/{id}` 订阅读取和 Base64 `ext` 解析
- ATVP raw Python
- `secspider/1` 官方格式解包
- 自定义 `secspider/1` 的动态 loader 和只读 keyring 回退
- AT 内置源原生桥接：`csp_BiliBili`、`csp_Media`、`csp_AList`、`csp_XiaoYa`、`csp_PianDan`、`csp_Live`、`csp_Emby`、`csp_Jellyfin`、`csp_FeiNiu`、盘搜、`csp_TgDouBan`、`csp_TgChannel`、`csp_TgWeb`
- source 级 Worker 会话、超时回收和 opaque 播放 ID

内置源的列表、搜索、详情和播放复用 AT 原生服务端；Media 的追更伪线路通过 `/t4/api/action` 复用 AT `/play`。Bilibili 的历史、关注、追番、收藏等导航值由原版 `/bilibili/{token}` 原样处理。Telegram 频道使用 `/tgsc`，Telegram 网页使用 `/tg-search?web=true`，不会混用两套合同。

`csp_Push` 是 Android 客户端本地能力，`csp_WebHome` 和 `csp_Nostr` 是页面入口；能力快照会明确标记为非标准 T4，不返回伪造的空 VOD 结果。AT 管理面登录、Cookie 二维码、`/api/media-subscriptions` 管理操作、播放同步管理面和 WebHome 注入逻辑继续由原版 AT 负责。

## 开发运行

```powershell
python -m pytest -q
python -m uvicorn sidecar.gateway.main:app --host 127.0.0.1 --port 8765
```

开发配置可复制 `.env.example` 和 `config/sources.example.json`，不要把真实订阅 token、secret、Cookie 或 keyring 写入 Git。

如果环境中已有完整 AT 订阅地址，可以只配置 `ALIST_TVBOX_SUBSCRIPTION_URL`。
Sidecar 会自动创建 `env-at` source，不要求修改 AT 或维护 source 配置文件。

## 容器运行

```powershell
Copy-Item .env.example .env
docker compose up --build -d t4-sidecar
```

使用文件 source 时，再复制并挂载 `config/sources.example.json` 的副本。
需要同源入口时，在已有 AT 反向代理中把 `/t4/` 转发到 Sidecar 的 `8765` 端口；不要改 AT JAR、数据库或前端构建产物。

仓库包含 `.github/workflows/container.yml`，推送到 `main` 后由 GitHub Actions
构建并发布 GHCR 镜像：

```text
ghcr.io/wab201/alist-tvbox-t4-sidecar:latest
```

`ALIST_TVBOX_SUBSCRIPTION_URL`、`T4_GATEWAY_TOKEN` 等运行时变量只在部署环境注入，
不会写入镜像层或 GitHub Actions 日志。

## T4 调用

请求头推荐使用 `X-T4-Token`。不能设置请求头的客户端可使用 `t4_token` 查询参数。

```text
/t4/api/home?source=at-main
/t4/api/category?source=at-main&t=all&pg=1
/t4/api/search?source=at-main&wd=关键词&pg=1
/t4/api/detail?source=at-main&ids=<opaque-id>
/t4/api/play?source=at-main&flag=<flag>&play=<opaque-id>
/t4/api/action?source=at-main&site=csp_Media&action=inspect&id=<opaque-id>
```

完整订阅 token、插件 secret、keyring 和原始播放 ID 不会出现在 T4 响应中。
