# AList-TVBox 订阅转 T4 Sidecar 项目计划

> 版本：v0.4（2026-10-26 片单导航及原生 AT 推送兼容）
> 基线日期：2026-10-22
> 计划状态：独立附属容器方案
> 分析基线：`power721/alist-tvbox` master，已核验提交 `e0210e5ccc83a1a05f0fd3c5a636c44e8f080370`

## 1. 方案结论

推荐采用独立于 AList-TVBox 的 T4 Sidecar 容器，不修改 AT Spring Boot、`Atvp.py`、`spring.jar` 或现有插件源码。

```text
FM / TVBox
    -> T4 Sidecar /t4/api/*
        -> AT 订阅解析器
        -> AT 兼容 Plugin Worker
            -> Atvp.py
            -> 官方 secspider 加密插件
            -> 自定义 raw Python / secspider 插件
            -> AT 过滤器
        -> T4 响应归一化
    -> 播放 URL、Header、字幕和代理信息

浏览器
    -> AT 同源 /t4/
        -> Sidecar Web UI
```

### 1.1 为什么不能只做 HTTP 转发

AT 的订阅接口主要负责输出订阅配置、插件入口、`ext` 和插件地址。对 `csp_PyProxy`、`Atvp.py`、Python Spider、官方加密包的实际执行，仍然依赖客户端侧的 ATVP/FongMi Python 运行时。

因此：

- 只转发 `/sub`、`/vod`、`/play`，只能复用 AT 原生网盘能力，不能执行任意 Python 插件。
- 要支持官方加密插件和自定义插件，Sidecar 必须提供与 `Atvp.py` 兼容的 Worker。
- Worker 负责插件初始化和方法调用，Sidecar Gateway 只负责 T4 协议、隔离和结果转换；上游授权复用 AT 配置。
- AT 不需要增加新的服务端插件执行 API，避免把任意 Python 执行能力直接暴露到 AT 主进程。

### 1.2 非侵入式定义

本方案的非侵入式边界为：

- 不修改 AT 源码。
- 不替换 AT 的 `/sub`、`/vod`、`/play`、`/plugins` 路由。
- 不修改 AT 数据库表和插件实体。
- 不把 T4 业务代码编译进 AT JAR。
- Sidecar 只通过 AT 已有 HTTP 接口、授权订阅 URL 和可选的只读密钥卷工作。
- `/t4/` 页面由 Sidecar 提供，通过反向代理挂到 AT 同源地址。

## 2. 最小实现目标

第一期必须完成一条真实闭环：

```text
配置已授权 AT 订阅链接
  -> Sidecar 读取 /sub 配置
  -> 识别官方或自定义插件
  -> Worker 初始化 Atvp.py
  -> 执行 home/category/search/detail/player
  -> 转换为 T4 响应
  -> FM / TVBox 完成浏览和播放
```

### 2.1 第一期开启

- 一个独立 T4 Sidecar 容器。
- 一个已授权的 AT 订阅配置。
- 一个受支持的插件或插件集合。
- 官方 `secspider/1` 加密插件运行。
- 自定义 raw Python 插件运行。
- 自定义 `secspider/1` 插件运行，优先复用 AT 为订阅生成的带 keyring 的 `Atvp/{token}/{version}.py` loader；部署受限时再使用 AT 自用 keyring 的只读挂载。
- `Atvp.py` 的 `homeContent`、`categoryContent`、`searchContent`、`detailContent`、`playerContent` 调用。
- AT 过滤器的只读处理链。
- T4 首页、分类、搜索、详情、播放。
- GET、POST 双方式。
- 不增加 Sidecar 令牌，复用 AT 订阅和上游接口原有授权。
- `/t4/` 管理入口。
- 插件能力检测、超时、进程隔离、日志脱敏和错误码。
- 脱敏 fixture、单元测试、Worker 测试和一次授权真实回归。

### 2.2 最小 UI

AT 页面中的独立 T4 入口只实现以下功能：

1. 添加或选择一个服务端保存的 AT 订阅。
2. 显示订阅中的插件、插件类型和能力状态。
3. 显示可直接复制的统一订阅和插件 T4 地址。
4. 一键执行首页、搜索、详情、播放冒烟测试。
5. 复制 T4 配置地址。
6. 显示明确错误，例如 `unsupported_plugin`、`worker_timeout`、`decrypt_failed`。

