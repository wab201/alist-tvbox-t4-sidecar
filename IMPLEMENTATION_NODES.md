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
| M2 | 配置和 source registry | 通过 | source 列表脱敏；额外鉴权已由 M12 删除 |
| M3 | AT 订阅读取、ext 解码和能力快照 | 通过 | 脱敏 fixture 可解析，敏感字段不出日志 |
| M4 | 独立 Atvp Worker 和会话隔离 | 通过 | worker 可独立启动、超时可回收、source 会话亲和 |
| M5 | 官方 secspider、自定义 raw、动态 loader 和 keyring 回退 | 通过 | 三类插件通过 ATVP/FongMi 兼容门禁 |
| M6 | T4 home/category/search/detail/play 适配层 | 通过 | GET/POST 等价，播放 ID 不泄露凭据 |
| M7 | `/t4/` 独立 Web 入口 | 通过 | source、能力、冒烟测试和配置地址可用 |
| M8 | 容器、反向代理和授权真实回归 | 进行中 | AT 零修改，分类、详情、播放闭环 |
| M9 | AT 内置源原生桥接 | 通过 | 已完成服务端内置源的原生路由、参数和播放桥接；客户端本地/页面型源单独标记边界 |
| M10 | 本地插件完整管理与可用性仿真 | 通过 | Python/JS 上传、删除、排序、重命名、刷新；浏览器操作及持久化与方法链回归 |
| M11 | 统一影视订阅及 FM type=4 请求合同 | 进行中 | 服务端、Web 和真实 AT 抽样已通过；Android/TV 实机导入与播放待验收 |
| M12 | 删除 Sidecar 额外鉴权 | 通过 | 删除令牌配置、校验、签名路由和 Web 输入；131 项回归通过，AT 上游授权保留 |
| M13 | 片单导航与 AT 推送兼容 | 进行中 | 实现、148 项回归、真实 AT 对照及 Web 验证通过；Android/TV 真机推送待设备验收 |
| M14 | 多链路日志与实际播放诊断 | 进行中 | 普通视频/DASH、片单服务端搜片已实现；真实媒体及客户端验收分层记录 |

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
4. 第一阶段先完成 `home/category/search/detail/play`；通用管理 action、History 和 WebHome 转换后置，Media 原版 `/play` 伪动作由 M9 原生桥接覆盖。
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

## M2 配置和 source registry

以下鉴权相关实现和验收是早期历史记录；2026-10-26 起由 M12 删除，不再是当前配置要求。

**输入**

- M1 应用和脱敏 source 配置。

**代码范围**

- `sidecar/gateway/config.py`
- `sidecar/gateway/auth.py`
- `sidecar/gateway/routes/sources.py`
- `tests/unit/test_auth.py`
- `tests/unit/test_sources.py`

**实现**

- 早期包含 Gateway token 校验（已删除）。
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

- 2026-10-27：公网 HTTPS 订阅返回 `200`，但生成的 172 个 type=4 插件地址均为 HTTP。
  当前容器未设置 `FORWARDED_ALLOW_IPS`，来自反代的 HTTPS 协议头未生效。
  “我的追剧”使用原生成地址连接失败，使用相同域名、端口、路径的 HTTPS 地址返回 `200`。

**HTTPS 反代修复和验证（2026-10-27）**

- Dockerfile 和 Compose 默认设置 `FORWARDED_ALLOW_IPS=*`，复用 Uvicorn 的代理头处理；
  无需另配公网域名。Nginx 示例使用 `$http_host` 保留外部端口。
- NAS 备份 Compose 后，仅增加这一项环境变量并重新创建 Sidecar。镜像、挂载和端口未变，
  两个 AT 容器的 ID、启动时间未变，Sidecar 为 `healthy`。
- 公网订阅中 172 个 type=4 API 均保持 HTTPS 和外部域名、端口；
  “我的追剧”首页 GET、JSON POST、表单 POST 均 `200`，13 个分类、9 条分类内容，
  详情复测 `200` 且返回 1 条，与 AT 原生接口一致。Bilibili 首页 `200`、44 个分类。
- 局域网 HTTP 订阅仍返回 `200`，站点 API 保持 HTTP；公开文档仅使用示例地址。
- `python -X utf8 -m pytest -o addopts='' -q`：152 passed，1 条已有依赖警告；
  新增 4 项代理头回归，覆盖 HTTP 直连、HTTPS、非标准端口和 IPv6 Host。
- 首次公网详情曾在约 10 秒后返回 `502`，后续同条目详情及 AT 原生详情均通过；
  首次超时的具体来源未确定，不宣称已完成全部慢请求或手机端长播验收。
- 客户端需刷新点播订阅，更新缓存中的旧 HTTP 地址。本轮只应用 NAS 部署配置修复，
  未重新构建镜像、提交或推送工作区的其他修改。

## M9 AT 内置源原生桥接

**输入**

- 真实授权 AT 订阅中的 builtin descriptor。
- 原版 AT `BiliBiliController`、`PlayController`、`TvBoxController` 和内置 Media 路由证据。

**代码范围**

- `sidecar/at/subscription.py`
- `sidecar/at/native.py`
- `sidecar/t4/adapter.py`
- `sidecar/gateway/main.py`
- `sidecar/gateway/routes/t4.py`
- `tests/at/`
- `tests/t4/`

**实现**

