# NAS 部署指南

本文适用于群晖、威联通、绿联及其他支持 Docker Compose 的 NAS。部署目标是：在 NAS 上直接拉取 GHCR 镜像，通过环境变量读取 AT 订阅，把 Sidecar 暴露为独立的 T4 服务。

## 一、部署前准备

需要准备：

1. NAS 已安装 Docker 或 Container Manager。
2. NAS 可以访问 `ghcr.io` 和 AT 服务地址。
3. 已在 AT 中生成一条可用的完整订阅地址。
4. 如果 GHCR 镜像为私有包，准备 GitHub 个人访问令牌，至少具有 `read:packages` 权限。

Sidecar 不读取 AT 用户名和密码，也不需要把 AT 登录密码写入容器。当前推荐直接使用 AT 生成的完整订阅地址：

```text
ALIST_TVBOX_SUBSCRIPTION_URL=https://你的AT地址/sub/订阅令牌/订阅编号
```

订阅地址中可能包含授权信息，必须视为密码处理。
Sidecar 不额外设置令牌或登录界面。AT 原有授权仍由上述订阅地址及其生成的插件配置提供。

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

编辑 `.env`，至少填写 AT 订阅地址：

```dotenv
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:latest
T4_PORT=8765
T4_SOURCES_FILE=/app/config/sources.json
ALIST_TVBOX_SUBSCRIPTION_URL=请替换为AT完整订阅地址
T4_WORKER_TIMEOUT=15
T4_SUBSCRIPTION_CACHE_TTL=30
T4_PLUGIN_DIR=/data/plugins
T4_UPLOAD_MAX_BYTES=2000000
T4_SERVICE_NAME=t4-sidecar
T4_VERSION=0.1.0
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

页面和健康检查均无需额外令牌。页面打不开时，先在 NAS 上执行：

```bash
curl -fsS http://127.0.0.1:8765/t4/health
```

### 页面操作

1. 打开页面，Sidecar 会自动读取容器中的 source 和 AT 订阅配置，不需要填写额外 Token。
2. AT 订阅插件列表会自动读取；需要立即同步 AT 变更时点击“重新读取 AT 订阅”。
3. 点击顶部的“复制影视订阅地址”，在 FM 影视点播配置中导入；无需逐个选择插件或填写 `site`。
4. 需要诊断时，在 AT 订阅下选择插件，点击“首页冒烟”测试首页。
5. “复制插件 API”只复制单个站点的 `api`，不是可导入点播配置的完整订阅。
6. 修改 `.env` 后先重新创建容器，再刷新页面。

页面中的“上传本地插件”支持标准 Python `Spider` 和 Sidecar JavaScript 插件。上传后会自动注册
`local-uploaded` source，并持久化到 Docker volume；不需要手工编辑 `sources.json`。

Python 插件可以继续使用 ATVP 常见的 `from base.spider import Spider` 导入方式；JavaScript
插件按 Sidecar 合同导出 `homeContent`、`categoryContent`、`detailContent`、`searchContent`、
`playerContent`，需要追更或检查更新时再导出 `action`。这不是对任意 Drpy/TVBox `rule`
文件的无条件兼容，上传后应先点击页面的“首页冒烟”，再使用 T4 的详情和播放接口验证完整链路。

本地插件列表每行提供上移、下移、重命名、刷新运行实例、首页冒烟、复制地址和删除按钮，
图标悬停会显示名称。删除前需要确认；重命名和排序不改变 FM 地址，重启后仍保留。
同内容重复上传不会新增插件；同名不同内容会自动编号。刷新运行实例会校验当前文件并回收旧实例，
下一次调用重新初始化；“刷新本地列表”只重载列表。删除最后一个插件会移除 `local-uploaded` source。

这些管理操作仅影响 Sidecar 自己存储的文件，不修改 AT 原始插件。AT 插件仍由 AT 管理，
修改后在此页面强制重读订阅。不要删除 Docker volume，否则上传文件和名称、顺序都会丢失。
保持 Compose 默认的单 Gateway 进程，不要让多个 Uvicorn worker 共同写入插件目录。

页面会标注不能转成标准 T4 的本地能力或页面入口，例如 `csp_Push`、`csp_WebHome`。这类条目不会被当作可播放的标准 VOD 插件。

### 访问范围

顶部复制的影视订阅可直接导入，不需要额外令牌或认证请求头。
管理面可以上传和执行插件，应仅供可信的家庭网络使用；远程访问复用现有 VPN 或反向代理。

### 页面常见错误

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `400` | 缺少 `source` 或多插件订阅缺少 `site` | 先确认 source ID，再选择具体插件 |
| `501` | 插件只有 AT 客户端本地能力，没有 AT 服务端 T4 路由 | 改用支持标准 T4 的插件 |
| `502` | AT 订阅、插件 loader 或上游站点失败 | 检查 AT 地址、订阅有效期和容器日志 |
| `400` | 上传文件语法或插件合同不合格 | 使用包含 `Spider` 类的 `.py`，或使用 Sidecar JavaScript 导出合同 |

## 六、在 T4 客户端中使用

FM 影视优先导入管理台顶部复制的完整点播订阅。地址固定为：

```text
http://NAS_IP:8765/t4/config.json
```

影视无需额外配置令牌或认证请求头。
下面的显式模块接口仅供开发调试，不应作为完整点播订阅导入。

直接调试模块 API：

```text
http://NAS_IP:8765/t4/api/home?source=env-at&site=csp_BiliBili
```

当一个 AT 订阅包含多个插件时，必须带上 `site` 参数；`csp_BiliBili`、`csp_Media`
等值来自订阅中的插件 `key`。

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

### 已有 NAS 实例升级

先通过 `docker inspect` 核对 `com.docker.compose.project.working_dir`、
`com.docker.compose.project.config_files` 和 `/data` 挂载，始终在原 Compose 目录升级。
备份原 Compose、`.env` 和数据卷，备份仅留在 NAS 并限制权限；保留旧镜像以便回滚。
只重建 `t4-sidecar`，不要执行 `down -v`，也不要重启 AT 本体。

如果现有凭证没有私有 GHCR 的 `read:packages` 权限，可以使用同一次 GitHub Actions 构建生成的
`nas-image-<完整提交 SHA>` 归档，无需改动账号权限或把令牌保存到 NAS。归档保留 3 天，
包含镜像、`SHA256SUMS` 和 `image-id.json`，可从对应构建的 Artifacts 下载。
将归档解压到 NAS 的独立升级目录后执行：

```bash
sha256sum -c SHA256SUMS
gzip -dc t4-sidecar.tar.gz | docker load
```

核对导入镜像 ID 与 `image-id.json` 一致、镜像的 `org.opencontainers.image.revision`
等于目标提交，再在原 `.env` 中仅更新 `T4_IMAGE`，保留订阅和其他配置，执行：

```bash
docker compose config --quiet
docker compose up -d --no-deps --no-build --pull never t4-sidecar
```

导入的镜像与 GHCR 发布物来自同一构建，不在 NAS 重新构建。升级后检查健康状态、统一订阅和真实插件调用。

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

### 升级后仍显示旧的 Token 输入框或 `gateway token required`

当前版本已删除 Sidecar 额外鉴权，旧环境变量也不会重新启用它。
核对容器是否已使用新镜像，并强制刷新浏览器页面。旧版带签名路径的订阅或插件地址不再使用，
从页面重新复制固定的 `/t4/config.json` 即可。AT 订阅过期或无效仍需在原版 AT 中处理。

### API 返回 `source is required`

请求缺少 `source` 参数。使用环境变量订阅时默认 source ID 是：

```text
env-at
```

### FM 影视如何使用

1. 用 NAS 的局域网 IP 打开 Sidecar 管理台，例如 `http://NAS_IP:8765/t4/`。
2. 点击顶部的“复制影视订阅地址”。
3. 在 FM 影视的“设置 → 配置 → 点播”中粘贴保存；菜单名称随版本可能略有不同。
4. 在影视的站点列表选择 Bilibili、我的追更或上传的插件。