第一期不实现插件在线编辑、任意代码上传、依赖在线安装和复杂后台。

### 2.3 第一期明确不实现

- 修改 AT 核心源码或重新打包 AT。
- 在 AT JVM 请求线程中运行 Python。
- 在 Sidecar 主进程中直接 `exec` 任意插件。
- 插件运行时自动 `pip install`。
- `csp_WebHome`、`csp_Nostr` 页面型入口的通用 VOD 服务端转换；Sidecar 只记录页面能力，不伪造 T4 VOD 结果。
- 任意第三方依赖的自动下载和执行。
- action、追剧写回、History、播放同步。
- 多租户后台和用户级权限模型。
- 通过管理页面、能力快照或 type=4 站点配置暴露 AT token、Cookie、secret、secspider keyring；
  原生 AT 推送按原版客户端合同透传自身 `jar/ext`，仅供可信客户端使用。

## 3. 插件兼容矩阵

| 插件类型 | 第一阶段 | 运行方式 | 约束 |
|---|---|---|---|
| 官方 `secspider/1` | 支持 | Worker 内置官方公钥/主密钥材料，执行 `Atvp.py` 解包 | 只支持 ATVP 已验证格式 |
| 官方 raw `.py` | 支持 | `Atvp.py` 加载明文源 | 需满足 FongMi/ATVP Spider 契约 |
| 自定义 raw `.py` | 支持 | Worker 加载用户授权的明文 Spider | 需通过兼容门禁 |
| 自定义 `secspider/1` | 支持 | 优先执行 AT 动态 loader，必要时只读挂载 keyring | keyring 不进入 Git、不返回客户端 |
| AT 过滤器 | 支持 | 随 `Atvp.py` 在 Worker 内加载 | 只读执行，失败策略可配置 |
| `csp_PyProxy` | 支持 | 由 `Atvp.py` 转换为 Worker 插件任务 | 禁止直接透传成公共 Python 服务 |
| `csp_WebHome` / `csp_Nostr` | 页面入口 | 能力快照标记 `page_entry_only` | 不等同于标准 T4 VOD |
| `csp_PianDan` | 标题导航 | `/pian-dan` 桥接并固定 `indexs=1` | FongMi 按标题搜索；非 indexs 客户端由 Sidecar 检索其他源并委托真实播放 |
| `csp_Push` | 原生客户端透传 | 统一订阅保留 `push_agent/type=3/csp_Push` 和原始 `jar/ext` | `native_client_passthrough`；不是独立 T4，需影视 JAR 运行时 |
| AT 原生 Java/网盘站点 | 旁路支持 | 调用 AT 原生 `/vod`、`/play` 等路由 | 不等同于 Python 插件执行 |
| 外部依赖未打包插件 | 暂不支持 | 不在线安装依赖 | 返回缺失依赖诊断 |

### 3.1 自定义加密插件的关键前提

官方加密包可以使用 `Atvp.py` 内置的官方 keyring。AT 当前会通过 `Atvp/{token}/{version}.py` 下发注入 AT 自用 keyring 的 loader，Sidecar 应优先读取订阅 `ext.loader` 并在 Worker 内执行该版本的 loader。这样不需要复制 AT 的密钥文件，也不需要修改 AT。

只有在 loader URL 不可访问、旧版 AT 没有动态 loader 或部署环境禁止 Sidecar 访问该地址时，才使用以下显式只读挂载作为回退：

```text
AT 数据目录/secspider/atvp-keyring.json
        -> Sidecar /run/secrets/atvp-keyring.json:ro
```

Sidecar 只读该文件，不提供下载、导出或 Web 展示接口。若动态 loader 和只读 keyring 都不可用，官方加密插件仍可运行，自定义加密插件返回 `custom_keyring_required`。

## 4. 总体架构

### 4.1 容器组成

第一期建议一个镜像、两个进程角色：

```text
t4-sidecar
├── gateway      # T4 HTTP、管理 API、结果转换
├── resolver     # AT 订阅和插件描述解析
└── worker       # 独立子进程或受限进程池，执行 Atvp.py/Spider
```

开发阶段可以由一个 Python 项目启动 Gateway 和 Worker；生产阶段必须通过独立进程边界隔离 Worker。