- 保留 builtin descriptor，不再从 `SubscriptionSnapshot` 丢弃。
- 根据 `api` 映射 AT 原生 `/vod`、`/bilibili`、`/media`、`/pian-dan`、`/live`、`/emby`、`/feiniu`、`/pansou` 等路由。
- Bilibili 详情/播放沿用 `ids`、`bvid`，保留 `header`、字幕、弹幕、章节和清晰度结果。
- Media 详情使用原生 `id=msub:*`，分集和伪动作 ID 通过 `/play` 处理。
- Telegram 频道使用 `/tgsc`，Telegram 网页使用 `/tg-search` 并固定透传 `web=true`；Telegram 详情保留 `id/ac/title`。
- 直播播放使用 `/live-play`；Bilibili、Media 和原生播放器请求继续透传 `X-CLIENT`。
- 详情 ID 和播放 ID 仍由 Sidecar opaque 化，token、secret、uid、播放原始 ID 不出 T4 响应。

**验证**

```powershell
python -m pytest -q
python -m compileall -q sidecar tests
git diff --check
```

真实订阅只读回归：

- 178 个站点、18 个 builtin、43 个 raw Python、116 个 secspider，解析错误 0。
- Bilibili：首页、分类、搜索、详情、播放通过；播放字段保留 `header/subs/danmaku/chapters`。
- Media：首页、分类、搜索、详情、分集和追更动作通过；播放字段保留 `playUrl/type`。
- TgWeb：首页、分类、搜索、详情、播放通过；TgChannel 首页、分类、搜索通过。
- TgDouBan、Live 首页/分类/搜索完成只读回归；Feiniu 搜索受 AT 上游 400 配置状态影响，未将上游错误误判为桥接成功。
- `csp_Push` 的当前策略见 M13：原生客户端透传，不伪造标准 T4；`csp_WebHome`/`csp_Nostr` 仍标记为 `page_entry_only`。

**边界**

- AT 管理 API、Bilibili 登录二维码/Cookie 管理、播放同步管理面和 WebHome 注入不是订阅 T4 模块，由原版 AT 继续负责。
- `csp_Push` 的字幕发现、YouTube、DASH、网盘离线下载等 Android 本地逻辑不属于 AT 服务端路由，Sidecar 不宣称已移植这些客户端实现。

## M10 本地插件管理与可用性仿真

**范围**

- 管理 Sidecar 自己上传的 Python/JS 文件；AT 原始插件保持只读，不改 AT 数据库和文件。
- 列表、名称、顺序和注册关系持久化；名称与排序变更不改变插件 ID 或 FM 地址。
- 明确区分列表重载、运行实例刷新、AT 订阅强制重读。

**实现**

- `GET /t4/api/plugins`、`PATCH /t4/api/plugins/{id}`、
  `POST /t4/api/plugins/reorder`、`POST /t4/api/plugins/{id}/refresh` 和删除接口。
- 排序需包含全部插件且不重复；过期列表、空白名称、控制字符和重名均返回明确错误。
- 同内容上传去重并保留名称、顺序；不同内容但同名自动编号。
- 删除和刷新回收 Worker 及初始化状态；与同插件请求互斥，不阻塞其他插件初始化。
- Python 刷新直接编译当前源码，避免同尺寸、同时间戳编辑命中旧字节码。
- Worker 正常退出关闭 JS 子进程；Python/JS 常规日志转向 stderr，不污染 JSON 行协议。
- 原生页面逐插件操作、删除确认、即时状态、FM 地址复制和手机布局；图标本地打包，无 CDN 依赖。
- AT 订阅 `?refresh=true` 绕过缓存，旧请求不能覆盖新快照；描述发生变化时重新初始化相应 Worker。

**验证（2026-10-26）**

```powershell
python -m pytest -o addopts='' -q
python -m compileall -q sidecar tests
node --check sidecar/worker/vendor/js_runtime.mjs
git diff --check
python -m pip wheel . --no-deps --wheel-dir tests/reports/wheels
```

- 全量自动化：83 passed；1 条现有 TestClient/httpx 依赖弃用警告。
- Python 和 JavaScript：管理后六模块的 GET/POST 合同、删除重传、刷新新实例、重启恢复通过。
- 本地服务真实重启后两类上传插件恢复，六模块 GET/POST 共 24 次 HTTP 请求全部返回 200；测试结束清理夹具注册。
- 隔离 Chromium：上传 PY/JS、排序、中文重命名、刷新、删除及首页冒烟已实际点击验证。
- 390px 手机视口无横向溢出，图标无加载失败；桌面/手机截图在忽略目录 `tests/reports/simulation/`。
- 本地服务读取环境变量中的真实 AT 订阅：177 个插件、解析错误 0；Media、Bilibili 首页和有效分类返回 200，
  分类分别有 9 和 29 条记录。本次只读验证不代表所有插件逐项验收。
- Wheel 构建成功，已核对管理 HTML、图标、许可证、Atvp 和 JS Worker 资源进入安装包。

**验收边界**

- 这是浏览器与接口仿真及真实 AT 只读抽样，不是 Android FM 真实播放验收。
- 夹具使用 `.invalid` 媒体地址，只验证协议和运行状态，不把返回 URL 当作可播放证据。
- 本节点开发验收时尚未发布新镜像、推送仓库或替换 NAS 容器；发布部署单独核验。

## M11 统一影视订阅与客户端合同

从消费端逆序推进，不把模块 API 冒烟等同于影视客户端兼容。

