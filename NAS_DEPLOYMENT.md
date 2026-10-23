# NAS 部署指南

本文适用于群晖、威联通、绿联及其他支持 Docker Compose 的 NAS。部署目标是：在 NAS 上直接拉取 GHCR 镜像，通过环境变量读取 AT 订阅，把 Sidecar 暴露为独立的 T4 服务。

## 一、部署前准备

需要准备：

1. NAS 已安装 Docker 或 Container Manager。
2. NAS 可以访问 `ghcr.io` 和 AT 服务地址。
3. 已在 AT 中生成一条可用的完整订阅地址。
4. 如果需要把服务放到非可信网络，再准备一个 T4 Gateway Token；家庭局域网本地部署可以不配置。
5. 如果 GHCR 镜像为私有包，准备 GitHub 个人访问令牌，至少具有 `read:packages` 权限。

Sidecar 不读取 AT 用户名和密码，也不需要把 AT 登录密码写入容器。当前推荐直接使用 AT 生成的完整订阅地址：

```text
ALIST_TVBOX_SUBSCRIPTION_URL=https://你的AT地址/sub/订阅令牌/订阅编号
```

订阅地址中可能包含授权信息，必须视为密码处理。

## 二、创建目录和配置

以下路径以群晖常见目录为例。其他 NAS 可替换为自己的 Docker 项目目录：

```bash
mkdir -p /volume1/docker/alist-tvbox-t4-sidecar
cd /volume1/docker/alist-tvbox-t4-sidecar
```

将仓库中的 `docker-compose.yml` 和 `.env.example` 上传到 NAS 项目目录。
可以使用 NAS 文件管理器上传，也可以在已经完成 GitHub 认证的 NAS 上克隆私有仓库：

```bash
git clone https://github.com/wab201/alist-tvbox-t4-sidecar.git .
cp .env.example .env
chmod 600 .env
```

如果 NAS 未安装 Git，直接通过文件管理器上传这两个文件即可，不需要把 GitHub
Token 写入克隆地址。当前仓库为私有仓库，匿名 `curl` 下载 GitHub Raw 文件不可用。

编辑 `.env`，至少填写 AT 订阅地址；本地部署无需填写 Gateway Token：

```dotenv
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:latest
T4_PORT=8765
T4_GATEWAY_TOKEN=
T4_SOURCES_FILE=/app/config/sources.json
ALIST_TVBOX_SUBSCRIPTION_URL=请替换为AT完整订阅地址
T4_WORKER_TIMEOUT=15
T4_SERVICE_NAME=t4-sidecar
T4_VERSION=0.1.0
```

如需启用保护模式，再生成 Gateway Token：

```bash
openssl rand -hex 32
```

不要把真实 `.env` 上传到 GitHub、截图、工单或公开日志。

## 三、登录 GHCR

仓库和镜像包为私有时，NAS 首次拉取前需要登录 GHCR。GitHub 个人访问令牌只用于 NAS 的 Docker 凭证，不要写进 `.env`：