统一订阅为 `http://NAS_IP:8765/t4/config.json`，自动包含所有可执行插件，
不需要逐个复制 API、编写 JSON 或配置 `source`、`site` 和认证请求头。
电视不能使用电脑的 `127.0.0.1` 或 `localhost`。页面通过局域网 IP 打开后，订阅中的站点 API
会使用对应主机地址；反向代理应保留外部 Host，HTTPS 代理还需正确传递并信任转发协议。
新增、删除、排序或改名插件后，在影视中刷新配置即可同步；站点 key 与名称和顺序无关。
`csp_Push`、WebHome 等非服务端 VOD 能力不会进入 type=4 站点列表。

逐插件“复制插件 API”生成的是站点 `api` 地址，不是完整点播订阅。该地址接收 `t`、`ids`、
`wd`、`play` 等标准参数，并支持 URL-safe Base64 分类筛选及 GET、JSON POST、表单 POST。

FM 地址只负责绑定插件，不携带 Sidecar 访问钥匙或 AT 订阅令牌。请使用电视可访问的 NAS 局域网地址。

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
- 不把 `8765` 暴露到公网；需要公网访问时，先放在 HTTPS 反向代理、访问控制或 VPN 后面。
- T4 返回的详情和播放 ID 是 Sidecar 的 opaque ID，不应被当成永久链接保存。