| 子节点 | 状态 | 验收内容 |
| --- | --- | --- |
| M11.1 客户端合同核对 | 通过 | 官方 `SiteApi` 的同地址参数分发与 URL-safe Base64 `ext`，分类 `ac=detail&t=...` |
| M11.2 统一出口与参数分发 | 通过 | 首轮 45 项测试通过；包含 PY/JS 三类 HTTP 传输的详情/播放链 |
| M11.3 Web 与指南 | 通过 | 顶部复制完整订阅；单插件 API 与完整订阅明确区分 |
| M11.4 回归与真实抽样 | 通过 | 126 项自动化、复制及手机布局、真实 AT Bilibili 解析链通过 |
| M11.5 Android/TV 实际播放 | 未开始 | 必须另有真实客户端导入、选源、播放与日志证据 |

**当前实现**

- `GET /t4/config.json` 返回 `sites`；汇总 AT 与本地插件，页面型源仍跳过，AT 推送按 M13 原生透传。
- 服务端站点 `type=4`，`api` 为可直接接收标准参数的绑定插件地址；站点 key 与名称和排序无关。
- type=4 站点按白名单保留搜索、快速搜索、筛选、分类等选项，不下发原始 ext 或 AT 凭证；
  原生推送为客户端运行的例外，需保留其原始 `jar/ext`。
- 支持 GET、JSON POST、表单 POST；将客户端筛选 `ext` 解码后交给现有适配层。
- 本节点早期支持可选保护模式及派生访问钥匙；M12 已统一删除，当前使用固定直连地址。
- 复用订阅缓存和本地管理快照，生成配置时不初始化插件；AT source 失败明确返回 `502`。

**合同来源**

- 2026-10-26 读取 `FongMi/TV` 的 `fongmi` 分支
  `app/src/main/java/com/fongmi/android/tv/api/SiteApi.java`。
- `homeContent` 使用 `filter=true`；分类使用 `ac=detail&t&pg&ext`；
  详情使用 `ac=detail&ids`，搜索使用 `wd&quick&pg`，播放使用 `play&flag`。
- 该源码核对不是客户端实机验收；`action` 在该客户端分支直接请求动作 URL，
  本轮的 `ac=action` HTTP 合同不能替代原生动作、History 或 WebHome 的实机验证。
- 读取的源码 UTF-8 SHA256：
  `6D80875561E47DE766D10D5E5CC7720B4574BA4F31CC16BCAA57BCE779B38D10`。
  GitHub 匿名 API 限流，未将该分支快照冒充固定提交身份。

**验证（2026-10-26）**

```powershell
python -m pytest -o addopts='' -q
python -m compileall -q sidecar tests
git diff --check
```

- 全量自动化：`126 passed, 1 warning in 28.65s`；警告为现有 TestClient/httpx 弃用提示。
- 管理台内联脚本通过 HTMLParser 提取并交给 `node --check -`，避免仅依赖页面字符串断言。
- 统一订阅覆盖跨 source 重名隔离、稳定 key、白名单选项、敏感字段隔离、当时的可选保护模式、
  本地插件增删改序及强制刷新 AT 后配置同步；生成配置不执行插件初始化。
- 上传 PY/JS 后直接读取 `sites[].api`，按 GET、JSON POST、表单 POST 三种方式完成
  首页、分类、搜索、详情、播放、action 合同；真实媒体播放不以 `.invalid` 夹具替代。
- 真实环境订阅：原能力快照 177 个条目，统一订阅输出 172 个服务端站点；
  Bilibili 首页 44 分类、推荐分类 29 条；我的追剧首页 13 分类、分类列表 9 条，均为 200。
- Bilibili 经统一订阅地址的分类、详情、播放解析返回 200，保留 `header/subs/danmaku`。
  本次未执行媒体长播，也未触发真实追剧增删或检查更新动作。
- 首次播放抽样失败定位为空播放 ID 被错误 opaque 化；已修复为保留空项，
  并加入 `test_adapter_does_not_invent_play_ids_for_empty_episode_entries` 回归。
- 当前样本整包生成约 236ms，缓存命中约 9ms；这是本地单次观测，不是吞吐基准。
- 浏览器实际点击复制，并在 Windows 系统剪贴板核对订阅字符串一致。
  内置浏览器自身剪贴板读接口未反映系统值，不以该接口误报复制失败。
- 390px 实测 `innerWidth=390`、`scrollWidth=375`，无横向溢出；桌面/手机截图：
  `tests/reports/simulation/unified-subscription.jpg`、`unified-subscription-mobile.jpg`。
- 本轮新服务为 `http://127.0.0.1:8767/t4/`；原 `8765` 旧进程未替换。
  此为开发验收记录，当时未提交、推送、构建镜像或部署 NAS。电视使用需在部署更新后通过局域网地址导入。

**未验收边界**

- Android/TV 真正导入、遥控选源、媒体长播、切集、客户端动作、跨重启收藏/历史恢复仍需实机证据。
- 现有详情/播放 opaque 映射为进程内状态；站点 key 稳定不代表播放 ID 跨服务重启稳定。
- 原插件的局部代理、页面入口与客户端专有能力不因统一导出就自动获得完整 T4 语义。

## M12 删除 Sidecar 额外鉴权

**实现（2026-10-26）**