```bash
read -s GHCR_TOKEN
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io --username 你的GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

令牌至少需要 `read:packages`。如果 GitHub 组织或仓库启用了额外 SSO，还需要按组织要求授权该令牌。

如果管理员已将 GHCR 包设为公开，可以跳过 `docker login`。不要为了绕过权限把含订阅授权信息的 AT 地址改成公开地址。

## 四、拉取并启动

先只校验 Compose 配置，不输出合并后的环境变量：

```bash
docker compose config --quiet
```

拉取镜像并启动：

```bash
docker compose pull t4-sidecar
docker compose up -d t4-sidecar
docker compose ps
```

检查健康状态：

```bash
curl -fsS http://127.0.0.1:8765/t4/health
```

预期返回包含：

```json
{"status":"ok","version":"0.1.0"}
```

从局域网访问：

```text
http://NAS_IP:8765/t4/
```

## 五、Web 管理界面使用

### 进入地址

直接访问：

```text
http://NAS_IP:8765/t4/
```

如果启用了可选的 Nginx profile，则访问：

```text
http://NAS_IP:8766/t4/
```

页面右上角会先检查：

```text
http://NAS_IP:8765/t4/health
```

健康检查不需要 Gateway Token。页面打不开时，先在 NAS 上执行：

```bash
curl -fsS http://127.0.0.1:8765/t4/health
```

### 页面操作

1. 打开页面，Sidecar 会自动读取容器中的 source 和 AT 订阅配置；本地模式不需要填写 Token。
2. 在已加载的 source 中点击“读取能力”，等待订阅插件列表出现。
3. 选择需要调用的插件 `site`。使用环境变量订阅时，source ID 固定为 `env-at`；一个订阅含多个插件时，必须选择 `site`。
4. 点击“首页冒烟”测试当前插件的首页请求。
5. 点击“复制 FM 地址”复制当前插件的 FM 兼容入口，直接粘贴到 FM 影视中使用；页面也保留“复制首页测试地址”用于接口排查。
6. 修改 `.env` 后先重新创建容器，再刷新页面。

页面会标注不能转成标准 T4 的本地能力或页面入口，例如 `csp_Push`、`csp_WebHome`。这类条目不会被当作可播放的标准 VOD 插件。

### 页面安全规则

- 本地模式不需要 Gateway Token，复制的 FM 地址可直接使用。
- 如果启用了保护模式，Token 仅临时保存在当前浏览器标签页的 `sessionStorage`。
- 不要把 Token 粘贴到公开工单、截图、浏览器书签或公网 URL。
- 生产环境优先使用支持自定义请求头的 T4 客户端，或把服务放在 HTTPS、VPN 和访问控制之后。

### 页面常见错误

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `401` | 保护模式 Token 缺失或错误 | 关闭 `T4_GATEWAY_TOKEN`，或在页面显示的保护模式输入框填入正确令牌 |
| `400` | 缺少 `source` 或多插件订阅缺少 `site` | 先确认 source ID，再选择具体插件 |
| `501` | 插件只有 AT 客户端本地能力，没有 AT 服务端 T4 路由 | 改用支持标准 T4 的插件 |
| `502` | AT 订阅、插件 loader 或上游站点失败 | 检查 AT 地址、订阅有效期和容器日志 |

## 六、在 T4 客户端中使用

Sidecar 的 T4 根地址为：

```text
http://NAS_IP:8765/t4/
```

如果启用了保护模式，通过请求头传递 Gateway Token：

```http
X-T4-Token: 你的T4_GATEWAY_TOKEN
```

本地模式无需令牌。保护模式下，不能设置请求头的客户端可以使用查询参数，但查询参数可能进入浏览器历史、
反向代理日志和访问日志：

```text
http://NAS_IP:8765/t4/api/home?source=env-at&site=csp_BiliBili&t4_token=你的T4_GATEWAY_TOKEN
```

当一个 AT 订阅包含多个插件时，必须带上 `site` 参数；`csp_BiliBili`、`csp_Media`
等值来自订阅中的插件 `key`。

因此，生产环境优先使用支持自定义请求头的客户端或在可信的内网反向代理后使用。

## 七、升级和固定版本

使用最新稳定镜像：

```bash
docker compose pull t4-sidecar
docker compose up -d t4-sidecar
```

需要可回滚和可复现时，把 `.env` 中的镜像改为提交标签，例如：

```dotenv
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:sha-<提交短 SHA>
```

将 `<提交短 SHA>` 替换为 GitHub Actions 构建日志或镜像标签中对应提交的短 SHA。

然后执行：

```bash
docker compose pull t4-sidecar
docker compose up -d t4-sidecar
```

回滚就是把 `T4_IMAGE` 改回上一个已验证的 `sha-` 标签，再重新执行上述两条命令。不要在 NAS 上使用 `docker compose up --build`，那会绕过 GHCR 镜像并在 NAS 本地构建。

## 八、可选：同源 `/t4/` 入口

如果 AT 已经有反向代理，可以把 `/t4/` 转发到 NAS 的 `8765` 端口。这样客户端只需要访问同一域名下的 `/t4/`，但反向代理必须正确转发：

- `Host`
- `X-Forwarded-For`
- `X-Forwarded-Proto`
- GET 和 POST 请求体

仓库提供了可选的 Nginx Compose profile。启用后，Sidecar 通过 NAS 的 `8766` 端口提供 `/t4/`：

```bash
docker compose --profile proxy up -d
```

这个 profile 只提供独立的 Nginx 入口，不会自动修改 AT 的反向代理配置，也不会改变 AT 的端口。

## 九、文件 source 方式

如果不使用 `ALIST_TVBOX_SUBSCRIPTION_URL`，可以改用脱敏的 source 配置文件：

1. 在 NAS 项目目录创建 `config/sources.json`。
2. 使用仓库中的 `config/sources.example.json` 作为结构模板。
3. 把 `T4_SOURCES_FILE` 保持为 `/app/config/sources.json`。
4. 在 `docker-compose.yml` 的 `volumes` 中启用只读挂载：

```yaml
volumes:
  - ./config/sources.json:/app/config/sources.json:ro