```text
AT container
  ├── 4567/5244 HTTP API
  └── /data/secspider/atvp-keyring.json

T4 Sidecar container
  ├── 8765 T4 API
  ├── /t4/ Web UI
  ├── AT_BASE_URL=http://at:4567
  └── optional read-only keyring mount
```

### 4.2 数据流

#### 启动或刷新订阅

```text
Sidecar Admin API
  -> 请求 AT /sub/{token}/{id}
  -> 解析 sites、插件 api、ext、source、token、secret、raw
  -> 解析插件包地址、Atvp loader 和过滤器地址
  -> 优先下载 AT 为订阅生成的 Atvp/{token}/{version}.py
  -> 生成 source capability snapshot
  -> 不保存明文插件密钥到浏览器
```

#### T4 浏览

```text
T4 /api/{module}
  -> source 白名单校验
  -> Worker job
  -> Atvp.init(ext)
  -> inner Spider.method(...)
  -> T4 normalizer
  -> JSON response
```

#### T4 播放

```text
T4 play request
  -> 解析 flag/play id
  -> 定位 source runtime session
  -> Worker.playerContent(...)
  -> 保留 url、header、subs/lrc、proxy 信息
  -> 签名 URL 不做长期缓存
  -> 返回 T4 播放结果
```

### 4.3 运行时状态和会话亲和性

`Atvp.py` 实例会缓存详情结果和播放上下文，尤其是详情返回的 `play_id` 与后续 `playerContent` 之间的关系。因此第一期不能把每个请求交给全新的无状态进程。

最小实现采用以下模型：

```text
source_key
  -> runtime_session
      -> 一个长期存活的 Worker 进程
      -> 一个 Atvp 实例
      -> 一个 inner Spider 实例
      -> detail/play context cache
```

规则：

- 每个启用的 source 至少绑定一个 Worker runtime session。
- 同一 source 的 `home/category/search/detail/play` 默认路由到同一 session。
- session 空闲超过 TTL 或 Worker 达到任务上限后重启，并清空旧的播放上下文。
- 重启后旧 `play_id` 必须返回 `runtime_session_expired`，不能随机调用新实例。
- T4 公开播放 ID 使用 opaque 包装，内部携带 source key、runtime session 标识和原始 play ID 的签名引用，不暴露 AT token 或插件 `ext`。
- 若后续需要横向扩展，多副本必须共享短期 session state，或改为自包含加密 play ID；第一期不做多节点共享。

这条约束是实现官方加密插件、自定义插件和分组播放链路的必要条件，不是性能优化项。

## 5. 对外接口设计

### 5.1 T4 公共接口

对外使用独立前缀，避免与 AT 现有 API 冲突：

```text
GET|POST /t4/api/{module}
GET      /t4/health
GET      /t4/api/sources
```

兼容字段：

```json
{
  "token": "gateway-token",
  "source": "configured-source-key",
  "ac": "list",
  "t": "分类ID",
  "pg": 1,
  "wd": "搜索词",
  "ids": "视频ID",
  "play": "播放ID",
  "flag": "线路标识"
}
```

至少支持：

| 功能 | 参数 |
|---|---|
| 首页 | 无 `wd`、`ids`、`play` 的请求 |
| 分类 | `ac=list&t=分类ID&pg=1` |
| 搜索 | `wd=关键词&pg=1` |
| 详情 | `ac=detail&ids=视频ID` |
| 播放 | `play=播放ID&flag=线路标识` |

如果目标客户端使用单一 `/t4/api` 而不是 `/t4/api/{module}`，增加路由别名，复用同一业务 handler。

### 5.2 管理接口

管理接口只允许内网、反向代理认证或明确的管理 token 访问：

```text
GET    /t4/admin/sources
POST   /t4/admin/sources
PUT    /t4/admin/sources/{id}
DELETE /t4/admin/sources/{id}
POST   /t4/admin/sources/{id}/refresh
GET    /t4/admin/sources/{id}/capabilities
POST   /t4/admin/sources/{id}/smoke-test
GET    /t4/admin/config
```

管理 API 返回脱敏信息：