- 彻底删除 Gateway Token 配置、校验模块、认证请求头参数及签名订阅/插件路由，不保留可选开关。
- 统一订阅固定为 `/t4/config.json`，插件地址固定为 `/t4/fm/{source}/{plugin}/`。
- Web 无 Token 输入、认证状态分支和会话令牌存储，打开后直接加载订阅与插件。
- 旧环境变量即使仍有值也不能启用额外鉴权；旧带签名路径的地址返回 `404`，需重新复制固定订阅。
- 保留 AT 完整订阅及插件配置中的上游授权、加载器校验、输入校验和业务错误处理。
- NAS 升级仅替换 Sidecar，保留 AT 容器、订阅配置和上传插件数据。

**验证**

```powershell
python -m pytest -o addopts='' -q
python -m compileall -q sidecar tests
git diff --check
```

- 全量回归 `131 passed`，仅一条已有 TestClient/httpx 弃用警告。
- 覆盖无额外凭证的统一订阅、source 快照、FM 和模块调用、插件上传/删除/改名/排序/刷新。
- 带旧环境变量的请求仍成功；AT 订阅令牌正确传至上游，AT 返回 `401` 时不绕过授权或伪造成功。
- Web 内联脚本通过 `node --check -`，页面不再包含 Token 输入、认证请求头及 `sessionStorage`。
- 发布与 NAS 真实验收结果单独记录在本地脱敏报告 `tests/reports/release/`，不把凭证提交到仓库。
- 本节点不改变 M11 的 Android/TV 真机、长播和跨重启历史验收边界。

## M13 片单导航与 AT 推送兼容

**实现（2026-10-26）**

- 片单导航继续桥接 AT `/pian-dan/{token}`，首页合并分类筛选与 `t=0` 推荐列表。
- 统一订阅固定 `indexs=1/searchable=0/quickSearch=0/filterable=1`，与原版 AT 的导航能力一致；
  分类筛选字段按原始名称传递，不错误改成 `filter.*`，不允许筛选覆盖分类和页码。
- 保留卡片标题、海报和其他字段，FongMi 点击标题由影视搜片；
  M14 为忽略 `indexs` 的客户端补齐服务端标题检索及委托播放，action 仍不伪造。
  搜索透传 AT 原有接口，是否支持以当前 AT 版本为准。
- AT 推送保留原版 `push_agent/type=3/csp_Push`、`jar/ext` 和站点选项；
  站点无 JAR 时自动采用原订阅全局 `spider`。
- 多个 source 按配置顺序只导出首个可用推送站点；两种 JAR 均不存在时不导出。
- AT 推送标记 `native_client_passthrough`；Web 可选、可复制完整订阅，禁用首页冒烟。
- 管理页面和能力快照不包含原始授权字段；完整订阅中的原生推送必须携带原始 AT ext，
  只供可信客户端使用。此例外不改变 type=4 站点的授权字段隔离。
- 不修改 AT、JAR、数据库、原始插件或设备管理，不新增 Gateway Token。

**验证进度**

| 子节点 | 状态 | 证据 |
| --- | --- | --- |
| M13.1 原版合同 | 通过 | AT `PianDanController/SubscriptionService`；FongMi `Site/SiteApi/JarLoader`、手机和电视 `TypeFragment` 按标题搜片 |
| M13.2 协议回归 | 通过 | GET/JSON POST/表单 POST；推送固定 key 去重、原始字段、全局 JAR、刷新和缺失 JAR 场景 |
| M13.3 真实 AT 对照 | 通过 | 统一出口三种传输与原始响应一致，12 分类均返回 200，原生推送字段及 JAR 已核对 |
| M13.4 Web 交互与中文指南 | 通过 | 实际选择原生推送并复制地址；片单首页冒烟复验通过；四份中文文档同步 |
| M13.5 Android/TV 设备验收 | 未开始 | 当前 ADB 无设备，MuMu `127.0.0.1:7555` 拒绝连接；不将接口结果替代真机推送 |

**验证结果**

```powershell
python -m pytest -o addopts='' -q
python -m compileall -q sidecar tests
git diff --check
```

- 全量 `148 passed, 1 warning in 27.12s`，包含 Web 内联脚本的 `node --check -`；
  警告为已有 TestClient/httpx 弃用提示。编译和 diff 检查通过，Git 仅提示本地换行转换。
- 真实订阅 178 个条目，解析 177 个插件、0 错误，统一订阅输出 173 个站点（含一个原生推送）。
- 片单首页 12 分类、12 组筛选、30 条推荐；GET、JSON POST、表单 POST 的首页、分类筛选和搜索均与当次 AT 响应一致。
- 12 个分类逐项请求均为 200；其中 `tmdb:movie`、`tmdb:discover_tv` 当次上游返回空列表，
  部分搜索和筛选请求也有空结果。不将上游空列表记为已获得可播放资源。
- 原生推送输出与原订阅站点字段一致；JAR 下载 200、450594 字节，DEX 包含 `Push` 类，
  SHA256 为 `18200bdf78bd86caeec2a461e672af4819a4dc0087a4c16976042c561c6a51de`。
  这只证明配置和资源可达，不证明 Android JAR 初始化或实际推送播放成功。
- Web 选择 AT 推送后显示“原生推送”、禁用首页冒烟、复制完整订阅；
  已与系统剪贴板核对一致。切回片单可复制其 API，并实际获得首页结果。
