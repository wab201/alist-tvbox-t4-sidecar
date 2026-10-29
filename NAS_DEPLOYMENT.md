# NAS 部署指南

当前版本 `0.3.0`，提供网盘服务端通路、可选 AT 内网回源、官方远程插件识别和媒体取消释放。
升级后重新打开影片详情再选集；继续使用原统一订阅，不需要新增 Token。
普通媒体连同必要请求头交给客户端；生产容器不安装 FFmpeg、不转码、不解码。
升级到包含该功能的镜像后，
在 `/t4/` 页面点击“开始检测”即可检查全部订阅插件，不需要新增 Token。
报告默认随现有 `/data` 卷保存，操作和验收边界见 [兼容门禁说明](COMPATIBILITY_GATE.md)。
正常部署使用：

```text
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:latest
```

需要固定版本时使用版本标签或版本日志中经核验的 `sha-<提交前七位>`，
不要用镜像创建时间推断版本。当前版本镜像：

```text
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:0.3.0
```

源码公开与 GHCR 包公开是两件事。如果镜像匿名拉取受限，可以使用本页的公开 Release
归档或按 [源码构建指南](BUILDING.md) 在本机构建，不需要给 Sidecar 添加 GitHub 凭证。
发布及 NAS 验证结果见 [版本记录](CHANGELOG.md)，未完成的性能、全部驱动和真机长播
不会因升级自动变成已通过。网盘模式默认直连。

历史记录：`0.2.3 / sha-bb19317` 已验证 NAS、24 组云盘取链及首中尾 Range。
2026-10-28 已核验 `0.2.2` 固定镜像 `ghcr.io/wab201/alist-tvbox-t4-sidecar:sha-503b2ec`：
NAS 健康、Web、173 站点订阅、55 个运行文件及 PY/JS 管理检查通过。
18 个内置条目七阶段抽样仍有 11 失败、1 受阻、5 不适用，只有 1 个整链通过；
截图对应四集的网络抽样通过不代表全部片源或 FM 真机兼容。
该旧版检测漏掉客户端 `ac=detail`，不能以历史四集通过推断客户端无误。
本轮在线身份及重新验收见 [版本记录](CHANGELOG.md)。
下面仅使用示例主机和域名，不包含实际部署地址。

本文适用于群晖、威联通、绿联及其他支持 Docker Compose 的 NAS。部署目标是：在 NAS 上直接拉取 GHCR 镜像，通过环境变量读取 AT 订阅，把 Sidecar 暴露为独立的 T4 服务。

## 一、部署前准备

需要准备：

1. NAS 已安装 Docker 或 Container Manager。
2. NAS 可以访问 `ghcr.io` 和 AT 服务地址。
3. 已在 AT 中生成一条可用的完整订阅地址。
4. 发布镜像默认是 `linux/amd64`；ARM NAS 参见源码构建指南，不直接导入 x86 归档。

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

可将仓库中的 `docker-compose.yml` 和 `.env.example` 上传到 NAS 项目目录，
或匿名克隆公开源码：

```bash
git clone https://github.com/wab201/alist-tvbox-t4-sidecar.git .
cp .env.example .env
chmod 600 .env
```

