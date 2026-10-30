# 部署指南

[功能介绍](README.md) · [使用指南](USAGE.md)

支持 Docker Compose 的 NAS 和 Linux 主机。只需 AT 生成的完整订阅地址，
不需要 AT 登录密码或额外 Gateway Token。本文使用示例地址，不含实际部署信息。

## 准备配置

在 NAS 的专用 Docker 目录中新建项目目录；已有实例务必使用原 Compose 目录，
不要覆盖现有 `.env` 或另建空数据卷。

```bash
mkdir -p /volume1/docker/alist-tvbox-t4-sidecar
cd /volume1/docker/alist-tvbox-t4-sidecar
git clone https://github.com/wab201/alist-tvbox-t4-sidecar.git .
cp .env.example .env
chmod 600 .env
```

路径按 NAS 实际磁盘调整。没有 Git 时，上传仓库的 `docker-compose.yml` 和 `.env.example`
即可使用发布镜像；源码构建需要完整源码。Windows 使用 `Copy-Item .env.example .env`。

编辑 `.env`，最少填写：

```dotenv
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:0.3.5
T4_PORT=8765
ALIST_TVBOX_SUBSCRIPTION_URL=https://at.example/sub/REPLACE_ME/0
```

将示例订阅替换为 AT 生成的完整地址，其他参数可保持默认。
订阅地址包含授权信息，不要提交到 Git、写入镜像或公开分享。

## 获取镜像并启动

以下三种方式任选一种。发布镜像及 Release 归档为 `linux/amd64`；
ARM NAS 可在原生 ARM 主机尝试源码构建，需自行验证运行时依赖，不要直接导入 x86 归档。

### 拉取发布镜像

```bash
docker compose config --quiet
docker compose pull t4-sidecar
docker compose up -d --no-deps --no-build t4-sidecar
```

可用镜像标签为 `0.3.5`、`latest` 或对应构建的 `sha-<提交前七位>`。
源码仓库公开不等于 GHCR 包公开；当前匿名拉取可能返回 `denied`。
无需为此配置新凭证，直接使用下一种镜像归档方式或源码构建即可。

### 导入公开镜像归档

现有公开 Release 归档为 `0.3.0`，不是最新的 `0.3.5`。
更新到 `0.3.5` 可使用成功构建的 Actions 归档：在仓库 Actions 中选择
对应版本提交的绿色构建，下载 `nas-image-<完整提交号>` 并解压。
使用 GitHub CLI 也可下载，需要有读取 Actions 的登录权限：

```bash
gh run list --repo wab201/alist-tvbox-t4-sidecar --workflow container.yml --branch main --status success --limit 5
gh run download RUN_ID --repo wab201/alist-tvbox-t4-sidecar --pattern 'nas-image-*' --dir releases/current
cd releases/current
sha256sum -c SHA256SUMS
gzip -dc t4-sidecar.tar.gz | docker load
cd ../..
```

将 `RUN_ID` 换为对应版本的构建编号，`.env` 的 `T4_IMAGE` 使用 `docker load`
输出的固定 `sha-...` 标签，再执行下方不拉取镜像的启动命令。
Actions 归档仅保留 3 天；没有访问权限或归档过期时，使用前述镜像拉取或下文源码构建。

如需使用保留的 `0.3.0`，从项目目录下载同一版本的镜像和校验文件：

```bash
mkdir -p releases/v0.3.0
cd releases/v0.3.0
curl -fL -O https://github.com/wab201/alist-tvbox-t4-sidecar/releases/download/v0.3.0/t4-sidecar.tar.gz
curl -fL -O https://github.com/wab201/alist-tvbox-t4-sidecar/releases/download/v0.3.0/SHA256SUMS
curl -fL -O https://github.com/wab201/alist-tvbox-t4-sidecar/releases/download/v0.3.0/image-id.json
sha256sum -c SHA256SUMS
```

仅在下载成功且校验通过后导入：

```bash
gzip -dc t4-sidecar.tar.gz | docker load
cd ../..
```

将 `.env` 中的镜像改为归档导入的固定标签，`v0.3.0` 对应：

```dotenv
T4_IMAGE=ghcr.io/wab201/alist-tvbox-t4-sidecar:sha-57a3cda
```

然后启动，不再执行 pull：

```bash
docker compose config --quiet
docker compose up -d --no-deps --no-build --pull never t4-sidecar
```

### 从源码构建 Docker 镜像

需要完整源码、Docker 和 Compose v2。将 `.env` 中的镜像改为本地标签：