- 返回 source 名称、插件名称、类型、版本、能力和错误。
- 不返回完整订阅 URL。
- 不返回真实 token、secret、Cookie、keyring 内容。
- 播放 URL、签名参数和插件明文仅留在 Worker 进程内。

### 5.3 Web 入口

优先使用反向代理挂载，不修改 AT 仓库：

```text
https://at.example/
https://at.example/t4/       -> T4 Sidecar Web UI
https://at.example/t4/api/* -> T4 Sidecar API
```

入口实现顺序：

1. Sidecar 自带 `/t4/` 静态页面。
2. Nginx、Caddy 或 Traefik 将 `/t4/` 转发到 Sidecar。
3. 通过反向代理注入一个导航入口，或在 AT 的外部首页扩展中增加“ T4 ”链接。
4. 如果部署环境无法做注入，再提供一个可选的 AT Web UI 单文件补丁；该补丁不是 Sidecar 运行依赖。

这样即使 AT 升级，T4 核心仍可独立升级，只有入口注入层需要复核。

## 6. Worker 设计

### 6.1 Worker 责任

Worker 只执行受控插件任务：

- 加载兼容版 `Atvp.py`。
- 解码订阅 `ext`。
- 加载官方或自定义 raw Python 源码。
- 解密受支持的 `secspider/1` 包。
- 提供 `base.spider`、网络请求、HTML、JSON、缓存等运行时兼容层。
- 调用 Spider 生命周期方法。
- 将结果通过内部 JSON-RPC 或管道返回 Gateway。
- 超时、异常和进程退出时清理任务。

Worker 不负责：

- 生成公共订阅。
- 管理 AT 用户。
- 写入 AT 数据库。
- 执行 action。
- 接受来自互联网的任意 Python 文本。

### 6.2 调度模型

最小实现采用受限进程池：

```text
Gateway
  -> job id
  -> worker subprocess
  -> JSON request
  -> method call
  -> JSON response
```

建议初始限制：

- Worker 并发：1 至 2 个。
- 单次初始化超时：15 秒。
- 单次列表/详情超时：20 秒。
- 单次播放解析超时：30 秒。
- 单任务最大响应：4 MB。
- 单 Worker 最大任务数：达到阈值后重启。
- Worker 临时目录：独立、可清理、不可写入 AT 数据目录。
- 同一 source 的请求必须保持 runtime session 亲和性。

插件需要访问公网时，至少阻断 localhost、内网网段、云元数据地址和危险重定向，防止插件通过 SSRF 访问 AT 管理面或 Sidecar 管理面。

### 6.3 兼容性门禁

每个插件进入 Worker 前必须记录：

- 插件格式：raw、official secspider、custom secspider。
- `Atvp.py` loader 和 `ext` 字段。
- loader 来源必须属于配置的 AT origin；记录 loader 版本和 SHA-256，禁止插件把任意远程 URL 当作运行时 loader。
- `Spider` 必需方法。
- `backend_parse`、`category_mode`、`local_proxy_config` 能力。
- 过滤器列表和失败策略。
- 明文源码或解密后源码 SHA-256，只保存摘要。

必须通过：

- ATVP 原始插件契约。
- FongMi 双运行时契约。
- 分类、详情、播放返回形状检查。
- 本地代理和播放 ID 检查。
- 无重复方法定义检查。

## 7. 工程目录

```text
sidecar/
  gateway/
    main.py
    config.py
    auth.py
    routes/
      t4.py
      admin.py
      health.py
    models/
      request.py
      response.py
      source.py
      errors.py
  resolver/
    at_subscription.py
    ext_decoder.py
    plugin_locator.py
    capability.py
  runtime/
    atvp/
      Atvp.py
      base/
      filters/
    worker/
      manager.py
      protocol.py
      subprocess_entry.py
      limits.py
      session.py
  adapters/
    plugin_adapter.py
    native_at_adapter.py
  normalizers/
    vod.py
    play.py
    error.py
  web/
    index.html
    assets/
  tests/
    fixtures/
    unit/
    worker/
    contract/
    integration/
  deploy/
    Dockerfile
    docker-compose.yml
    reverse-proxy/
  docs/
    deployment.md
    plugin-compatibility.md
    security-boundary.md
```

## 8. 进度时间表