如果 NAS 未安装 Git，直接通过文件管理器上传这两个文件即可；不要把 GitHub
Token 写入克隆地址。不要覆盖已有部署的 `.env`。

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
T4_COMPATIBILITY_REPORT=/data/reports/compatibility-latest.json
```

不要把真实 `.env` 上传到 GitHub、截图、工单或公开日志。

## 三、获取镜像

优先尝试 `docker compose pull t4-sidecar`。GHCR 包可见性独立于源码仓库；
若返回 `denied`，可直接使用公开 Release 的同次构建镜像归档，无需登录：

```bash
mkdir -p releases/v0.3.0
cd releases/v0.3.0
curl -fL -O https://github.com/wab201/alist-tvbox-t4-sidecar/releases/download/v0.3.0/t4-sidecar.tar.gz
curl -fL -O https://github.com/wab201/alist-tvbox-t4-sidecar/releases/download/v0.3.0/SHA256SUMS
curl -fL -O https://github.com/wab201/alist-tvbox-t4-sidecar/releases/download/v0.3.0/image-id.json
sha256sum -c SHA256SUMS
gzip -dc t4-sidecar.tar.gz | docker load
cd ../..
```

归档导入后会显示 `sha-<提交前七位>` 标签，将 `.env` 的 `T4_IMAGE` 改为这个完整标签，
然后跳过 pull，执行：

```bash
docker compose config --quiet
docker compose up -d --no-deps --no-build --pull never t4-sidecar
```

归档适用于 `linux/amd64`。下载失败时不要继续导入；先检查 HTTP 状态和校验值。
如果已有可用 GHCR 账号，也可登录私有包：

```bash
read -s GHCR_TOKEN
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io --username 你的GitHub用户名 --password-stdin
unset GHCR_TOKEN
```

令牌至少需要 `read:packages`。如果 GitHub 组织或仓库启用了额外 SSO，还需要按组织要求授权该令牌。

这一步不是 Sidecar 的额外鉴权，也不是必须配置。不要把含订阅授权信息的 AT 地址改成公开地址。

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
{"status":"ok","version":"0.3.0"}
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

### 网盘服务端通路

在 AT 订阅下选择“服务端加速”并保存；若 Sidecar 与 AT 在同一 NAS，
可填从 Sidecar 容器可访问的 AT 内网地址，例如 `http://at:4567`。
容器网络不相通时应先配置共同 Docker 网络或使用可达的 NAS 地址，
`127.0.0.1` 不是另一个容器。地址只填 AT 服务根地址或其真实前缀，
不要填 PowerList 端口、订阅地址或带凭证的 URL。

Sidecar 只接入 AT 实际返回的 `/p`，保留原网盘参数，复用 AT/PowerList 的分片层，
不增加第二层下载器。播放设置保存在 `/data/plugins/playback.json`，
不修改 AT；选择直连并保存可恢复新取链的默认行为，不强制中断已有播放。
公网中转可能比直链更慢，内网与原版客户端完整性能需分别验证。
完整合同和环境变量说明见 README 的“网盘服务端加速”。

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

页面中的 AT 推送标记为“原生推送”，复制按钮提供完整影视订阅，首页冒烟不可用。
它通过原版 `type=3 + csp_Push + spring.jar` 运行，不是服务端 VOD 插件。
WebHome 等页面入口仍标为非标准 T4，不导出为可播放站点。

### 访问范围

顶部复制的影视订阅可直接导入，不需要额外令牌或认证请求头。
管理面可以上传和执行插件，应仅供可信的家庭网络使用；远程访问复用现有 VPN 或反向代理。

### 页面常见错误

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `400` | 缺少 `source` 或多插件订阅缺少 `site` | 先确认 source ID，再选择具体插件 |
| `501` | 请求了客户端专有能力，或片单导航不存在的详情/播放接口 | AT 推送导入完整订阅；片单卡片交由影视按标题搜片 |
| `502` | AT 订阅、插件 loader 或上游站点失败 | 检查 AT 地址、订阅有效期和容器日志 |
| `400` | 上传文件语法、ZIP 结构或插件合同不合格 | 使用包含 `Spider` 类的 `.py`、Sidecar JavaScript，或包含 `main.py/main.js` 及配套文件的 ZIP |

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

回滚就是把 `T4_IMAGE` 改回上一个已验证且仍保留的镜像，再重建 Sidecar。
使用镜像部署时不要混用 `up --build`；有意从源码构建请按 [构建指南](BUILDING.md)，
使用独立本地标签并明确 `--pull never`。

### 已有 NAS 实例升级

先通过 `docker inspect` 核对 `com.docker.compose.project.working_dir`、
`com.docker.compose.project.config_files` 和 `/data` 挂载，始终在原 Compose 目录升级。
备份原 Compose、`.env` 和数据卷，备份仅留在 NAS 并限制权限；保留旧镜像以便回滚。
只重建 `t4-sidecar`，不要执行 `down -v`，也不要重启 AT 本体。

如果镜像拉取受限，可使用版本 Release 的镜像归档，或同一次 GitHub Actions 构建生成的
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

- `Host`，保留外部域名和端口
- `X-Forwarded-For`
- `X-Forwarded-Proto`
- GET 和 POST 请求体

### 地址填写和路径拼接

Sidecar 的路由本身已经包含 `/t4` 前缀。反向代理的配置重点是：