```dotenv
T4_IMAGE=alist-tvbox-t4-sidecar:local
```

在仓库根目录执行：

```bash
docker compose config --quiet
docker compose build --pull t4-sidecar
docker compose up -d --no-deps --no-build --pull never t4-sidecar
docker compose exec t4-sidecar python -m sidecar.worker.dependencies
```

Dockerfile 安装 Python 3.12、Node.js 和基础依赖，不安装 FFmpeg，也不启动或修改 AT。

### 运行资源

Linux 容器通过单线程 forkserver 预加载基础库，再启动独立插件进程，
共享未修改的基础内存页；不在带有请求线程的网关进程中直接 fork。
插件代码、缓存、鉴权上下文和播放状态仍彼此隔离，无需增加配置。
Windows 本地开发使用独立解释器进程。

插件调用本地代理时才启动监听服务；空闲监听使用阻塞等待，不定时轮询。
二进制内容保持流式交付，已缓存内容的 Range 切片不额外整块复制。
服务端不解码、不转码，也不通过限制播放带宽或并发降低资源数字。
Compose 已启用 `init: true`，用于转发退出信号及回收孤儿子进程；
自行使用 `docker run` 时增加 `--init`。

内存回收自动启用，无需额外环境变量：

- RPC 完成后释放上条请求、响应和解析副本，避免空闲期间持有大型列表。
- 插件空闲至少 30 秒后整理不可达对象，并在支持的 Linux 分配器上归还空闲堆页；
  每实例两次整理请求至少间隔 5 分钟，无新业务活动时不反复执行完整回收。
  后台分批调度，避免大量插件同时整理造成 CPU 峰值。
- 插件代理的媒体流从开始到关闭均标记为活动，整理不清除插件缓存、不改变代理地址。
- 只执行过搜索、首页或分类的插件，空闲 3 分钟后每批最多退出 8 个实例；
  默认每 15 秒检查一次。全源搜索结束后进程会逐步减少，并非所有实例在同一秒退出。
  选择旧卡片时自动重建插件，必要时恢复该卡片对应的首页、分类或搜索上下文。
  浏览记录保存在临时磁盘，不把整份插件响应长驻内存。
- 公开 ID 映射在同一请求追踪内按源、插件、类型和原始 ID 去重，使用有界热缓存，
  仅冷数据转存当前运行期的临时数据库；
  释放热缓存后旧详情、分集和关联日志仍可查询。临时数据库随服务退出删除，不写入订阅文件。
  运行期不同 ID 会占用临时磁盘；若把 `/tmp` 挂载为内存盘，该部分也会计入容器内存。
- 网关在请求和媒体流结束后的空闲期整理内存，清除已过期的媒体映射；
  健康探针、Web 页面和诊断轮询不会阻止回收。服务退出时同时关闭检测实例和临时 ID 存储。

为保持网盘详情和连续播放上下文，已经执行详情、播放、动作或启用代理的实例不会强制退出，
仍被插件引用的缓存与状态也不会强制清除，因此不能保证占用始终回落到启动值。
插件重新初始化及必要的浏览恢复存在冷启动开销；上游已删除或动态失效的卡片不能靠回放恢复。
需要手动重建某个插件实例时，可在 Web 插件管理中“刷新”，但应先停止该插件的播放。
查看资源使用可运行：

```bash
docker stats --no-stream
curl -fsS http://127.0.0.1:8765/t4/api/diagnostics/resources
```
构建上下文排除 `.env`、Git 历史和测试报告，真实配置仅在运行时传入。
构建不会自动推送；本地镜像不要覆盖发布标签。依赖采用版本范围，
精确复用部署时应保留镜像归档或固定镜像 digest，而非重复解析浮动依赖。

## 确认服务可用

```bash
docker compose ps
curl -fsS http://127.0.0.1:8765/t4/health
```

健康响应应包含 `"status":"ok"` 和版本号。若修改了 `T4_PORT`，使用修改后的端口。

用局域网设备访问 `http://NAS_IP:8765/t4/`，点击“复制影视订阅地址”导入影视。
插件上传、管理、网盘播放和检测操作见 [使用指南](USAGE.md)。

## 配置项

Compose 自动读取项目目录中的 `.env`。