按 1 名工程师、每天 6 至 8 小时估算。新方案包含插件 Worker 和 Web 入口，不能继续沿用原先 10 个工作日的纯转发排期。基础工作量按 27 个工作日排入日历，另留 4 个工作日缓冲；基线交付日为 2026-11-27，建议稳定交付日为 2026-12-03。

| 日期 | 里程碑 | 工作内容 | 产出 |
|---|---|---|---|
| 2026-10-22 至 2026-10-23 | M0 方案冻结 | 固定 Sidecar 边界、T4 字段、Worker 协议和插件矩阵 | 本计划、协议草案 |
| 2026-10-26 至 2026-10-28 | M1 Sidecar 骨架 | FastAPI、配置、token、健康检查、Docker 基线 | 可启动容器 |
| 2026-10-29 至 2026-11-02 | M2 AT 订阅解析 | `/sub` 读取、ext 解码、插件定位、能力快照 | source registry |
| 2026-11-03 至 2026-11-09 | M3 Worker 基础运行时 | Atvp.py、base.spider、进程协议、超时和重启 | raw 插件可初始化 |
| 2026-11-10 至 2026-11-16 | M4 加密与自定义插件 | 官方 secspider、自定义 raw、自定义 secspider（优先动态 loader，keyring 只读回退） | 三类插件运行闭环 |
| 2026-11-17 至 2026-11-20 | M5 T4 适配 | home/category/search/detail/play、GET/POST、播放归一化 | T4 API |
| 2026-11-23 至 2026-11-24 | M6 Web 入口 | `/t4/` UI、source 管理、能力检测、冒烟测试 | 独立 T4 页面 |
| 2026-11-25 至 2026-11-27 | M7 验证与交付 | fixture、兼容门禁、授权集成、部署文档 | MVP 候选版 |
| 2026-11-30 至 2026-12-03 | B1 风险缓冲 | 只处理上游字段偏差、插件兼容和真实播放回归 | 稳定交付版 |

### 8.1 关键路径

```text
M0
 -> M2 订阅/ext 解析
 -> M3 Worker 基础运行时
 -> M4 加密/自定义插件
 -> M5 T4 归一化
 -> M7 真实回归
```

M6 Web 入口可以与 M5 并行，但不能替代 M7 的真实插件和播放验收。

## 9. 工程量预估

### 9.1 MVP 人日

| 工作项 | 预估 |
|---|---:|
| 架构、协议样本和 AT 源码验证 | 2.00 人日 |
| Sidecar Gateway、配置和鉴权 | 2.50 人日 |
| AT 订阅解析、ext 解码和插件定位 | 2.00 人日 |
| Atvp.py 和 base.spider 兼容运行时 | 4.00 人日 |
| Worker session、进程池、超时、清理和资源限制 | 3.50 人日 |
| 官方 secspider 和自定义插件适配 | 3.00 人日 |
| T4 五类接口和响应归一化 | 2.50 人日 |
| `/t4/` Web UI 和反向代理配置 | 2.00 人日 |
| fixture、契约、Worker 和集成测试 | 3.00 人日 |
| 安全检查、部署文档和授权回归 | 2.00 人日 |
| **基础合计** | **26.50 人日** |
| 风险缓冲约 15% | 3.98 人日 |
| **建议总量** | **约 31 人日** |

### 9.2 最小化版本的工程容量

为保持可控，第一期限制为：

- 1 个 AT 实例。
- 1 个 AT 订阅。
- 1 个 Sidecar 容器。
- 1 至 2 个 Worker 并发。
- 只读 T4 五模块。
- 只支持官方 secspider、自定义 raw Python、自定义 secspider（优先动态 loader，keyring 只读回退）。
- 只支持一个反向代理入口。
- 不做数据库迁移；source 配置使用受保护 YAML/JSON 或 SQLite 文件。
- 自动化测试约 40 至 60 个用例。
- 至少 1 个官方加密插件和 1 个自定义插件完成分类、详情、播放回归。

### 9.3 后续增量

| 能力 | 增量预估 |
|---|---:|
| 多订阅、多租户和用户权限 | 5 至 8 人日 |
| 插件在线导入、编译和版本回滚 | 5 至 8 人日 |
| secspider keyring 管理和轮换 | 4 至 6 人日 |
| action、追剧和 History | 6 至 10 人日 |
| csp_WebHome 专用适配器 | 8 至 12 人日 |
| Worker 沙箱强化和多节点调度 | 10 至 18 人日 |
| 合入 AT Spring Boot 核心 | 15 至 25 人日 |