```

文件中同样可能包含订阅令牌或私有地址，权限建议设置为：

```bash
chmod 600 config/sources.json
```

同一实例建议只选择一种 source 配置方式，优先使用环境变量中的完整订阅地址。

## 十、故障排查

### `docker pull` 返回 denied

检查 NAS 是否登录了正确的 GHCR 账户，以及令牌是否具备 `read:packages`。重新登录：

```bash
docker logout ghcr.io
read -s GHCR_TOKEN
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io --username 你的GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

### `/t4/health` 正常，但 API 返回 `401`

说明服务已启动且启用了保护模式，但请求没有携带正确的 `T4_GATEWAY_TOKEN`。检查客户端请求头：

```http
X-T4-Token: 你的T4_GATEWAY_TOKEN
```

### API 返回 `source is required`

请求缺少 `source` 参数。使用环境变量订阅时默认 source ID 是：

```text
env-at
```

### FM 影视如何使用

1. 打开 Sidecar Web 管理入口，页面会自动读取配置。
2. 点击 source 的“读取能力”。
3. 选择需要的插件，例如 `csp_BiliBili`。
4. 点击“复制 FM 地址”。
5. 在 FM 影视中新增接口/源，把复制的地址作为接口地址保存。

每个插件对应一条 FM 地址。`csp_BiliBili`、`csp_Media` 等插件不能共用同一条地址，因为 Sidecar 需要根据地址自动选择对应插件。FM 地址已经包含访问授权，不需要再填写 `source`、`site` 或 `X-T4-Token`。

本地模式下 FM 地址是绑定插件的便捷入口；保护模式下 FM 地址同时属于访问凭证。建议只在家庭网络、VPN 或 HTTPS
反向代理中使用，不要发布到公网、截图或公共配置仓库。修改 `T4_GATEWAY_TOKEN` 后，保护模式旧 FM 地址会失效，
需要重新从 Web 页面复制。

### API 返回订阅不可用或 `502`

检查三点：

1. NAS 能否访问 AT 地址。
2. `.env` 中的 `ALIST_TVBOX_SUBSCRIPTION_URL` 是否完整且未被换行截断。
3. AT 订阅是否仍然有效，插件 loader 和依赖站点是否可访问。

查看日志时不要把日志原样发布到公开位置：

```bash
docker compose logs --tail=200 t4-sidecar
```

### 修改 `.env` 后没有生效

环境变量修改后必须重新创建容器：

```bash
docker compose up -d --force-recreate t4-sidecar
```

### 如何确认当前镜像版本

```bash
docker inspect alist-tvbox-t4-sidecar --format '{{.Config.Image}}'
docker compose images
```

如果容器名称被 Compose 加了项目名前缀，以 `docker compose ps` 显示的名称为准。

## 十一、安全边界

- 不把 AT 账号密码、完整订阅地址、Cookie、插件 secret、keyring 提交到 Git。
- 不把 GHCR PAT 写入 `.env`、Compose 文件或镜像构建参数。
- 不把 Gateway Token 放到公开截图和公开 URL 中。
- 不把 `8765` 暴露到公网；需要公网访问时，先放在 HTTPS 反向代理、访问控制或 VPN 后面。
- T4 返回的详情和播放 ID 是 Sidecar 的 opaque ID，不应被当成永久链接保存。