| 变量 | 默认值 / 用途 |
| --- | --- |
| `T4_IMAGE` | 发布镜像地址，Compose 默认使用 `latest` |
| `T4_PORT` | NAS 对外端口，默认 `8765` |
| `ALIST_TVBOX_SUBSCRIPTION_URL` | AT 完整订阅地址；仅使用本地插件时可留空 |
| `T4_CLOUD_PLAYBACK_MODE` | `direct`，可选 `server`，作为环境订阅的初始播放模式 |
| `ALIST_TVBOX_INTERNAL_URL` | 可选 AT 内网服务地址，不是 PowerList 端口或订阅路径 |
| `T4_WORKER_TIMEOUT` | 插件请求超时秒数，默认 `15` |
| `T4_SUBSCRIPTION_CACHE_TTL` | 订阅缓存秒数，默认 `30` |
| `T4_PLUGIN_DIR` | 插件和管理配置目录，默认 `/data/plugins` |
| `T4_UPLOAD_MAX_BYTES` | 单次上传上限，默认 `2000000` |
| `T4_SOURCES_FILE` | 文件型来源配置，默认 `/app/config/sources.json`，只用环境订阅无需创建 |
| `T4_SERVICE_NAME` | 健康检查中的服务名，默认 `t4-sidecar` |

Web 保存的播放设置优先于环境初始值，按订阅独立保存。
内网 AT 地址必须从 Sidecar 容器可达；另一个容器不是 `127.0.0.1`，
需要共同 Docker 网络或可访问的 NAS 地址。多套 AT 并存时确认实际媒体后端，不能猜端口。

修改 `.env` 后重新创建容器，单纯 `docker restart` 不会载入新环境：

```bash
docker compose config --quiet
docker compose up -d --no-deps --no-build --pull never --force-recreate t4-sidecar
```

## 数据与备份

默认命名卷 `t4_data` 挂载到 `/data`，实际名称可能带 Compose 项目前缀。

| 路径 | 内容 |
| --- | --- |
| `/data/plugins` | 上传插件、名称与排序、`playback.json` 播放设置 |
| `/data/logs/playback.jsonl` | 脱敏播放日志，约 2 MB 轮转，保留两份备份 |
| `/data/reports/compatibility-latest.json` | 最近兼容检测报告 |

备份原 Compose 文件、`.env` 和实际数据卷，备份仅保存在受限目录。
重建 Sidecar 不删除卷；不要执行 `docker compose down -v`。
保持默认单 Gateway 进程，不让多个 Uvicorn worker 共写插件目录。

多订阅可复制 [来源配置示例](config/sources.example.json) 到 `config/sources.json`，
配置不同的 `id`、名称和完整 `subscription_url`，并在 Compose 中启用只读挂载：

```yaml
volumes:
  - t4_data:/data
  - ./config/sources.json:/app/config/sources.json:ro
```

只使用文件配置时清空环境订阅，避免重复导入同一来源。文件同样可能包含授权信息。
自定义加密插件的 keyring 可按 Compose 注释只读挂载，并通过
`ATV_SECSPIDER_KEYRING` 指定容器内路径；不需要自定义 keyring 时保持默认。

## 反向代理

推荐使用独立 T4 域名，并原样转发整个 `/t4/`：

```text
公网：https://t4.example/t4/config.json
后端：http://NAS_IP:8765/t4/config.json
```

Lucky 等反代软件的后端地址填写 `http://NAS_IP:8765`，不要重复添加 `/t4`。
保留外部 Host（含非标准端口）及 `X-Forwarded-Proto`，
否则订阅中的站点 API 可能生成错误的协议、域名或端口。

Nginx 示例，放在实际处理外部请求的虚拟主机中：

```nginx
location = / {
    return 302 /t4/;
}

location /t4/ {
    proxy_pass http://NAS_IP:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
}
```

HTTPS 由其他代理终止时，该代理需传递实际外部协议；不要把 HTTPS 误标为 HTTP。
镜像默认启用 Uvicorn 代理头处理，Compose 设置 `FORWARDED_ALLOW_IPS=*`。
同时保留 GET/POST 请求体和 `Range`、`If-Range`，不要遗漏 `/t4/media/`、`/t4/proxy/`
或 `/t4/images/`。
若 AT 自身也使用 `/t4/`，不要将同域整段路径同时分配给两个后端，改用独立域名。

反代后检查 `/t4/health`、`/t4/config.json`，再检查订阅内站点 API 是否使用相同公网地址。
`/t4/t4/...` 的重复前缀会导致 `404`；HTTPS 入口生成 HTTP API 可能造成切源失败。
修正后在影视中重新刷新完整订阅。

使用完整仓库时，也可启用其提供的独立 Nginx 入口：

```bash
docker compose --profile proxy up -d --no-build --pull missing
```

