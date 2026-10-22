# T4 Sidecar 实现节点台账

> 这是本项目的执行清单。每个节点完成后必须补齐状态、验证命令和验收证据，再进入下一个节点。

## 状态定义

- `未开始`：尚未实现。
- `进行中`：代码或验证正在进行。
- `通过`：实现和节点验收均完成。
- `阻塞`：存在明确外部阻断，记录原因和解除条件。

## 节点总览

| 节点 | 目标 | 当前状态 | 验收边界 |
|---|---|---|---|
| M0 | 方案、边界、样本和证据台账冻结 | 通过 | 计划与节点台账一致，核心约束可追溯 |
| M1 | Sidecar 可运行骨架和健康检查 | 通过 | `/t4/health` 返回稳定 JSON，单元测试通过 |
| M2 | 配置、Gateway token 和 source registry | 通过 | 无 token 拒绝，source 列表脱敏 |
| M3 | AT 订阅读取、ext 解码和能力快照 | 通过 | 脱敏 fixture 可解析，敏感字段不出日志 |
| M4 | 独立 Atvp Worker 和会话隔离 | 通过 | worker 可独立启动、超时可回收、source 会话亲和 |
| M5 | 官方 secspider、自定义 raw、动态 loader 和 keyring 回退 | 通过 | 三类插件通过 ATVP/FongMi 兼容门禁 |
| M6 | T4 home/category/search/detail/play 适配层 | 通过 | GET/POST 等价，播放 ID 不泄露凭据 |
| M7 | `/t4/` 独立 Web 入口 | 通过 | source、能力、冒烟测试和配置地址可用 |
| M8 | 容器、反向代理和授权真实回归 | 进行中 | AT 零修改，分类、详情、播放闭环 |

## M0 方案和证据台账

**输入**

- `power721/alist-tvbox` 源码分析和本地 AT 源码证据。
- 已脱敏的订阅、raw Python、官方 secspider 和自定义 secspider 样本。
- ATVP/FongMi 双运行时兼容规则。

**代码和文档范围**

- `PROJECT_PLAN.md`
- `IMPLEMENTATION_NODES.md`
- 后续 `fixtures/` 中的脱敏输入，只允许提交不含 token、secret、Cookie、私有播放 URL 的样本。

**设计决策**

1. 独立 Sidecar，不修改 AT 核心、JAR、数据库和原始插件。
2. Gateway 与 Worker 分进程，Worker 不在 FastAPI 主进程执行插件。
3. 自定义 secspider 优先使用 AT 动态 `Atvp/{token}/{version}.py` loader，只有不可用时才使用只读 keyring 回退。
4. 第一阶段只读实现 `home/category/search/detail/play`，不做 action、History 和 WebHome 通用转换。
5. 公开播放 ID 使用 opaque 包装，不包含 AT token、secret、ext 或原始插件地址。

**验证**

```powershell
git diff --check
rg -n -F -e "动态 loader" -e "keyring 只读回退" PROJECT_PLAN.md IMPLEMENTATION_NODES.md
```

**验收证据**

- 方案文档已明确 Sidecar、Worker、T4 Gateway 三层职责。
- 计划中的 secspider 路径与实现节点一致。
- `git diff --check` 通过。

**失败记录**

- 暂无。

## M1 Sidecar 骨架和健康检查

**输入**

- Python 3.14。
- `fastapi`、`uvicorn`、`pytest`。

**代码范围**

- `pyproject.toml`
- `sidecar/gateway/main.py`
- `sidecar/gateway/config.py`
- `sidecar/gateway/routes/health.py`
- `tests/unit/test_health.py`

**实现**

- 创建 FastAPI 应用。
- 提供 `GET /t4/health`。
- health 响应只包含版本、服务状态和 worker 状态，不包含凭据。
- 配置从环境变量读取，默认只绑定本机开发地址。

**验证**