- 页面首次片单冒烟出现一次 `NativeAtError`，当时未保留上游具体原因。
  同进程模块入口、FM 入口、原生默认调用及页面复验均通过，未证实参数缺失；
  已增加两种入口默认 `X-CLIENT` 一致的回归，不新增无依据的重试或回退。
- 脱敏本地报告和页面截图位于忽略目录 `tests/reports/builtin-compat/`。

本节点尚未提交 Git、构建镜像或更新 NAS；发布部署需单独核验。

## M14 多链路日志与实际播放诊断

**实现（2026-10-27）**

- 修正未声明客户端身份的播放请求：不再默认向 AT 报告为支持 DASH 的 FongMi；
  实际客户端明确声明身份时保留原值，不修改 AT 配置。
- AT 返回内嵌 DASH 时提供 HTTP MPD 和独立音视频 Range 转发，保留字幕等附加信息，
  不转码、不丢弃音频；不把原生 HLS-only 播放器视为已支持 DASH。
- 保留 FongMi 的 `indexs=1` 标题导航；其他客户端进入片单详情时执行服务端搜片，
  按当前订阅顺序选前 6 个可搜索源、最多 3 路并发，仅合并同名且年份不冲突的结果。
  播放请求委托实际命中源；未命中明确报错，不返回虚假媒体。
- 请求自动生成链路 ID，经异步任务、AT HTTP 和 Worker 传递；
  opaque ID 记录前序链路，媒体映射连接解析与 MPD/Range 请求。
- 日志记录阶段、状态、耗时、字节和错误类型，不保存 URL、查询、正文或授权字段。
  Worker stderr 持续排空，仅保留错误类型、级别、长度和指纹，并限制记录速率。
- Web 增加片名检查、实时日志、关联筛选和 JSONL 导出；
  Docker 日志保存到已有 `/data` 卷，约 2 MB × 3 轮转，内存保留 2000 条。
- 实播校验明确分离清单可达、媒体首段、音视频识别、实际解码和真机验收；
  HLS/外部 MPD 只有清单证据时不显示媒体通过。

**验收分层**

| 子节点 | 状态 | 证据 |
| --- | --- | --- |
| M14.1 协议与脱敏回归 | 通过 | 三种传输的片单检索和委托；DASH 双轨、Range、头保留、日志恢复、关联和脱敏 |
| M14.2 真实 AT 媒体 | 通过 | 截图关键词样本：普通 MP4 及 HTTP DASH 均首段 206，FFprobe 识别音视频，FFmpeg 5 秒/125 帧解码 |
| M14.3 真实片单点击 | 通过 | 首页卡片 → 同名检索 → 实际源详情 → 播放委托 → 首段媒体 206；命中组数随当次源状态变化 |
| M14.4 Web 交互 | 通过 | 实际点击 Bilibili 片名检查，媒体首段 206、链路 ID 与日志加载；桌面/手机布局单独截图 |
| M14.5 手机/电视实播 | 待验证 | 无客户端解码和长播证据，不能由服务器 FFmpeg 代替 |
| M14.6 发布与 NAS 部署 | 未执行 | 本节点不自动提交所有既有改动，不将本地测试当作 NAS 已升级 |

本地真实报告：`tests/reports/release/playback-verification-20261027.json`，
仅保存结构化脱敏事件和结果，不保存播放地址或凭证。验证工具为
`scripts/verify_live_playback.py`；仅在开发机使用 FFmpeg，不增加 Sidecar 镜像的转码依赖。

**收尾验证**

```powershell
python -m pytest -o addopts='' -q
python -m compileall -q sidecar tests scripts/verify_live_playback.py
python -m scripts.verify_live_playback --report tests/reports/release/playback-verification-20261027.json
git diff --check
```

- 全量 `168 passed, 1 warning in 30.40s`；警告为已有 TestClient/httpx 弃用提示。
- 真实报告包含 136 条脱敏事件，33 个运行源码/页面文件 SHA256 与当前工作区一致。
- 桌面 1440×1000、手机 390×844：布局无横向溢出；检查、日志关联筛选、
  开启实时日志后自动取消旧筛选、JSONL 下载和服务重启后日志恢复均已验证。
- 本地新开发入口为 `http://127.0.0.1:8768/t4/`，与原有服务分开；
  不能将这个回环地址导入远程手机，也不能将本地进程当作 NAS 已更新。

## M15 版本基线与全订阅兼容门禁

**实现（2026-10-27，0.2.0 本地候选版）**

- 以 `CHANGELOG.md` 固定 M14 的本地报告 SHA256、Git 基点及未发布边界。
  版本由 `sidecar.__version__` 统一供包、健康检查、Web 和检测报告使用。
- 所有订阅条目归入清单，包括未知运行时、客户端专属源、页面及解析错误。
  重复 ID 失败，不把未执行的条目隐藏或当作通过。
- 检测首页 → 分类 → 搜索 → 详情 → 图片 → 解析 → 媒体；
  目录卡片走真实分类导航，分类请求还原 opaque ID。
- 复用原生桥接、ATVP 和 JS Worker；批量 Worker 独立于播放会话，
  默认并发 2、最大 3；普通插件逐项回收，批次结束回收片单委托产生的子进程及初始化缓存。
- 图片同时要求成功状态、匹配的 MIME 和文件签名；HLS 跟随真实变体并检测分片，
  MP4/WebM 检查首中尾 Range；DASH 只对实际已检测的轨道首段给出证据。