```text
前端域名：<T4_DOMAIN>
前端端口：<PUBLIC_PORT>
后端地址：http://<NAS_IP>:8765
访问路径：/t4/
```

推荐的请求链路如下：

```text
https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/
        ↓
http://<NAS_IP>:8765/t4/
```

健康检查、订阅和插件接口也必须保持同样的前缀：

```text
/t4/health
/t4/config.json
/t4/api/sources
/t4/fm/{source}/{plugin}/
```

不要把 `http://<NAS_IP>:8765/t4` 作为后端地址，同时又让反代保留或追加客户端的 `/t4`。这种配置可能让首页通过路径替换打开，但页面脚本继续请求 `/t4/...` 时，被拼接成：

```text
客户端：/t4/api/sources
错误后端：/t4/t4/api/sources
结果：HTTP 404
```

这也是“管理台页面能打开，但右上角显示不可用、订阅和插件一直加载失败”的典型原因。

如果需要从域名根路径 `/` 进入，可以仅配置一个重定向：

```nginx
location = / {
    return 302 /t4/;
}
```

随后仍由 `/t4/` 规则负责转发。不要通过“后端地址带 `/t4`”来代替这个重定向。

通用 Nginx 示例：

```nginx
server {
    listen 80;
    server_name <T4_DOMAIN>;

    location = / {
        return 302 /t4/;
    }

    location /t4/ {
        proxy_pass http://<NAS_IP>:8765/t4/;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
```

Lucky 或其他反代软件没有单独的“路径重写”选项时，使用“后端地址不带 `/t4` + 客户端访问 `/t4/`”的方式最简单。若软件支持“前缀转发”，要确认 `/t4/` 只被保留一次。

### HTTPS 订阅能导入但切源失败

公网通过 HTTPS 访问、后端通过 HTTP 转发是正常部署方式。反代需传入
`X-Forwarded-Proto: https`，同时保留客户端 `Host` 中的域名和端口。
Nginx 示例使用 `$http_host`，避免 `$host` 丢失非标准端口；
Lucky 的“使用目标地址 Host 请求头”保持关闭，以保留公网 Host。

Sidecar 使用请求的协议和 Host 自动生成订阅中的插件 API，不需要手工配置公网域名。
Docker 镜像和 Compose 均默认设置 `FORWARDED_ALLOW_IPS=*`，让 Uvicorn 读取反代传入的协议头。
如果使用自定义启动命令，应保留 Uvicorn 默认开启的代理头处理，不要添加 `--no-proxy-headers`。
HTTPS 终止代理直接转发到 Sidecar 的 `8765` 端口即可。

可用示例：

```text
订阅：https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/config.json
api： https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/fm/env-at/csp_Media/
```

错误示例：

```text
订阅：https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/config.json
api： http://<T4_DOMAIN>:<PUBLIC_PORT>/t4/fm/env-at/csp_Media/
```

后一种情况会向 HTTPS 端口发送明文 HTTP，可能直接断开连接。客户端可能显示
`DioException`、`HttpException` 或 `statusCode: null`，而没有 HTTP 状态码。
只检查订阅返回 `200` 不足以判断站点可用，还需核对并请求其中的 `api`。

已部署旧镜像时，可在 Compose 的 `t4-sidecar.environment` 中加入以下一项，无需改动 AT 配置：

```yaml
FORWARDED_ALLOW_IPS: "*"
```

然后应用配置并重新创建 Sidecar：

```bash
docker compose config --quiet
docker compose up -d --no-deps --no-build --pull never t4-sidecar
```

仅执行 `docker restart` 不会载入修改后的 Compose 环境变量。
修正后通过公网 HTTPS 地址重新读取订阅，确认 type=4 站点的 `api` 使用相同的协议、域名和端口，
再在影视中刷新点播配置。局域网 HTTP 直连仍可照常使用。

反代调整后，在客户端或 NAS 上检查：

```bash
curl -i https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/health
curl -i https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/api/sources
curl -i https://<T4_DOMAIN>:<PUBLIC_PORT>/t4/config.json
```

如果直接访问 `http://<NAS_IP>:8765/t4/health` 正常，而公网地址返回 `404`，问题在反代路径规则；如果内网地址本身也返回 `404`，先重新创建或重启最新 Sidecar 容器。

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