```powershell
python -m pytest -q
python -m uvicorn sidecar.gateway.main:app --host 127.0.0.1 --port 8765
```

**验收证据**

- 单元测试通过。
- HTTP 健康响应为 JSON，状态码为 `200`。
- 启停不生成受版本控制的运行时文件。
- `python -m pytest -q`：4 passed。
- `GET http://127.0.0.1:8765/t4/health`：`200`，返回 `service/status/version/worker`。
- `python -m compileall -q sidecar tests`：通过。

**失败记录**

- 首次测试发现 `slots=True` 数据类通过 `cls.field` 读取默认值会产生 `member_descriptor`；已改为显式环境变量默认值并复验通过。

## M2 配置、token 和 source registry

**输入**

- M1 应用和脱敏 source 配置。

**代码范围**

- `sidecar/gateway/config.py`
- `sidecar/gateway/auth.py`
- `sidecar/gateway/routes/sources.py`
- `tests/unit/test_auth.py`
- `tests/unit/test_sources.py`

**实现**

- Gateway token 校验。
- source 配置加载和稳定 ID。
- source 列表只输出显示名、能力和状态。
- token 日志和错误信息脱敏。

**验证**

```powershell
python -m pytest -q tests/unit/test_auth.py tests/unit/test_sources.py
```

**验收证据**

- 无 token 或错误 token 得到明确 `401`。
- 正确 token 得到 source 列表。
- 返回 JSON 不出现 AT token、secret、Cookie、keyring。
- `python -m pytest -q`：9 passed。
- `python -m compileall -q sidecar tests`：通过。
- 环境变量 probe：`GET /t4/api/sources` 返回 `200` 和空 source 列表。

**失败记录**

- 暂无。

## M3 AT 订阅和 ext 能力解析

**输入**

- 脱敏 `/sub` 响应 fixture。
- AT origin、订阅 token 和插件描述。

**代码范围**

- `sidecar/at/`
- `sidecar/gateway/routes/sources.py`
- `tests/fixtures/`
- `tests/unit/test_subscription_parser.py`

**实现**

- 请求 AT `/sub/{token}/{id}`。
- 解析 `sites`、`api`、`source`、`loader`、`raw`、`token`、`secret`、`local_proxy_config` 和过滤器。
- Base64/JSON ext 解码。
- loader URL 必须属于配置的 AT origin。
- 建立不含敏感值的 capability snapshot。

**验证**

```powershell
python -m pytest -q tests/unit/test_subscription_parser.py
```

**验收证据**

- raw、官方 secspider、自定义 secspider、WebHome 和缺依赖状态可区分。
- loader、secret、keyring 不能进入日志和 Web 响应。
- `python -m pytest -q`：16 passed。
- Mock HTTP 验证了 `/sub/{token}/{id}` 请求路径。

**失败记录**

- 暂无。

## M4 Atvp Worker

**输入**

- AT `Atvp.py` 和兼容 fixture。

**代码范围**

- `sidecar/worker/`
- `sidecar/worker_protocol.py`
- `tests/worker/`

**实现**

- Worker 子进程 JSON 请求/响应协议。
- 复用 ATVP 方法合同，不在 Gateway 进程 `exec` 插件。
- source 级会话亲和。
- 初始化、分类、详情、播放方法。
- 超时、任务上限、异常归一化和进程重启。

**验证**

```powershell
python -m pytest -q tests/worker
```

**验收证据**

- Worker 崩溃不拖垮 Gateway。
- 同一 source 的详情和播放使用同一运行时会话。
- 超时后 Worker 可回收并重新初始化。
- `python -m pytest -q tests\worker`：4 passed。
- `python -m pytest -q`：20 passed。

**失败记录**

- 首轮 Windows 子进程启动时限设置过紧，已改为仅对延时任务显式使用短超时。

## M5 插件运行时

**输入**

- 官方 secspider、授权自定义 raw、授权自定义 secspider。
- AT 动态 loader 和只读 keyring 回退 fixture。

