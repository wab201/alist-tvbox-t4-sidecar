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
| M2 | 配置、可选 Gateway token 和 source registry | 通过 | 本地模式免额外令牌；保护模式拒绝无 token 请求，source 列表脱敏 |
| M3 | AT 订阅读取、ext 解码和能力快照 | 通过 | 脱敏 fixture 可解析，敏感字段不出日志 |
| M4 | 独立 Atvp Worker 和会话隔离 | 通过 | worker 可独立启动、超时可回收、source 会话亲和 |
| M5 | 官方 secspider、自定义 raw、动态 loader 和 keyring 回退 | 通过 | 三类插件通过 ATVP/FongMi 兼容门禁 |
| M6 | T4 home/category/search/detail/play 适配层 | 通过 | GET/POST 等价，播放 ID 不泄露凭据 |
| M7 | `/t4/` 独立 Web 入口 | 通过 | source、能力、冒烟测试和配置地址可用 |
| M8 | 容器、反向代理和授权真实回归 | 进行中 | AT 零修改，分类、详情、播放闭环 |
| M9 | AT 内置源原生桥接 | 通过 | 已完成服务端内置源的原生路由、参数和播放桥接；客户端本地/页面型源单独标记边界 |
| M10 | 本地插件完整管理与可用性仿真 | 通过 | Python/JS 上传、删除、排序、重命名、刷新；浏览器操作及持久化与方法链回归 |
| M11 | 统一影视订阅及 FM type=4 请求合同 | 进行中 | 服务端、Web 和真实 AT 抽样已通过；Android/TV 实机导入与播放待验收 |

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
- `csp_Push` 标记为 `client_local_only`，`csp_WebHome`/`csp_Nostr` 标记为 `page_entry_only`，不伪造标准 T4 能力。

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

- `GET /t4/config.json` 返回 `sites`；汇总 AT 与本地插件，跳过客户端本地/页面型源。
- 各站点 `type=4`，`api` 为可直接接收标准参数的绑定插件地址；站点 key 与名称和排序无关。
- 原订阅的搜索、快速搜索、筛选、分类等客户端选项按白名单保留，不下发原始 ext 或 AT 凭证。
- 支持 GET、JSON POST、表单 POST；将客户端筛选 `ext` 解码后交给现有适配层。
- 默认无新增鉴权要求；已启用保护模式的实例自动派生整包订阅和插件级访问钥匙。
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
- 统一订阅覆盖跨 source 重名隔离、稳定 key、白名单选项、敏感字段隔离、保护模式、
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