源码公开不等于镜像包公开。先使用第三节的 Release 归档或源码构建；
已有 GHCR 凭证时再检查账号和 `read:packages` 权限：

```bash
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
片单导航自动保留首页推荐、分类筛选及 `indexs=1`：FongMi 点击标题搜索其他片源。
忽略 `indexs` 的客户端请求片单详情时，Sidecar 检索其他源并汇总真实播放组，
不会把豆瓣/TMDB 条目直接作为媒体播放。
`csp_Push` 以原生 type=3 站点加入同一个订阅；WebHome 等页面入口仍不导出。

逐插件“复制插件 API”生成的是站点 `api` 地址，不是完整点播订阅。该地址接收 `t`、`ids`、
`wd`、`play` 等标准参数，并支持 URL-safe Base64 分类筛选及 GET、JSON POST、表单 POST。

FM 地址只负责绑定插件，不携带 Sidecar 访问钥匙或 AT 订阅令牌。请使用电视可访问的 NAS 局域网地址。

### AT 推送如何使用

1. 升级 Sidecar 后，在影视中刷新 `/t4/config.json`。
2. 在原版 AT 设备管理中选择已连接的影视设备，按原流程推送链接。
3. 影视通过固定 `push_agent` 加载 AT 原版 `spring.jar`，继续使用原来的解析和网盘能力。

不需要在 Sidecar 配置设备、Gateway Token 或额外推送地址。
Web 中选择“原生推送”后复制的是完整订阅，不能将它作为单插件 type=4 API 使用。
推送源默认可能不出现在普通选源列表中，保留原版 `changeable` 设置，不影响客户端按固定 key 处理推送。
多个 AT source 只导出第一个可用推送站点，顺序由 source 配置决定。

这项兼容需要影视支持 type=3 JAR，且影视设备能访问原 AT 地址。Sidecar 读取原订阅的 `jar/ext`，
站点缺省 JAR 时自动采用全局 `spider`，不要求用户手工拼接。两者均缺失时不会生成伪造站点。
原生推送所需的 AT `ext`（可能含上游授权）会原样交给客户端；它不会出现在管理页面或能力快照中。
请勿公开分享完整订阅。纯 type=4 客户端不能替代原版推送运行时。

当前自动化和真实 AT 接口验证不等同于已完成 Android/电视的设备推送、网盘播放或长播验收。

### API 返回订阅不可用或 `502`

检查三点：

1. NAS 能否访问 AT 地址。
2. `.env` 中的 `ALIST_TVBOX_SUBSCRIPTION_URL` 是否完整且未被换行截断。
3. AT 订阅是否仍然有效，插件 loader 和依赖站点是否可访问。

查看日志时不要把日志原样发布到公开位置：

```bash
docker compose logs --tail=200 t4-sidecar
```

### 有详情和分集，但播放失败

打开 Web 底部“播放链路诊断”，选择插件、输入片名后检查。
勾选实时日志，在实际客户端复现，再导出脱敏日志；不需要配置其他 Token。
每个响应的 `X-T4-Trace-ID` 可关联列表、详情、解析、AT/Worker 和媒体转发事件。

Bilibili 未声明客户端身份时按普通视频请求 AT；不再默认声称客户端支持 DASH。
AT 仍返回内嵌 DASH 时，Sidecar 提供 HTTP MPD 和音视频 Range 转发，不进行转码。
反代必须转发整个 `/t4/`，包括 `/t4/media/`，保留 `Range`/`If-Range`。
如果实际播放器不支持 DASH，应使用普通 MP4/HLS 路径；服务端探测通过并非真机解码证明。

结构化日志保存于现有 `/data` 数据卷，默认文件为 `/data/logs/playback.jsonl`，
单文件约 2 MB，轮转两份备份。可以通过 Web 导出，或在 NAS 查看：

```bash
docker compose exec t4-sidecar tail -n 200 /data/logs/playback.jsonl
```

日志只保存阶段、状态、耗时和关联 ID；不会保存 AT 订阅、Cookie、原始地址或插件 stderr 正文。
普通直连媒体不会经过 Sidecar，只有诊断探测和 HTTP DASH 转发具有服务端媒体请求记录。
新代码只有在构建并更新镜像后才会出现在 NAS，工作区测试不能当作 NAS 已升级。

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