- 明确五种阶段状态及抽样覆盖；操作组不执行，action 不调用。
  格式不支持、只有清单、需要客户端解析、缺样本或缺覆盖均不能冒充媒体通过。
- Web 提供启动、进度、停止、类型/结果筛选、逐项证据与导出；
  容器默认落盘至 `/data/reports/compatibility-latest.json`。
- 报告保存运行源码指纹、脱敏配置指纹、逐插件关联日志，进程启动后源码变更使报告失效。
  回收及落盘完成前不返回最终放行；中断、取消、写入失败都不放行。
- CLI：`python -m scripts.check_compatibility`；门禁未通过退出 2，工具启动失败退出 1。

| 子节点 | 状态 | 证据 |
| --- | --- | --- |
| M15.1 版本和前轮基线 | 通过 | `CHANGELOG.md`；单一版本 `0.2.0`；包版本与健康检查一致 |
| M15.2 全清单和判定模型 | 通过 | 未支持条目/解析错误/重复 ID/空结果/覆盖上限回归 |
| M15.3 七阶段真实调用和资源探测 | 通过 | MP4 三段、HLS 两片、DASH 音视频、图片 MIME/签名；上传 PY/JS 真实 Worker |
| M15.4 任务生命周期和可重跑性 | 通过 | 取消、初始化缓存回收、文件写入失败、进程指纹、收尾时序回归 |
| M15.5 Web 管理和中文说明 | 通过 | 启动/停止、失败筛选、逐行证据；1440×1000 与 390×844 无整页横向溢出 |
| M15.6 真实订阅全条目检测 | 已完成，兼容未放行 | 178/178；13 通过、76 失败、78 受阻、8 不适用/未支持、3 未完成必要覆盖 |
| M15.7 发布与 NAS 升级 | 未执行 | 本轮未获得新的发布请求，不将本地检测结果当作线上升级 |

**验证命令**

```powershell
python -m pytest -o addopts='' -q
python -m compileall -q sidecar scripts tests
python -m pip wheel . --no-deps --wheel-dir tests/reports/dist
git diff --check
```

当前全量回归 `211 passed, 1 warning`；警告仍为已有 TestClient/httpx 弃用提示。
本轮独立开发入口是 `http://127.0.0.1:8769/t4/`，没有替换旧的 8768 服务。
详细抽样边界和 API 见 `COMPATIBILITY_GATE.md`。
最终基线见 `COMPATIBILITY_BASELINE.md`：源码树指纹一致、0 解析遗漏，
报告保存 7,191 条关联事件，检测结束后无残留 Worker；
构建 Wheel 的 38 个运行文件与报告指纹逐项一致。

**下一轮**

1. 固定本轮完整报告，以逐插件七阶段失败矩阵作为 M16 基线。
2. 先处理加载/合同错误，再处理目录、空样本和搜索参数，不盲目重写 AT 或原始插件。
3. 对图片失败按 HTTP、MIME/签名、请求头分别定位。
4. 对播放缺口分别处理解析失败、客户端解析、未支持格式、分片/Range 失败；
   不用临时重试或跳过阶段制造通过率。
5. 接入 FM 手机/电视实际解码、切集和长播证据，单独设置客户端验收门禁。

## M16 失败根因与服务端运行环境补齐

2026-10-27 首批修复已落地；该阶段为 `0.2.0` 本地候选版。
2026-10-28 已核验 `0.2.1` 发布及 NAS 升级，详细记录见本节末尾和 `FAILURE_ANALYSIS.md`。

| 子节点 | 状态 | 任务 |
| --- | --- | --- |
| M16.1 根因复现 | 已完成 | 扫描真实 159 个 Python/secspider 插件；复现 3 个缺公共模块、dataclass 注册异常、错误响应 ID、分类参数、4 个字符串请求头插件及图片 MIME 不一致 |
| M16.2 Worker 错误链 | 已实现并验证 | 保留请求 ID、安全缺模块和根异常，真实 Worker、API 及门禁回归通过 |
| M16.3 依赖交付 | 部分完成 | 依赖清单、14 项构建/NAS 预检、PY/JS ZIP 包管理及公共目录加载已实现；Linux 基础环境已验，3 个实例公共源码仍未交付 |
| M16.4 模块加载 | 已实现并验证 | 主模块/inner/filter 注册与清理；真实 dataclass 初始化及首页通过，原 vendor 未改 |
| M16.5 参数合同 | 首批完成 | JSON 字符串 header 及直接 loads/字典筛选适配；4 插件 19 条线路解析通过，媒体仍有缺口 |
| M16.6 宿主能力 | 首批实现并验证 | Python 同会话 HTTP 代理、siteKey、localProxy、实例缓存、字节 Range/HEAD、流式返回、刷新回收；真实代理封面通过，真实代理 HLS 全链仍待补 |
| M16.7 检测器纠偏 | 部分完成 | 图片解码、真实搜索样本、普通 AES-128 HLS、外置音轨及合法 ID3+TS 已实现；加密字节范围及其他非标准格式仍待完成 |
| M16.8 原生源与网络 | 待实现 | 飞牛、直播、Telegram、片单及真实请求故障分类 |
| M16.9 Linux 镜像验收 | 部分完成 | CI/Linux 回归、镜像构建、14 项 NAS 导入及 53 运行文件绑定通过；全订阅同镜像重跑及远程文件版本绑定仍待完成 |
| M16.10 FM 客户端验收 | 待验证 | 实际播放、切集、快进、长播 |