## 10. 验收标准

### 10.1 功能验收

- Sidecar 容器可独立启动，AT 停止时 `/t4/health` 仍能返回 Sidecar 自身状态。
- 管理页面可以读取一条授权 AT 订阅并显示脱敏能力状态。
- 官方 `secspider/1` 插件可以完成初始化、分类、详情和播放。
- 自定义 raw Python 插件可以完成初始化、分类、详情和播放。
- 挂载自用 keyring 后，自定义 `secspider/1` 插件可以完成同样闭环。
- T4 home、category、search、detail、play 支持 GET 和 POST。
- 详情包含可解析的播放组和播放 ID。
- play 返回媒体 URL，并保留必要 Header、字幕或代理字段。
- 不支持的插件返回明确错误，不伪造空数据和播放地址。

### 10.2 非侵入验收

- AT 仓库源代码、JAR、数据库结构和既有路由不发生修改。
- Sidecar 只使用 AT 已有订阅、插件内容和播放接口。
- `/t4/` 通过反向代理或外部页面入口提供，不要求 AT 重新编译。
- Sidecar 可单独升级、回滚和停止。

### 10.3 安全验收

- AT token、Cookie、secret、keyring 不进入管理响应和普通日志；type=4 配置不携带原始 ext。
- 原生 AT 推送仅在统一订阅下发原版 `jar/ext`，不新增授权字段或暴露 secspider keyring，
  完整订阅仅供可信客户端导入。
- 管理 API 与公共 T4 API 分离。
- Worker 不运行在 Gateway 主进程内。
- Worker 无法写入 AT 数据目录。
- 禁止在线安装任意 Python 包。
- 阻断 localhost、内网网段、云元数据地址和管理端口访问。
- 单请求、单 Worker、响应大小和并发均有上限。
- 插件源码、解密明文和播放签名不写入 Git。

### 10.4 兼容性验收

- 官方加密包通过 ATVP `secspider/1` 解包和签名校验。
- 自定义 raw Python 通过 ATVP 和 FongMi 双运行时检查。
- `backend_parse`、`category_mode`、`atvp_detail:`、播放 ID 和 `local_proxy_config` 行为保持兼容。
- 分类、详情、播放结果与原始 AT/FongMi 路径进行对照。
- 至少完成一次真实客户端或等价 FongMi 调用链回归。

## 11. 风险和处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 误把订阅配置当成插件执行服务 | T4 只能返回空列表 | 必须部署 Worker，不能只做 `/sub` 转发 |
| 自定义 secspider 没有 keyring | 无法解密 | 明确返回 `custom_keyring_required` |
| Python 插件执行任意网络请求 | SSRF、资源耗尽 | 独立进程、超时、内网阻断、响应限制 |
| 插件依赖第三方模块 | Worker 初始化失败 | 固定镜像依赖清单，不在线安装 |
| 播放 URL 短时签名 | 缓存后失效 | 播放实时执行，不做长期缓存 |
| AT 升级改变订阅/ext | 能力识别错误 | 记录 AT 版本、ext schema 和 fixture 回归 |
| Web 入口注入随 AT UI 变化 | 菜单消失 | Sidecar `/t4/` 永久入口，注入层只做增强 |
| 自定义插件包含恶意代码 | Sidecar 被接管 | 只允许管理员配置的授权源，Worker 与管理面隔离 |

## 12. 最小实现顺序

1. 固定一条脱敏 AT 订阅样本和一个官方加密插件样本。
2. 固定一条自定义 raw Python 插件样本。
3. 实现 Sidecar `/sub` 读取、ext 解码和 capability snapshot。
4. 移植并验证 `Atvp.py`、`base.spider` 和 secspider 解包路径。
5. 实现受限 Worker，先通过 `init -> category -> detail -> player`。
6. 实现 T4 Gateway 和五类响应归一化。
7. 验证 AT 动态 Atvp loader；仅在动态 loader 不可用时测试只读 keyring 回退。
8. 实现 `/t4/` 页面、source 管理和冒烟测试。
9. 做官方插件、自定义插件、GET/POST、超时和真实播放回归。
10. 最后接入反向代理并完成非侵入验收。