默认访问 `http://NAS_IP:8766/t4/`。它不会修改现有 AT 反代；归档部署应先按前文启动
Sidecar，镜像使用已导入的固定标签。
管理页可执行上传代码，远程访问应使用可信 VPN 或有访问控制的反代，不直接裸露管理端口。

## 升级与回滚

始终在原 Compose 目录操作，保留原 `.env`、数据卷及可回滚的旧镜像，不重启 AT 本体。
通过 `docker compose ps`、`docker compose images` 核对当前实例。

升级到 `0.3.5` 时保留现有 Compose 自定义配置，并确认
`services.t4-sidecar.init: true` 已启用；它不影响原端口、数据卷或订阅鉴权。

拉取方式：将 `T4_IMAGE` 改为目标版本，执行：

```bash
docker compose pull t4-sidecar
docker compose up -d --no-deps --no-build t4-sidecar
```

归档方式：下载目标版本并校验、导入，将 `T4_IMAGE` 改为其固定标签，执行带
`--pull never` 的启动命令。源码方式：更新完整源码，以独立本地标签构建再启动。
部署新版本前按前文备份配置与数据，避免直接覆盖唯一的回滚镜像。

回滚时将 `T4_IMAGE` 改回已保留的旧镜像，再仅重建 Sidecar。
升级后检查健康、统一订阅和实际插件；在影视中刷新配置、重新进入详情取链。

## 不用 Docker，直接运行源码

需要 Python 3.11+、Node.js，以及可执行 `node` 的 PATH。发布容器使用 Python 3.12。
在仓库根目录执行，订阅值替换为真实地址，仅保存在当前进程环境。

Linux/macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
export T4_PLUGIN_DIR="$PWD/.local/plugins"
export T4_LOG_FILE="$PWD/.local/logs/playback.jsonl"
export T4_COMPATIBILITY_REPORT="$PWD/.local/reports/compatibility.json"
export ALIST_TVBOX_SUBSCRIPTION_URL='https://at.example/sub/REPLACE_ME/0'
python -m sidecar.worker.dependencies
python -m uvicorn sidecar.gateway.main:app --host 0.0.0.0 --port 8765 --no-access-log
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
$env:T4_PLUGIN_DIR = Join-Path $PWD '.local/plugins'
$env:T4_LOG_FILE = Join-Path $PWD '.local/logs/playback.jsonl'
$env:T4_COMPATIBILITY_REPORT = Join-Path $PWD '.local/reports/compatibility.json'
$env:ALIST_TVBOX_SUBSCRIPTION_URL = 'https://at.example/sub/REPLACE_ME/0'
python -m sidecar.worker.dependencies
python -m uvicorn sidecar.gateway.main:app --host 0.0.0.0 --port 8765 --no-access-log
```

直接运行 Python 不自动加载 `.env`，不要添加多个 `--workers`。
需要分发安装包时可执行 `python -m pip wheel --no-deps --wheel-dir dist .`；
wheel 包含 Web 与运行时资源，但不包含 Node.js。

## 部署排查

```bash
docker compose ps
docker compose logs --tail=200 t4-sidecar
docker compose exec t4-sidecar python -m sidecar.worker.dependencies
curl -fsS http://127.0.0.1:8765/t4/health
```

健康正常但订阅 `502` 时，检查 Sidecar 到 AT 的网络、完整订阅及授权有效性。
内网正常而公网 `404` 时检查反代路径，不必重新配置插件。
页面仍显示旧界面时核对运行镜像并强制刷新浏览器。
插件和播放问题使用 [使用指南](USAGE.md) 中的兼容检测与脱敏日志，不公开粘贴凭证或完整日志。

容器日志默认输出结构化 JSON，无需开启 Uvicorn 访问日志：
`runtime.started` 为启动，`runtime.resources` 每分钟报告资源，
`worker.retired` 为浏览实例退出，`worker.context_restored` 为旧卡片上下文恢复，
`worker.memory_reclaimed` / `gateway.memory_reclaimed` 为内存整理。
请求与播放事件保留 `trace_id`、状态、耗时等元信息，不记录完整地址、请求头或 Cookie。
高频日志每 10 秒最多输出 100 条，并报告 `log.suppressed`，完整脱敏事件仍写入轮转文件。
默认 Compose 使用 Docker `local` 日志驱动，每份 10 MB、最多 3 份；升级已有部署时
保留自定义日志驱动，没有配置的可按仓库 Compose 增加该设置后重建 Sidecar。