已修改 Sidecar 运行代码，不改 AT、原始插件或 vendor。旧分析报告保持不变，
修复复测另存 `tests/reports/runtime-repairs*-20261027.json`。
真实 B站、我的追更通过七阶段抽样，后者 4/4 线路取得媒体字节；
4 个 header 插件 19/19 线路解析；最终源码复测 12 条媒体通过、4 条受阻（含3条加密）、
3 条分片失败，外部状态变化不覆盖先前报告。
不得将这些局部结果当作新的全订阅放行或真机验收。

干净 Windows/Python 3.14 环境按 `.[test]` 安装后，`pip check`、14 项导入预检及
全量 `261 passed, 1 warning` 通过；编译、Wheel（50 文件比对）和 diff 检查通过。
Web 的 ZIP、完整管理流程、缺模块根因及桌面/手机布局已实际验证。
新开发入口为 `http://127.0.0.1:8770/t4/`；详细报告与未完成项见 `FAILURE_ANALYSIS.md`。

### 第二轮修复与复测

2026-10-27：仍为 `0.2.0` 本地候选版，不修改 AT、JAR、原插件和 vendor，不新增鉴权。

- Python 宿主用回环 HTTP 传输二进制和流，不挤入 Worker JSON 通道；
  JSON 响应及文本清单中的内部端点自动转换为当前 T4 域名。
- 透传插件响应码、请求头、Range/HEAD 和重定向；缓存仅属于当前 Worker；
  独立门禁回收不影响播放器，刷新/删除使对应旧代理地址失效。
- 内部回环请求禁用环境代理接管；插件日志不污染 RPC，也不记录原始内容。
- 普通 AES-128 校验真实密钥与显式/序号 IV，并验证解密字节；不放宽非标准分片和 DRM 判断。
- 搜索缺本插件卡片时复用同订阅本次运行的真实片名，保留空首页/分类状态。
- 新增真实 Worker、并发、清理、流、Range/HEAD、密钥轮换和错误输入回归。

全量 `292 passed, 1 warning`；依赖预检、编译通过。
干净 Wheel 的 53 个运行文件逐字节一致，未包含历史构建缓存中的已删除模块；
旧构建缓存已保留到忽略的报告目录，未删除用户文件。
11 插件真实 HTTP 定向复测已完成，3 个整链通过；
原 3 条加密 HLS 线路均取得解密后 TS 字节，3 条非标准分片仍失败。
这一轮全订阅门禁单独重跑，不覆盖 M15/M16 首批报告。

当前独立开发入口为 `http://127.0.0.1:8771/t4/`，保留旧开发服务。
完整复测证据及未完成项见 `FAILURE_ANALYSIS.md`。

### 第二轮收尾补修

真实 178/178 条目检查结束，但历史任务最终报 RuntimeError，仍未放行：
21 通过、55 失败、93 受阻、8 不适用/未支持、1 未覆盖。
原始异常被调度器丢失，不能事后断言根因；历史报告及其源码指纹保留。

- 已复现 Windows 读取占用导致报告替换失败；仅对该步骤的 WinError 5/32 有界重试。
- 任务错误新增阶段、根异常、源码位置、系统错误号与 trace 关联；
  真实写入/回收失败继续阻止门禁通过，不靠修改状态完成收尾。
- 303 项回归通过，覆盖真实 Windows 文件占用、持续写失败、多协程失败及 178 条离线夹具。
- 最终源码真实定向 8/8 正常完成：4 通过、3 失败、1 受阻，24 条媒体线路通过、3 条非标准分片失败；
  无任务错误，无残留门禁 Worker 或代理路由。并非全订阅验收。
- 新 Wheel 53 个运行文件与源码、隔离安装一致；原 vendor 不变。
- 独立依赖环境再次 303 项通过；最终 HTTP 定向 7 个插件为 4 通过、3 个同类分片失败，
  与当前源码指纹一致，另存报告，不用局部成功替换历史失败。

最新独立开发入口为 `http://127.0.0.1:8772/t4/`，旧服务及证据不覆盖。
M16.3/M16.6/M16.7 的剩余能力与 M16.8～M16.10 仍待完成，总门禁未放行；未发布或升级 NAS。

### 0.2.1 针对性优化

检测器增加外置 HLS 音轨独立抽样、AAC/ADTS 连续帧识别和合法 ID3+TS 结构校验；
新增非法前缀回归，避免任意去头造成假阳性。版本提升为 `0.2.1`，
发布须核对完整回归、Linux 镜像和 NAS 健康、持久数据及 AT 容器未变证据。

### 0.2.1 发布与 NAS 验收

2026-10-28：仓库运行提交 `352d925`，镜像 `sha-352d925`，
Actions `40534874762` 成功。Linux 回归为 306 通过、1 个 Windows 专属测试跳过，
Windows 为 307 通过；已有弃用警告不影响结果。

- 首次 CI 未准备 `/data` 导致权限错误，已按真实结果修复 CI 运行目录与配置测试隔离；
  测试门禁未绕过，产品路径、AT 和 NAS 其他容器未修改。