页面型入口、未打包依赖或未授权外部资源仍只记录能力状态，不扩大 Worker 权限。
AT 推送采用原生客户端透传，不将 Android 能力搬入 Worker，也不额外增加用户配置。

## 13. 项目决策

- **最终架构：独立 T4 Sidecar + AT 兼容 Plugin Worker。**
- **AT 核心零修改是第一期硬约束。**
- **官方加密插件和自定义插件必须由同一个 ATVP 兼容运行时处理。**
- **T4 Gateway 不直接解释 Python 业务，只负责调度 Worker 和转换响应。**
- **自定义 secspider 优先复用 AT 动态下发的 Atvp loader；只读 keyring 仅作为部署回退，不通过 Web 暴露密钥。**
- **AT Web 页面入口优先用反向代理 `/t4/`，不把 Sidecar 依赖写入 AT 构建产物。**
- **第一期先做只读浏览和播放；通用管理 action、History、WebHome 和多租户后置，Media 原版 `/play` 伪动作由内置源原生桥接覆盖。**
- **片单导航保留原版标题搜片语义；AT 推送保留原版客户端站点并汇入同一订阅，不伪装成标准 T4。**
- **多个 AT source 只导出首个可用 `push_agent`；无 JAR 的推送条目不伪造为 type=4。**

## 14. 源码证据映射

以下源码位置说明了为什么 Sidecar 必须复用 ATVP 运行时，而不是只转发订阅 JSON：

| AT 责任 | 代码位置 | 结论 |
|---|---|---|
| 订阅、VOD 和播放入口 | `src/main/java/cn/har01d/alist_tvbox/web/TvBoxController.java`、`PlayController.java` | AT 已有 `/sub`、`/vod`、`/play`，可作为 Sidecar 上游 |
| 插件源码内容 | `src/main/java/cn/har01d/alist_tvbox/web/PluginContentController.java` | 插件内容通过受保护的 `.txt`/`.py` 地址提供 |
| Atvp 脚本下发 | `src/main/java/cn/har01d/alist_tvbox/web/AtvpScriptController.java`、`AtvpScriptService.java` | `Atvp.py` 会根据 AT 自用 keyring 动态生成 |
| secspider 编译 | `src/main/java/cn/har01d/alist_tvbox/web/PluginController.java`、`PluginCompilerService.java` | 官方和自定义加密包有固定 `secspider/1` 合同 |
| ext 生成 | `src/main/java/cn/har01d/alist_tvbox/service/SubscriptionService.java` | `ext` 包含 `api`、`source`、`loader`、`token`、`secret`、`local_proxy_config`、过滤器等字段 |
| 实际插件执行 | `src/main/resources/static/Atvp.py` | 负责解码 ext、加载/解密 Spider、执行 category/detail/player 和过滤器 |
| AT Web 导航 | `web-ui/src/router/index.ts`、`web-ui/src/App.vue` | AT 当前菜单是 Vue 路由；直接加入菜单会修改 AT 前端，因此第一期采用反向代理入口 |

这些文件是设计基线，不代表 Sidecar 需要复制 AT 的数据库或 Java 业务实现。Sidecar 只复用其可观察的订阅和插件运行合同。

## 15. 0.2.0 兼容门禁迭代

版本基线、阶段验收和实际进展以 `CHANGELOG.md`、`IMPLEMENTATION_NODES.md` 的 M15 为准；
检测操作和判定规则见 `COMPATIBILITY_GATE.md`。

本轮最小闭环是全订阅条目清单、七阶段抽样、独立 Worker、进度/停止、
逐插件证据和报告导出，不自动发布、不替换 AT 插件、不增加鉴权。
先建立可重复的失败矩阵，再按源码责任处理兼容问题，不把“检测器能运行”误写成“所有源均兼容”。

下一轮从加载、分类/目录、搜索、详情、封面到解析/媒体按依赖顺序修复；
客户端推送、页面入口及服务器媒体证据与真机播放分别验收。
报告需要同时保留服务版本、进程启动源码指纹、脱敏描述符指纹和抽样范围。
发布前再检查报告身份和真实客户端结果，本地门禁不等价于 NAS 已升级。