**代码范围**

- `sidecar/worker/atvp_runtime.py`
- `sidecar/worker/plugin_loader.py`
- `tests/worker/test_plugin_runtime.py`
- `tests/fixtures/plugins/`

**实现**

- 官方 secspider/1 解包。
- 自定义 raw Python。
- 优先执行动态 Atvp loader。
- 动态 loader 不可用时使用只读 keyring，禁止 Web 暴露。
- 固定依赖清单，不在线安装依赖。

**验证**

```powershell
python -m pytest -q tests/worker/test_plugin_runtime.py
python "$env:USERPROFILE\.agents\skills\alist-tvbox-compatibility-check\tools\atvp_compat_gate.py" <fixture> --runtime upstream-1.25-raw
python "$env:USERPROFILE\.agents\skills\alist-tvbox-compatibility-check\tools\verify_dual_runtime.py" <fixture>
```

**验收证据**

- 三类插件都能完成 `init`。
- raw 插件通过 ATVP/FongMi 双运行时门禁。
- 解密明文只在 Worker 内存或临时受控目录存在。
- `python -m pytest -q tests\worker`：7 passed。
- `python -m pytest -q`：27 passed。
- direct-play ATVP gate：`ok=true`。
- custom secspider 动态 loader、HTTP loader、只读 keyring 回退均通过。

**失败记录**

- 暂无。

## M6 T4 适配层

**输入**

- M4/M5 标准化 Spider 结果。

**代码范围**

- `sidecar/t4/`
- `sidecar/gateway/routes/t4.py`
- `tests/t4/`

**实现**

- `/t4/api/{module}`。
- `home`、`category`、`search`、`detail`、`play`。
- GET/POST 参数归一化。
- T4 `class`、`list`、`detail`、`play` 响应归一化。
- opaque 播放 ID 和 source 会话绑定。

**验证**

```powershell
python -m pytest -q tests/t4
```

**验收证据**

- 分类、搜索、详情、播放可以串联。
- 客户端看不到 AT 凭据。
- `vod_play_from`、`vod_play_url`、Header、字幕和代理信息不丢失。
- `python -m pytest -q tests\t4`：2 passed。
- `python -m pytest -q`：27 passed。
- GET、POST 和 query token 调用均通过；detail/play opaque ID 在 Worker 前解包。

**失败记录**

- 暂无。

## M7 Web 入口

**输入**

- M2 source registry 和 M6 T4 API。

**代码范围**

- `sidecar/web/`
- `sidecar/gateway/routes/web.py`
- `tests/web/`

**实现**

- `/t4/` 页面。
- source、能力、错误、冒烟测试、T4 配置地址。
- 不修改 AT 前端构建产物。

**验证**

```powershell
python -m pytest -q tests/web
```

**验收证据**

- 通过反向代理挂到 AT 同源 `/t4/`。
- 页面不显示 secret、Cookie、keyring 和完整订阅 token。
- `python -m pytest -q tests\web`：1 passed。
- `python -m pytest -q`：28 passed。

**失败记录**

- 暂无。

## M8 部署和真实回归

**输入**

- M1-M7 通过。
- 明确授权的 AT 实例和插件样本。

**代码范围**

- `Dockerfile`
- `docker-compose.yml`
- `reverse-proxy/nginx.conf`
- `tests/integration/`

**实现**

- Sidecar 容器。
- Worker 进程边界。
- 反向代理 `/t4/`。
- healthcheck、日志脱敏和只读 keyring 卷。

**验证**

```powershell
docker compose config
docker compose up --build -d
python -m pytest -q tests/integration
```

**验收证据**

- AT 核心文件、数据库和插件未修改。
- 官方加密、自定义 raw、自定义 secspider 均完成授权回归。
- 分类、详情、播放和 GET/POST 通过。

**失败记录**

- 暂无。