- 原 Compose 目录原地替换 Sidecar，保留备份、端口及数据卷，健康状态为 `healthy`。
- 统一订阅 172 个 T4 + 1 个原生推送，178 个 AT 插件快照解析无错误。
- PY/JS 三种请求方式 36 项隔离检查及完整管理、重建应用后的持久化通过；53 运行文件与提交一致。
- NAS 真实 11 插件定向：5 通过、5 失败、1 受阻；30 媒体线路：25 通过、3 非标准分片失败、
  2 需客户端解析。本轮未重跑全部 178 条，未完成 FM 真机或远程插件源码精确版本绑定。

本节点完成“发布与运行环境交付”，不代表 M16 所有兼容缺口已完成，总门禁仍未放行。

## M17 服务端媒体交付与客户端减负

2026-10-28，候选 `0.2.2.dev1`，仅本地开发，不修改线上 `0.2.1`。

| 子节点 | 状态 | 范围及证据 |
| --- | --- | --- |
| M17.1 剩余线路归因 | 已完成 | 在原 NAS 镜像只读复现三条 PNG 包装 TS；前缀 126 字节，PNG 前置块 CRC、188 字节包及 PAT 均有证据 |
| M17.2 统一 HLS 出口 | 已实现并定向验证 | 使用已有 m3u8 库改写主/媒体清单、音轨、密钥、MAP 等 URI，请求头留在服务端；直播刷新和会话预算回归 |
| M17.3 分片标准化 | 已实现并定向验证 | 仅认可结构的 PNG/TS 流式去头，GET/HEAD/Range 一致；加密与 BYTERANGE 保持原字节 |
| M17.4 网页解析收敛 | 部分完成 | HTML video/source、MacCMS 声明式数据解析；不执行 JS，不回传客户端嗅探；真实一条线路曾通过，其他请求存在 Worker 超时 |
| M17.5 工程与媒体验证 | 定向完成，发布项待验 | 347 项回归、最终源码 5 插件 HTTP 抽测 4 通过/1 空详情失败、3 条 FFmpeg 短时解码及依赖预检通过；Wheel/Linux/全订阅待验 |
| M17.6 发布与真机 | 待执行 | Linux 镜像、当前源码全订阅、FM 真机、切集、快进及长播，不能用局部通过替代 |

客户端不需新参数；仍使用统一订阅。媒体解码、原生 AT 推送仍属于客户端职责，
不宣称所有客户端仅靠服务端转发就自动支持全部编码或原生功能。

## M18 内置插件实测与客户端职责

2026-10-28，`0.2.2` 承接 M17。按用户明确要求，不做服务端转码、重封装或解码。

| 子节点 | 状态 | 验证要求 |
| --- | --- | --- |
| M18.1 截图定位 | 已完成 | 我的云盘同名内容、相同分集大小；首两集带头 206、去头 403，实际 MKV |
| M18.2 直链合同 | 已实现 | 普通文件 url/header 原样交付；开发 FFmpeg 不进入生产依赖或请求链 |
| M18.3 内置桥接修复 | 已实现 | AList type=0；直播最终 HTTP 地址不进入空 live-play；GET/JSON/表单回归 |
| M18.4 内置运行矩阵 | 本地抽样完成，仍有缺口 | 18 条目：1 通过、10 失败、2 受阻、5 不适用；截图四集首中尾 Range 通过，不等于所有线路通过 |
| M18.5 构建发布 | 已完成工程验收 | Windows 361 通过，Linux 360 通过/1 跳过；55 文件包核对；503b2ec / Actions 40620249534 / GHCR 已核验 |
| M18.6 NAS 交付 | 已部署并分层复验 | healthy、55 文件与提交一致、AT/配置不变；截图四集 Range 和36项隔离请求通过；18内置仍1通过/11失败/1受阻/5不适用，不是全兼容 |

本轮发布与运行交付完成，未关闭 M17 的全订阅、FM 真机解码、切集、快进与长播节点。
后续修复只处理有证据的网络/解析/插件合同问题，不增加服务端转码、重封装或解码。

## M19 我的云盘客户端参数修复

2026-10-28，`0.2.3`。按已确认的 `/vod` 合同完成最小修复并发布到现有 NAS。

| 子节点 | 状态 | 验证要求 |
| --- | --- | --- |
| M19.1 根因定位 | 已完成 | `ac=detail` 生成代理 URL，AT `/play` 解析站点号失败；原始诊断保留 |
| M19.2 最小修复 | 已实现 | 仅 `/vod` 详情消费 `ac`，保留 `ids/depth`；其他后端 `ac` 不变 |
| M19.3 防漏检 | 已实现 | FM HTTP 三种传输、两类入口、有无 `ac`；门禁及诊断使用客户端详情参数 |
| M19.4 本地验收 | 已完成定向验收 | 392 项回归；真实首两集 24 次取链 200、首中尾 206、去头 403，不解码；18 内置仍有 7 失败/4 受阻 |
| M19.5 发布与 NAS | 已完成定向验收 | bb19317 / Actions 40631058673；Linux 391 通过/1 跳过；NAS healthy、55 文件一致、24 取链及 36 隔离 PY/JS 请求通过；原 AT/卷/端口/配置不变 |
| M19.6 内置源复测 | 已完成，保留未兼容项 | NAS 18 条目 1 通过/7 失败/5 受阻/5 不适用；B站本轮搜索空，云盘图片 418；截图四集首中尾网络通过，总门禁不放行 |

升级后重新打开详情获取当前进程的播放 ID。FM 真机切集、快进和连续播放仍独立待验，
不得将媒体字节可达提升为客户端验收通过。
