# 源码构建与开发

## 选择运行方式

| 场景 | 推荐方式 |
| --- | --- |
| 只在 NAS 使用 | 按 [NAS 指南](NAS_DEPLOYMENT.md) 拉取镜像或导入 Release 镜像归档 |
| 修改源码后部署 | 本文的 Docker 本地构建 |
| 调试、测试插件运行时 | Python 虚拟环境与 Node.js |
| 分发 Python 安装包 | 构建 wheel，另行安装 Node.js |

本文只使用示例地址。真实 AT 订阅、Cookie、keyring 和 NAS 凭证不得进入源码、
构建参数、镜像层或 Git 历史。服务端负责爬虫和网络交付，客户端负责视频解码。

## Docker 本地构建

需要 Docker Engine/Desktop 与 Compose v2。以下命令在仓库根目录执行：

```bash
git clone https://github.com/wab201/alist-tvbox-t4-sidecar.git
cd alist-tvbox-t4-sidecar
cp .env.example .env
chmod 600 .env
```

Windows PowerShell 用 `Copy-Item .env.example .env` 代替 `cp`，权限按本机策略设置。
编辑 `.env`：

```dotenv
T4_IMAGE=alist-tvbox-t4-sidecar:local
T4_PORT=8765
ALIST_TVBOX_SUBSCRIPTION_URL=https://at.example/sub/REPLACE_ME/0
```

构建及启动：

```bash
docker compose config --quiet
docker compose build --pull t4-sidecar
docker compose up -d --no-deps --no-build --pull never t4-sidecar
docker compose exec t4-sidecar python -m sidecar.worker.dependencies
curl -fsS http://127.0.0.1:8765/t4/health
```

`T4_IMAGE` 是本地构建标签，不会自动推送。不要把源码构建产物误标为官方
`latest`。Dockerfile 使用 Python 3.12 和系统 Node.js，安装 Python 基础依赖并
执行运行时依赖检查；不安装 FFmpeg，也不启动 AT。

`.dockerignore` 仅允许 Dockerfile、`pyproject.toml` 和 `sidecar/` 进入构建上下文，
排除 `.env`、`.git`、测试报告、个人凭证和开发缓存。运行配置通过 Compose 的
`env_file` 传入，不使用 `--build-arg` 传递秘密。

数据在 `/data` 卷，重建镜像不清空已上传插件和 Web 播放设置。
查看页面 `http://NAS_IP:8765/t4/`，复制 `/t4/config.json` 到影视点播配置。
本地 `127.0.0.1` 仅供当前机器调试，不是电视可访问地址。

当前发布流水线默认构建 `linux/amd64`；ARM NAS 应在自己的 ARM 主机上源码构建，
不能将 x86 镜像归档当作原生 ARM 镜像。不要仅凭 Dockerfile 推断某架构已经验收。

## Python 本地运行

要求 Python 3.11+、Node.js，以及能运行 `node` 的 PATH。
发布容器使用 Python 3.12；主机版本测试与 Linux 镜像测试分别记录。

### Linux / macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
export T4_PLUGIN_DIR="$PWD/.local/plugins"
export T4_LOG_FILE="$PWD/.local/logs/playback.jsonl"
export T4_COMPATIBILITY_REPORT="$PWD/.local/reports/compatibility.json"
export ALIST_TVBOX_SUBSCRIPTION_URL='https://at.example/sub/REPLACE_ME/0'
python -m sidecar.worker.dependencies
python -m uvicorn sidecar.gateway.main:app --host 127.0.0.1 --port 8765 --no-access-log
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
$env:T4_PLUGIN_DIR = Join-Path $PWD '.local/plugins'
$env:T4_LOG_FILE = Join-Path $PWD '.local/logs/playback.jsonl'
$env:T4_COMPATIBILITY_REPORT = Join-Path $PWD '.local/reports/compatibility.json'
$env:ALIST_TVBOX_SUBSCRIPTION_URL = 'https://at.example/sub/REPLACE_ME/0'
python -m sidecar.worker.dependencies
python -m uvicorn sidecar.gateway.main:app --host 127.0.0.1 --port 8765 --no-access-log
```

直接运行 Python 不自动读取 `.env`，需要进程环境变量。没有订阅也能启动，
用于上传插件或离线合同测试；AT 功能需要有效的上游订阅。
不要在公开终端录屏中输入真实订阅。测试产物和运行目录不提交到 Git。
保持一个 Gateway 进程，不加多个 `--workers` 共写插件目录。

## 测试和打包

设置上述本地数据目录后执行：

```bash
python -m pip check
python -m sidecar.worker.dependencies
node --check sidecar/worker/vendor/js_runtime.mjs
python -m compileall -q sidecar
python -m pytest -o addopts='' -q
python -m pip wheel --no-deps --wheel-dir dist .
```

wheel 包含 Web 页面、Lucide 图标和许可证、`Atvp.py`、JS 运行时；
版本来自 `sidecar/__init__.py`。wheel 不包含 Node.js，目标机器仍需自行安装。
测试环境可用 `pip install .[test]`，生产只安装 `pip install .`。

依赖使用版本范围，不是逐字节可重现锁文件；需要精确部署时固定已发布镜像 digest
或提交标签，并保存原镜像归档与校验值，而不是重复解析浮动基础镜像和依赖。

## 镜像发布流程

`main` 更新触发 `.github/workflows/container.yml`，也可在 Actions 手动触发：

1. Ubuntu/Python 3.12 运行全量离线测试、依赖检查和 Node.js 检查。
2. Buildx 构建 GHCR 镜像，附提交 revision 标签并推送版本、`latest` 和 `sha-` 标签。
3. 从相同 digest 导出镜像，检查镜像内依赖、版本、图标、健康与空订阅。
4. 生成 `t4-sidecar.tar.gz`、`SHA256SUMS` 和 `image-id.json`，上传短期 Actions 归档。
5. 维护者完成 NAS 验证后，将同一归档附到版本 Release，公开下载不需要 GHCR 令牌。

源码仓库公开不会自动把既有 GHCR 包设为公开。若 GHCR 仍受限，
使用版本 Release 的镜像归档或本地构建，不为部署要求用户新增网关认证。
运行测试通过不等于全插件、原客户端加速性能或电视长播已验收。
