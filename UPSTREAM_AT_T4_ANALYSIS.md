# AT 官方 T4 新提交核查与接入方案

核查日期：2026-10-28。项目版本仍为 `0.2.3`。初轮仅分析；
本轮已实施 U1/U2 的本地接入和 U3 的部分媒体合同补齐，未发布、未更新 NAS。
结论来自固定提交的源码、测试文件及本项目的本地复现，不等同于官方镜像或客户端实播验收。

**晚间更新**：2026-10-28 再次核验，最新 `master` 为 `c8b9e49`，
`1.121.0` 已正式发布，并新增 Python 插件运行时。
**第 1–8 节保留为早间 `81c8f08` 的历史证据，不再代表最新上游能力。**
当前差异、网盘加速项目对照和实施顺序以第 9–12 节为准。
本次只更新分析与节点，没有修改运行代码、订阅、AT 或 NAS。

## 1. 源码身份

通过公开 Git 仓库克隆源码，并两次执行：

```powershell
git ls-remote --symref https://github.com/power721/alist-tvbox.git HEAD
```

两次结果的默认分支均为 `master`，HEAD 均为：
`81c8f083c2d94249158a2a4cd504dfc2cf4acb4b`。
这是本次查询的最新分支状态，不保证后续不会继续变化。

| 提交 | 提交时间（UTC+08:00） | 实际变化 |
|---|---|---|
| [e890f2a](https://github.com/power721/alist-tvbox/commit/e890f2aff1ea5d2156ab488244cb4811523d3469) | 2026-10-28 08:11:48 | 新增 T4 订阅、14 个内置源五动作适配、服务端 B 站代理形态及 Web 订阅入口 |
| [81c8f08](https://github.com/power721/alist-tvbox/commit/81c8f083c2d94249158a2a4cd504dfc2cf4acb4b) | 2026-10-28 08:21:56 | B 站改为短代理地址，在真正拉流时重新取链；是上一提交的直接后继 |

前序加速分析固定的 AT 参考 SHA 已经是同一个 `81c8f08`；本轮是补齐逐方法核查，
不是发现了一个比前序参考更晚的提交。官方说明页标注“2026-10-27 实现”，
不能用该文档标注代替上述 Git 提交时间。

## 2. 官方已经解决的部分

下文 Java 文件均相对于 AT 仓库的 `src/main/java/cn/har01d/alist_tvbox/`，
行号绑定 `81c8f08`。

### 2.1 订阅与内置源协议

`service/SubscriptionService.java:731` 的 `subscriptionT4()` 从原订阅生成配置，
`applyT4()` 在 `:751` 将 14 个内置 `csp_*` 站点改为 `type=4`，
移除这些站点的 `jar/ext`，保留原站点 key 和其他属性。

```text
AT 配置： GET /t4/{token}/{sid}
单站 API：GET /t4/{token}/v/{site}
原订阅：  GET /sub/{token}/{sid}，保持不变
```

无 token 的配置入口为 `/t4/{sid}`；生成的单站 API 使用 `-` 占位。
这些是 AT 自身入口，不是 Sidecar 的 `/t4/config.json`。
上游既有鉴权仍由 AT 处理，不需要另外设计 Gateway Token。

`web/T4Controller.java:90` 根据 `play/ids/wd/t` 分派播放、详情、搜索、分类和首页，
而不是把客户端的 `ac=detail` 原样送进 `/vod`。
首页合并分类与推荐，分类解码 Base64 筛选，TG/盘搜族旁路缓存条目标题，
直播关注/取消关注及未开播占位在服务端处理。

内置源清单为 AList、小雅、片单、Media、B 站、Emby、Jellyfin、飞牛、直播、
TG 豆瓣、TG 频道、TG 网页、盘搜及分组盘搜。
它们复用现有 Java 业务，不是新建了一套 Python/JS 插件执行框架。

### 2.2 B 站短地址与延迟取链

```text
T4 play
  -> X-CLIENT 精确为 t4
  -> 返回 /proxy/{token}/{playId}，此时不调用 B 站取链 API
播放器开始拉流/重新连接
  -> BiliBiliController.proxy()
  -> resolvePlayUrlForProxy(playId)
  -> getPlayUrl(..., client=t4p)，取得当次 durl
  -> LiveProxyService 顺序转发，服务端补 CDN Referer
```

核心证据：`service/BiliBiliService.java:2323/:2480/:2487`、
`web/BiliBiliController.java` 的 `/proxy/{token}/{playId}`。
“play 接口零取链”只是把取链延后，不等于客户端首字节也没有取链耗时。
短地址改善签名过期时机，并不等于已完成持续连接中的自动续签或并发加速。

### 2.3 网盘服务端代理

`web/T4Controller.java:259` 的播放分派调用对应业务时不指定 `client-proxy`。
`service/TvBoxService.java` 的代理选择因此允许按原配置生成后端 `/p`，
而本项目当前默认指定 `type=client-proxy`，两者的数据通路不同。
是否代理仍取决于驱动及 AT 配置，不能概括为所有文件无条件走 `/p`。

`service/ProxyService.downloadStraight()` 自身仍是顺序转发。
并发分片能力来自已有 PowerList 下载器，不是这两个提交新增的独立分片器。
复用时应保持唯一分片层，不在 Sidecar 再叠加一层。

## 3. 不满足本项目无损目标的部分

| 项目要求 | 最新源码实际情况 | 对本项目的影响 |
|---|---|---|
| 全部插件服务端运行 | 第三方插件、WebHome、推送保持 Type 3；全局 spring.jar 保留 | 无 JAR 客户端不能因此使用全部订阅；ATVP/PY/JS Worker 仍有必要 |
| B 站保留原媒体能力 | BiliBiliService 的 `:2342` 对所有 `t4*` client 强制 `dash=false` | 不能用统一单文件 durl 代替原 DASH 后宣称无损；实际清晰度、编码及音轨需对照 |
| 保留字幕等附加数据 | 精确 `t4` 的短地址响应仅有 url/parse/jx；旧包装分支在 `:2468` 显式移除 subs/danmaku/chapters | 官方最小响应并不是原功能全量等价响应 |
| HTTP 媒体合同完整 | LiveProxyService 的 `:182` 普通转发仅传请求 Range，不回传 Content-Range/Accept-Ranges，也没有 If-Range 转发 | 206 响应缺少范围元数据；客户端 seek/长度识别存在兼容风险，必须验证和补齐 |
| HEAD/大文件正确性 | 同一转发构造上游请求时没有使用客户端方法；Content-Length 仅在不超过 Integer.MAX_VALUE 时设置 | HEAD 仍可能发起上游 GET，大文件长度交付不完整；不能把该代理直接当成熟大文件出口 |
| GET/POST 等价 | T4 配置和 VOD 使用 GetMapping | 上游新增入口没有提供本项目已有的 POST JSON/表单合同 |
| 取链与计算效率 | resolvePlayUrlForProxy 复用完整 getPlayUrl，后者仍调用 player info、组装附加数据并可能发 heartbeat，最终却只取 url | 每次回源可能有不必要工作；“短地址接口快”不能代替完整首播/seek 性能测量 |
| 原版增强交互 | 官方文档明确缺少设备端进度同步、弹窗确认、直播弹幕及 B 站本地换线 | 必须继续记为差异，不能因为官方称为取舍就计入本项目完成项 |

上述 Range/HEAD 结论是普通代理分支的源码合同缺口，不是本轮已经复现的真机故障；
未使用真实 B 站媒体访问来验证其客户端影响。
不把直播续租或 FLV 拼接路径搬入本项目；本项目不转码、不重封装、不解码。

## 4. 本项目的直接兼容缺口

### 4.1 实施前的新订阅识别缺口

实施前使用无凭证的构造夹具调用 `SubscriptionParser`：

| 站点 api | 实施前识别结果 |
|---|---|
| `csp_AList`，type=3 | builtin |
| `http://at.example.test:4567/t4/demo/v/AList`，type=4，无 ext | unsupported_plugin，t4_supported=False，解析错误数为 0 |

原因是原解析器只按 `csp_*` 及 ext 识别，没有官方 T4 URL 描述符分支。
本轮 U1 已补齐该分支并通过本地回归；真实官方服务与媒体等价未验收，
仍不能仅以解析错误为 0 建议生产直接换输入。

### 4.2 当前 B 站身份不会触发官方短地址分支

`sidecar/at/native.py` 的默认播放身份为 `t4-sidecar`。
在最新 AT 中它符合 `startsWith("t4")`，会被强制单文件；
但不符合精确 `"t4"`，不会触发短代理及最小响应分支。
因此，当前本项目不会仅靠升级 AT 就自动取得该短地址能力。
也不能只改一个身份字符串：这样会同时改变清晰度、附加字段和媒体路径。

### 4.3 同源反代存在路由冲突

仓库当前 `reverse-proxy/nginx.conf` 将整个 `/t4/` 转到 Sidecar。
如果同一个公网域名还要使用官方 `/t4/{token}/{sid}` 或 `/t4/{token}/v/{site}`，
请求也会被送进 Sidecar，可能返回 404。
部署时应区分 AT 上游地址与 Sidecar 公布地址，或制定明确的不重叠路由；
不能把该问题误判为 token 失效。本轮未修改现有反代。

## 5. 调整后的落地顺序

这两个提交可以减少内置源的重复适配，但不能取代整个 Sidecar。
保留统一订阅、插件管理、Worker 与媒体合同；对已验证等价的内置能力复用官方端点。
原 `/sub` 输入不自动切换业务链路；显式提供官方 `/t4` 订阅时才使用本轮的新接入。
用户已说明官方入口尚未正式发版，**暂时忽略真实官方入口，不将其作为当前升级前置条件**。
U1/U2 保留为本地预备能力，当前主线仍使用 `/sub` 推进 M20 与媒体合同。

| 节点 | 状态 | 实施及验收 |
|---|---|---|
| U0 固定上游证据 | 已完成源码核查 | 两个 SHA、真实方法、测试边界及本项目解析缺口已记录 |
| U1 描述符接入 | 已实现，本地合同通过 | 14 源及新旧路径名、原 Type 3/加密/PY 混合夹具；同源已识别端点，内部 URL 不出管理元数据 |
| U2 内置业务复用 | 已实现最小闭环，真实上游暂缓 | 同一 source 保留原 key 和业务身份；官方 GET 调用、Sidecar GET/JSON/表单、片单委托及独立门禁路径；不阻断旧路径优化 |
| U3 媒体合同与能力 | 取消与 HTTP 定向通过，整体未通过 | 媒体/插件代理异步取消已落地；真实 socket、5 GiB 虚拟资源重复 seek 和实际云盘首中尾字节通过；附加功能、签名刷新与长播待验 |
| U4 三方对照 | 待执行 | 原 /sub+JAR、官方 T4、Sidecar 使用同内容/账号/媒体档位交错复测；覆盖首字节、吞吐、seek 和附加功能 |
| U5 发布决策 | 待执行 | 复用方案满足 M20 原版基线后才切换；不满足的功能保持未完成，不修改 AT，不增加鉴权 |

U0 不等于 M20.2 原版客户端基线已采集，U2 不等于删除本项目现有内置桥接后盲目换地址。
浏览功能可先做窄范围验证，媒体性能结论必须等原版、官方 T4、Sidecar 三方证据齐备。
客户端不支持某种编码、容器或 DASH 时，服务端网络转发不能凭空补出解码能力；
在禁止重封装的前提下不能承诺所有播放器均能无损消费原档位。

## 6. 初轮分析验证与未验证

- 两次公开远端 HEAD 查询一致；固定检出工作树无修改。
- 已读取 T4ControllerTest、SubscriptionServiceT4Test、BiliBiliServiceTest、
  BiliBiliControllerTest 和 LiveProxyServiceTest；新增代理测试主要检查委派/域名，
  不能代替真实 HTTP Range/seek/长播验收。
- 上游提交说明称全量 2161/2164 测试通过，这是作者记录，不是本轮执行结果。
  本机未找到 Java/Maven，未运行上游 JUnit，也未下载新 JDK 或更改 AT 容器。
- 本项目定向回归：
  `python -X utf8 -m pytest tests/at tests/unit/test_subscription_parser.py tests/t4/test_player_headers.py`，
  **94 通过，1 条已有 Starlette 弃用警告**。
- 初轮 Type 4 识别夹具复现的是已知不支持状态；后续修复成绩见第 8 节。
- 未测试官方最新运行镜像、真实官方 T4、全订阅或真机，不变更已发布版本和线上服务。

## 7. 工程升级路线与对照要求

以下为包含官方预备接入的完整工程拆分；官方运行节点按用户要求暂缓，
执行时跳过该依赖，直接在现有 `/sub` 路径完成 U3、M20.2 和后续性能选型。
工期按单人、已有项目环境可用估算，为工程量，不是完成承诺。
设备、真实上游版本和账号资源等待不计入；既有插件依赖缺失仍按 M16 单独跟进。

| 批次 | 节点与依赖 | 工程量 | 交付与进入下一节点的条件 |
|---|---|---|---|
| 1 | U1，依赖 U0 | 0.5–1 人日 | 官方 14 源解析；混合订阅不丢条目；敏感地址不出管理元数据；旧输入行为保持 |
| 2 | U2，依赖 U1 | 1–2 人日 | 官方内置 HTTP 调度复用、三种传输、筛选/分页、详情/播放、片单与操作消息；真实官方服务逐源复验 |
| 3 | U3，依赖 U2；并行采集 M20.2 | 2–4 人日 | 媒体 HEAD/Range/取消/重连；同档位、字幕/音轨/章节；修正 Sidecar 责任内的缺口，不靠降质适配 |
| 4 | U4 对接 M20.3–M20.6 | 3–5 人日 | 三方交错复测及独立报告；选定唯一分片层；无 JAR 客户端首播、快进、切集、20–30 分钟长播 |
| 5 | U5 对接 M20.7 | 0.5–1 人日 | 门禁和性能证据齐备，得到发布授权后构建、发布和 NAS 升级；回读版本、配置与运行文件 |

基础路线估计 **7–13 人日**。若现有 PowerList 与官方媒体路径不能达到原版等价，
M20.4 独立分片调度另估 **3–6 人日**，须先完成数据通路选型，不预先堆叠第二个分片器。
本轮不为未落地的生产能力升版本，不将本地候选写成已发布新版。

### 7.1 本轮逐项落地

| 对照项 | 落地位置 | 当前证据与边界 |
|---|---|---|
| 描述符 | `sidecar/at/subscription.py` | 新增内部 `t4_endpoint`；管理响应仍用 `csp_*` 身份，不暴露原 URL；刷新后保持统一订阅站点 key |
| 调用所有权 | `sidecar/at/native.py` | 已识别官方条目复用共享 HTTP 客户端；普通 PY/加密插件继续走 Worker；不增加配置项或额外鉴权 |
| 统一出口 | `sidecar/t4/adapter.py`、`sidecar/gateway/routes/t4.py` | opaque ID、三种传输和附加字段归一化；显式模块入口补齐表单 POST；增加 official_t4 诊断分类 |
| 片单及动作 | 现有 Adapter 导航 + 官方 `/v` | 片单仍按标题检索再委托播放；不将官方空详情当可播；已有动作 ID 用 play 调度，没有 ID 的命令不猜测执行 |
| 媒体合同 | `sidecar/gateway/media_response.py`、`sidecar/gateway/streaming.py`、`sidecar/playback.py` | 未改写媒体保留校验头及范围语义；媒体和插件代理统一取消清理；同步门禁复用变换规则，不猜造范围元数据 |
| 自动验证 | `tests/at/test_official_t4.py`、`tests/unit/test_media_http_contract.py` | 本地 HTTP 合同、真实 Adapter/应用生命周期/七阶段门禁；资源为夹具，不是官方 Java 运行或真机证据 |

本轮不主动把旧输入替换为官方 `/t4`。同源完整 URL 是内部调用边界，
不是新增 Gateway 鉴权；AT 凭证继续来自原订阅，用户不需再次填写。
配置了 AT 子路径前缀时按该前缀识别；不能用其他站点的 Type 4 URL 冒充官方内置服务。

### 7.2 下一批执行顺序

1. 旧 `/sub` 链路的媒体断开取消、If-Range/seek 和大文件合同已定向通过；继续签名刷新和长播，不把已完成子项当作整个 U3 放行。普通直链不强制中转。
2. 取得 M20.2 原客户端完整加速基线，对照当前 Sidecar 的相同内容/档位及附加字段；按原 provider 配置选择唯一服务端加速层，不以短区间测速替代。
3. 完成选定数据通路的实现、交错复测和无 JAR 客户端验收，再按授权进入发布；功能缺失、性能退化或证据不足不放行。
4. 官方入口正式可用后，单独恢复 U2.4 与三方对照；此前不重复探测、不为解阻升级 AT、不让用户切换输入。
5. B 站官方路径的单文件降档、短代理 Range/HEAD 和附加字段差异保留在预备能力清单，不作为现有主线的默认实现。

每项记录输入版本/配置摘要、样本与覆盖范围、命令、结果和缺口。
现有 `gate.passed` 仍只表示服务器抽样阶段通过，没有被扩写为无损迁移放行。

## 8. 本轮实施验证记录

2026-10-28，未发布，产品版本仍为 `0.2.3`。

- `python -X utf8 -m pytest -o addopts='' -q`：**527 通过，1 条已有弃用警告**；
  相较原 392 项基线新增 135 项回归。
- 首次全量收集误入 `tests/reports` 中的历史发布副本与研究仓库而失败；
  `pyproject.toml` 现排除报告目录，不删除历史文件、不跳过本项目测试，修正后重跑通过。
- `python -X utf8 -m compileall -q sidecar scripts tests/at/test_official_t4.py tests/unit/test_media_http_contract.py`、
  `python -X utf8 -m pip check`、`git diff --check` 均通过；Git 仅提示既有自动行尾转换。
- 环境变量指向的真实原订阅 **200**，将同一订阅入口路径替换为官方 `/t4` 后 **404**。
  只记录到达该入口的结果，不能据此确定是旧 AT 版本还是反代路由截获；
  本轮没有读取运行 AT 的精确 Java 源码身份，也未更改其部署。
  随后的 NAS 只读入口探针自身执行失败，未产生可用于归因的运行证据；
  用户随后确认官方入口未正式发版、暂时忽略，已停止该方向探测。
- 复用 `python -X utf8 -m tests.reports.release.verify_vod_contract`：
  真实旧输入首两集、三传输 × 两入口 × 有/无 `ac`，**24 次取链全部 200**；
  两集首/中/尾均 **206**，每段 8192 字节；去掉原请求头均 **403**。
  36 条详情/播放 trace 正常完成，仍交付直链及 Referer/User-Agent，不解码。
- 脱敏旧链复测报告在本地忽略目录：
  `tests/reports/release/local-vod-1793176472863586200.json`。
- 独立本地开发实例监听 `127.0.0.1:8775`；Web、health、统一订阅均 200，
  统一订阅返回 173 个站点。使用隔离插件/日志/报告目录，未替换既有本地实例。

**未验证**：真实官方 14 源、官方短代理真实媒体合同、Linux 新镜像、全订阅、
原客户端完整加速基线、FM 真机、快进切集与长播。
U2 的真实服务节点按用户要求暂缓，不阻断当前 `/sub` 主线；U3/U4 无损验收仍未放行，
未发布或升级 NAS。

### 8.1 媒体生命周期续推

真实 socket 先复现等待响应头/首块后阻塞时上游未及时释放，再实现可取消的异步媒体和插件代理。
同步检测保留，共用原有 HLS/PNG-TS 变换规则；不引入第二层分片、转码或解码。
全量回归提升为 **551 通过**；24 项新增真实 socket 测试覆盖两种 ASGI、四种出口和异常清理。
旧订阅 24 次取链保持成功；实际 5566387403 字节文件的 HEAD、首中尾转发与原链一致，
本次主动断开约 11 ms 后记录释放。详细命令、独立报告及边界见 `IMPLEMENTATION_NODES.md` 的 U3 续推。
未进行官方入口探测、原版加速性能等价或客户端长播验收，未发布和升级 NAS。

## 9. 2026-10-28 晚间上游增量

### 9.1 版本与发布状态

再次读取公开提交 Atom、Release 页面和固定 SHA 源码快照，末次 HEAD 查询仍为：
`c8b9e49ecac76ed7abab3c8bbbb0e43da2b934f9`。
时间均为 UTC+08:00，不以“今晚”替代具体时间：

| 提交 | 2026-10-28 时间 | 核实到的变化 |
|---|---|---|
| `9ea3230` | 19:04:56 | B 站短代理调整为 `/proxy/bilibili/{token}/{playId}` |
| `94782b2` | 19:10:20 | 按 URL 签名期限缓存 B 站取链结果；`t4p` 跳过字幕/章节查询 |
| `1002c10` | 19:13:35 | 移除剩余 Type 3 及全局 JAR；普通 Type 0/1/2 保留 |
| `558ceaa` | 19:31:05 | 内置源支持表单/JSON POST；片单详情委托 `/media` 代搜 |
| `71b6a20` | 20:04:06 | Python 插件 Worker、Atvp 装载、插件 Type 4 化；镜像增加 Python 依赖 |
| `790b51b`、`01405e9` | 20:50:07、20:57:58 | 后台 print 与协议输出分离；JSON 行协议禁用 pretty-print |
| `9dfcf95`、`faabb02` | 21:22:15、21:26:18 | 新增 CENC 媒体桥及插件键查找修复；不是网盘多线程加速 |
| `c8b9e49` | 22:15:52 | `1.121.0` 发布说明 |

[Release 1.121.0](https://github.com/power721/alist-tvbox/releases/tag/1.121.0)
指向同一 `c8b9e49`，页面 `relative-time` 为 **22:21:26**。
Release Atom 的 **22:35:55 是 updated 时间，不是发布时刻**。
因此，早前“官方尚未正式发版”的外部事实已经改变；本轮不据此自动升级用户实例，
也不把正式 Release 等同于已部署镜像或本项目验收通过。

### 9.2 插件运行链路

以下位置绑定 `c8b9e49`，Java 根目录同第 2 节：

```text
subscriptionT4(..., PluginWorkerService.isAvailable())
  -> applyT4(): 内置 14 源 + 符合条件的 csp_PyProxy / 本机 Atvp 插件改成 Type 4
  -> /t4/{token}/v/{原插件站点 key}
  -> T4Controller.pluginVod()
  -> buildPluginExtPayload(): 服务端生成 ext，移除 local_proxy_config/preheatUrl
  -> PluginWorkerService: 每站点常驻 Python 子进程，JSON 行协议
  -> scripts/t4worker.py -> 内置 Atvp.py -> raw 或加密 Python 插件
  -> 插件/AT 取链；需要网盘后端代理时走 /p
```

关键位置：`SubscriptionService.java:740/:760/:790`、
`T4Controller.java:163/:218`、`PluginWorkerService.java:127/:144`。
解释器及 `requests/lxml/Crypto` 探测成功才加入插件源；未就绪时移除对应 Type 3，
不能把“配置还能加载”当成插件全量运行成功。

Worker 是每站点串行调用、全局串行冷初始化；初始化 45 秒、调用 30 秒、
闲置 10 分钟回收。已吸收请求 ID 保持、stdout 隔离、筛选参数 AST 探测及 base shim。
官方 `docs/t4-sidecar-borrow-assessment.md` 明确列出本项目相关实现作为参考。
这是可核实的实现交集，不表示两边现在拥有相同覆盖率或性能。

**需要撤回的旧结论**：官方已不只是 14 个内置源，也不再依赖客户端执行全部 Python 插件。
**仍成立的边界**：这不是通用 JS/JAR 运行时，插件 localProxy 没有通用 HTTP 桥接，
上传管理、逐插件门禁、HLS 交付和非侵入式部署仍不能由该新增 Worker 全部替代。

### 9.3 B 站可吸收的效率设计

`BiliBiliService.java:2487` 使用按 playId 的短期缓存，最多 300 条。
`t4ProxyUrlTtlSeconds()` 位于 `:2525`：
有 `deadline/e` 时 TTL 为 `min(1800, 剩余秒数-300)`，最小为 0；
识别秒/毫秒期限，无期限字段时为 600 秒。`t4p` 不再请求 player info。

可吸收的是“**结果缓存不能超过签名期限、代理取链只计算实际需要字段**”。
该代码是 `getIfPresent -> getPlayUrl -> put`，**不是并发 singleflight**；
并发 miss 仍可能重复取链。也没有在媒体 403 后自动失效缓存的完整闭环。
不直接把 B 站期限字段规则泛化给 115/夸克，不缓存会产生副作用的播放动作。
强制单文件、字幕/章节等差异仍需以原版同档位对照，不能将减少字段视为无损优化。

### 9.4 最新源码仍有的合同风险

下面区分源码判断和本地复现，未运行官方 Java 服务，未声称是真机故障：

| 问题 | 证据 | 本项目应如何处理 |
|---|---|---|
| 插件 JSON POST 未进入统一解码 | `T4Controller.java:111` 提前返回 pluginVod，JSON body 兜底从 `:114` 才开始 | 未来复用官方插件时服务端发送 GET；统一出口继续接受三种传输，不能照搬分派顺序 |
| 插件搜索页码丢失 | `pluginVod` 搜索只填 wd/quick；pg 仅在分类分支填入；Worker 默认搜索第 1 页 | 接入前加入第 2 页夹具和真实分页对照；不以 Worker 单独接收 pg 的测试替代 Controller 链路 |
| 插件推荐/动作不能等同完整宿主 | pluginVod 首页只调 homeContent；公开分派未消费 ac/action，Worker 虽有 action/local 方法却无对应通用 HTTP 入口 | 分别核对 homeVideoContent、action 和 localProxy，不能以五动作冒烟代替原版全部功能 |
| 插件刷新不重建现有 Worker | acquire 仅按 siteKey 返回存活进程，不比较新的 extend/source 修订 | 插件刷新必须绑定源码和配置代次；不能用站点名相同认定运行内容已更新 |
| stderr 管道可能阻塞 | 启动保留 stderr PIPE；正常运行无持续读取，只有初始化失败后读取尾部；Python 又将 print 导向 stderr | 保留本项目已有 stderr 消费与脱敏；上游实现不整段搬入 |
| 媒体桥 HEAD/错误语义不完整 | `PluginWorkerService.java:163` 起只转发 Range，未设置请求方法/If-Range；`:183` 用 getInputStream，4xx 不能原样进入正常复制 | 继续使用本项目已测的媒体合同；单独验证 416、HEAD 和 If-Range |
| 后缀 Range 解析错误 | `t4worker.py:137`；本地直接提取纯函数执行：1000 字节文件的 `bytes=-100` 返回 `(0,100)`，应为 `(900,999)` | 新桥不得视为完整 Range 参考；这属于真实函数复现，不是 Java/播放器实测 |
| 长播放可能被闲置回收 | lastUsed 在行协议完成时更新；forward 获取端口后开始长流，但没有活跃流计数；10 分钟闲置判断不能表示整个流已空闲 | 活跃会话和正在传输必须独立于控制调用时间；真实长播仍是门禁 |
| B 站普通流代理旧合同未补齐 | `live/service/LiveProxyService.java:182` 起仍缺响应 Content-Range/If-Range，长度设置仍限制为 int | 早间此项结论仍成立；不把 CENC 桥的新增响应头误认为 B 站代理也已修复 |

官方 CENC 桥是加密媒体处理，**不是网络分片加速，更不是音视频解码**。
本轮不移植该模块，不修改受保护媒体处理策略，不引入转码或重封装。
分离 `audio_url` 也不等同于所有客户端都具备选轨/合流能力。

### 9.5 对当前 Sidecar 的新影响

本轮用无真实凭证夹具验证官方新插件端点：

```text
输入: key=plugin-42, type=4, api=http://at.example.test:4567/t4/demo/v/plugin-42
输出: site_count=1, plugins=0
错误: site[0]: official T4 api names an unknown AT builtin
```

当前 `_official_t4_api()` 仅识别 14 个已知内置名，不能承接晚间新增的任意插件站点键。
本地 U1/U2 的“官方已支持”应严格限定为这 14 个内置源。
后续应增加**官方远程插件描述符**，保留原 key，执行归官方 HTTP，
而不是把任意名字强行当成 `csp_*` 内置类；远程插件不再在 Sidecar 重复启动一个 Worker。
旧 `/sub` 原有 PY/加密/JS 路径不受该新夹具影响，本轮不切换输入。

## 10. 网盘代理加速开源实现对照

### 10.1 检索范围与固定参考

本轮尝试通用检索与 GitHub 仓库检索，再沿项目引用追到原始实现。
内置 web 工具没有返回可用内容；Google 返回跳转壳、Bing TLS 失败，
GitHub API 匿名额度耗尽，因此改用公开 GitHub 搜索页、提交 Atom、
固定 SHA 的 raw/codeload 源码。未登录个人浏览器，未使用账号凭证。
`hjdhnx/drpy-node` 当前公开入口 404，不能据此读取其最新代码；
下面 drpy 结论**仅对应公开可读的 Hululu007 快照，不冒称原作者最新版本**。
这是可复核的候选集，不声称穷尽全网。

| 项目 | 固定 SHA | 核实到的机制 | 与本项目的关系及许可文件 |
|---|---|---|---|
| [PowerList](https://github.com/power721/PowerList/tree/34b0edb0b92d845f3790f168eb2a587eebd7ddd5) | `34b0edb` | `/p` 应用 driver 配置，RangeReader/Downloader 真正并发取片；支持多源 | 首选复用现有服务，不新增容器；AGPL-3.0 |
| [OpenList](https://github.com/OpenListTeam/OpenList/tree/6aeaf1dafd66fd1ac2b7ca7104b41a69e0070749) | `6aeaf1d` | 通用 RangeReader/Downloader、分片及并发限制 | 底层行为对照；不能假定拥有 PowerList 的 AT 配置映射；AGPL-3.0 |
| [公开 drpy-node 快照](https://github.com/Hululu007/drpy-node/tree/295f2b7047e14122d542a7736cb931e81abcf85c) | `295f2b7` | `/mediaProxy` 内存并发取流和磁盘 chunk 两种路径 | T4 外置媒体出口的实际范例；GPL-3.0，不能直接无检查复制 |
| [mediaProxy](https://github.com/Zhu-zi-a/mediaProxy/tree/c26c448c0a8b64968184ee06efca9f288f91d771) | `c26c448` | Go ConcurrentDownload/ProxyWorker/ProxyStop，参数化线程与片长 | drpy 注明的接口参考；根目录未见 LICENSE，暂不复制实现 |
| [FastStrm](https://github.com/wabisabi926/faststrm/tree/d72b2dd48d159e088980556c7f0c81fe338781b8) | `d72b2dd` | 115 Range 分片、按序写、单次换链协调、账号级并发许可 | 独立调度的优先行为参考，不是 T4 协议服务；MIT |
| [MediaFlow Proxy](https://github.com/mhdzumair/mediaflow-proxy/tree/99f4af1153d4766420a098a232183cba1efdc560) | `99f4af1` | 通用异步流、HLS/DASH、Range 断流续传 | 参考生命周期，不把清单预取当普通网盘文件多线程；MIT |
| [MediaFlow Proxy Light](https://github.com/mhdzumair/mediaflow-proxy-light/tree/a50f1a254bbc7d8610781d1c070a4d142781aa3f) | `a50f1a2` | Rust 连接池、按主机许可随流持有、流式交付 | 参考资源所有权；本轮不引入 Rust 服务或转码模块；MIT |

以上除 mediaProxy 仅下载核心文件外，均有固定源码目录；
FastStrm、OpenList、MediaFlow 两个目录是既有副本，本轮核验其远端 HEAD 与本地 SHA 一致。
许可列是各快照 LICENSE 的身份记录，不代表已完成引入代码时的许可审查。
所有候选均未部署或做本项目账号实测，项目 README 的倍速/内存宣传不作为选型证据。

### 10.2 优先级一：复用 PowerList 的单一加速层

最新 PowerList 仍存在完整链路：

```text
AT local_proxy_config
  -> AListLocalService.updateProxyConfig()
  -> PowerList settings.proxy_config
  -> /p -> applyProxyConfig(driver, link)
  -> common.Proxy -> GetRangeReaderFromLink
  -> Downloader(Concurrency, PartSize)
  -> 顺序输出原始字节
```

源码：`server/handles/down.go:49/:152`、
`server/common/proxy.go:20`、`internal/stream/util.go:32`、
`internal/net/request.go:60/:74`。`chunk_size` 在映射处乘 KiB；
这不是可随意改成 MiB 的同名参数。未配置时 Downloader 默认 2 路、8 MiB，
但 AT 的实际驱动配置可覆盖，**不能把默认值当成 NAS 当前值**。

最新 AT 插件主动移除 `local_proxy_config`，目的不是关闭后端加速，
而是不向 `/play` 声明设备端 `client-proxy`，使后端有机会选择 `/p`。
是否真的进入下载器仍须按 provider、返回 URL、驱动参数和请求日志确认。
AT `ProxyService.java` 与早间参考忽略行尾后无差异，其 Java 下载仍是顺序转发。

PowerList `internal/stream/util.go:135` 的多源路径会把并发乘源数并封顶 64；
这不是推荐并发数。源码为整个请求预建分片槽，调度许可在下载结束后释放，
不等于按播放器消费进度限制所有已完成缓冲。**有并发上限不代表有完整预取内存上限**。
先验单源，再验证多源的大文件、慢客户端和取消，不能直接启用多账号叠速。
115 曾出现的 4 路部分 403 仍有效，不因官方新增 T4 就消失。

最小候选通路：

```text
客户端 -> 既有统一 T4 -> AT 内网 /play -> AT /p -> PowerList -> 网盘 CDN
                    -> 既有媒体出口(仅必要时公开转发) -> 客户端解码
```

图中控制取链与媒体返回为两条阶段，不应让 `/play` 本身传输视频。
对客户端可达且头部合同合适的地址可直接交付；
需要隐藏内网或补头时使用现有出口，**Sidecar 在此不再开一层分片**。
不全局删除 `client-proxy`，只对明确选中的网盘服务端加速策略处理。
NAS 到 AT 使用已验证内网地址，避免带视频流绕公网反代回 NAS。
本轮历史短测中该绕行路径明显偏慢，但仍没有原客户端完整加速基线。

### 10.3 优先级二：FastStrm 的刷新协调与正确性合同

优先阅读：
`internal/handler/range_proxy.go:92/:122/:151/:185/:237/:343/:380`、
`range_limiter.go` 及相邻测试。

值得吸收的行为：

- 首片优先交付，剩余片按文件偏移排序，不把完成顺序当媒体顺序。
- 同一代链接失败时，多个分片共享一次刷新，不并发打爆网盘取链 API。
- 精确验证 206、区间、长度，以及真实短读/超长，不只看状态码。
- 许可随响应体释放；多个会话按账号共享预算，seek 取消在途任务。

不能照搬的部分：

- 其普通路径只在 GET + 上游 206 时启用；HEAD、200、If-Range 仍须独立处理。
- 刷新回调仅返回 URL，本项目必须同时刷新配套请求头、签名身份和对象校验信息。
- `validateChunkResponse` 没有把所有分片总长/对象校验头绑定到同一个初始对象。
- 首片连接还存在时，剩余调度会另取许可；若移植，必须把首片计入同一账号预算。
- 特定状态换链重试不能泛化为重新执行所有插件 play/action。

因此仅把它作为独立调度的规范和测试样本，不整包替换 Sidecar，也不增加 FastStrm 容器。

### 10.4 drpy/mediaProxy：证明可做，不证明可直接投产

`controllers/mediaProxy.js:91/:113/:332` 提供 thread、size 和 header，
按环境选择内存多流或磁盘分片。`utils/chunk.js` 和 `utils/pan/quark.js:1180`
还有磁盘缓存路径；`Zhu-zi-a/mediaProxy/proxy.go:96/:193/:207` 是更早的 Go 实现参考。
这说明 T4 的 JSON 取链层和媒体并发出口本来可以独立，客户端不需要运行分片器。

公开 drpy 快照的内存分支仍有明确风险：

- `start >= end` 会把合法的单字节 `bytes=0-0` 拒为 416。
- 没有 Range 也按默认范围写 206；不能代替正确的完整 GET 200。
- 子流没有严格校验每个响应的 Content-Range；不能把忽略 Range 的 200 拼回去。
- 在流异常后继续发送下一子流，可能产生字节缺口却打印成功。
- `reply.raw.write()` 未处理返回 false 的背压，不能仅靠流 API 推断内存有界。
- 随机 UA 可选项不能用于有 UA 签名绑定的 115；header 参数也不应留给用户填写。

这些为源码判断，未安装该项目依赖或宣称完成其运行验收。
本项目只吸收“控制面/媒体出口分离”的设计，不复用这些不完整的 Range 规则。

### 10.5 MediaFlow：连接生命周期参考，不是必需新服务

Python `mediaflow_proxy/utils/http_utils.py:179` 有断流后 Range 续传，
但接受 200/206，不能在已输出部分字节后无检查接受重来的完整文件；
遇到部分失败后正常返回也不能用于本项目“短包必须失败”的合同。
HLS/DASH 预取与普通 MKV 文件并行 Range 要分别验收。

Light `src/proxy/stream.rs` 可参考复用连接、按主机计费许可与流生命周期绑定，
但 Rust 重写或 README 自报性能数字不能证明本项目 Python 是瓶颈。
当前已完成异步连接池与真实断开回归，下一步先测吞吐/CPU/内存，再决定是否需要底层替换。
两个项目的转码、重封装等功能均不纳入当前方案。

## 11. 吸收后的推进顺序

不新开一套路线，继续 U1–U5 与 M20。下表仅是候选工程量，**未实施项目不写成完成**。

| 顺序 | 对应节点 | 交付与门禁 | 估计工程量 |
|---|---|---|---|
| 1 | U0.2 | 本轮固定新 AT/Release、开源实现矩阵、两个兼容差异复现 | 本轮研究完成 |
| 2 | U1.3/U2.5 | 新官方远程插件描述符及五动作；保留原 key；外侧三种传输转官方 GET；远程插件不重复起 Worker；分页、刷新、错误台账测试 | 1–2 人日 |
| 3 | M20.2 | 固定原 `/sub`+客户端/JAR完整基线；同账号、同内容/档位、同网络交错采样；现有 Sidecar/内网后端对照 | 1–2 人日及设备时间 |
| 4 | M20.3/M20.4-A | 先验证唯一 PowerList 加速；内部取链/回源地址分离，自动复用已有配置；HEAD/Range/取消/大文件与源字节一致；不新增 token | 2–3 人日 |
| 5 | U3.2/M20.4-B | 只有 A 未满足原版等价且归因明确时，按 FastStrm 行为设计独立调度；原子刷新 URL+header、对象一致性、账号预算、有界预取 | 额外 3–6 人日 |
| 6 | U4/M20.5–M20.6 | 三方对照、115/阿里/夸克/UC 分驱动验证；真机 seek/切集、20–30 分钟长播；附加功能和性能分别判定 | 2–3 人日及设备时间 |
| 7 | U5/M20.7 | 得到发布授权且门禁齐备后构建/发布/NAS回读；AT 自身升级单独处理 | 0.5–1 人日 |

本轮“正式发版”解除的是上游源码的等待理由，不是用户实例的版本限制或部署授权。
不自动将 `/sub` 改成 `/t4`，也不先删除仍能提供完整功能的本地 Worker。
每个来源只由一个运行时执行；每条媒体路径只允许一个分片调度层。
原有普通直链与 header 交客户端的职责不变，不以降档、删字幕、服务端解码换兼容成绩。

## 12. 本轮验证与证据边界

- 首尾两次公开 `master.atom` 一致，固定 `c8b9e49`；Release 页面同 SHA。
  本地早间研究目录保留，没有 reset 或覆盖。
- 新源码快照 SHA-256：
  AT `a9acb53ad78007e19631dbd3cb2e1718ed3556f5560c44ed5efcc79b5ccf7934`；
  PowerList `ac06ebdf1467479f2510a3c8a33b67adc9749c91e13685dc952c7fb9b2c82d48`；
  drpy `6ad33f51a51707cb04e823456fe05fe80225b1ea9f0d5f234f4d8bdd90d51a8e`。
  快照和源码仅在忽略目录 `tests/reports/research/`，不打包进产品。
- 直接提取上游 `_parse_range` 纯函数：4 个用例中后缀范围不匹配；
  超长末端钳制、单字节范围、越界起点的 3 项结果匹配预期。
  没有运行 CENC 媒体解密、完整 Python Worker 或官方 Java 服务。
- 官方新插件描述符夹具复现 `plugins=0`，明确是**当前未兼容项**，不是测试成功。
- 本项目定向命令：

  ```powershell
  python -X utf8 -m pytest -o addopts='' -q tests/at/test_official_t4.py tests/unit/test_media_http_contract.py tests/unit/test_media_disconnect.py
  ```

  **159 通过，1 条已有 TestClient/httpx 弃用警告，27.61 秒**。
  这是现有 14 内置/媒体合同回归，不包含上面未实现的新远程插件能力，
  也不是本轮重新执行早前 551 项全量。
- 本机未找到 Java/Maven/Go，没有运行上游 JUnit 或 Go 测试；
  官方 Worker JUnit 还限制 Linux/macOS，不把作者“2178 绿”写成本机测试。
- 未调用真实订阅或 NAS，未使用凭证，没有测速/发布/生产变更。
  原版完整加速基线、官方新运行镜像、真实多驱动和客户端长播仍待验证。

**当前决定**：保留非侵入式 Sidecar；内置和已验等价的官方远程能力按源复用，
PY/JS 上传与现有 Worker 保留；网盘先选唯一 PowerList 后端，
独立 Range 调度只在明确证明需要时实施。

## 13. 官方插件兼容落地

2026-10-29 接续实施，对应 U1.3/U2.5，不另建升级路线。

已吸收的是运行所有权与协议边界，不是整体复制上游 Worker：

1. 官方 Type 4 插件保留其 externalId / `plugin-{id}`，不再强制归到内置类名。
   内置源仍保留既有 csp 业务身份；内部 endpoint 不出现在管理元数据。
2. 官方插件交给 AT HTTP 执行，Sidecar 不重复解密或运行代码；统一出口、FM 地址、
   opaque 详情/播放 ID、原站点选项和错误台账继续复用现有实现。
3. 外侧 GET、JSON、表单均转换为官方 GET。新入口发送搜索 pg、筛选 ext 和 flag，
   但不补造上游缺失能力；独立插件 action 为 501，不误执行播放轨道。
4. 吸收 JSON 字符串结果规范化，应用到本地 PY/JS 五动作及 Atvp raw/加密内层。
   处理位置在目录包装、过滤器和上下文缓存前，不改 vendor；初始化、名称和 action
   普通字符串不误解析，非对象仍失败。
5. 订阅刷新可更新远端 endpoint 而保持客户端 key；门禁不回收 AT 拥有的 Worker。
   这不修复上游 Worker 按旧 extend 常驻的问题，也不保证刷新了上游插件代码。

本地定向回归分别为 191 和 36 通过；源码、用例和逐节点证据见
`IMPLEMENTATION_NODES.md` 的 U1.3/U2.5 实施记录。
最终全量 593 通过、1 条已有弃用警告；原云盘 24 次取链和两集首中尾 Range 回归通过。
真实 raw/加密各一条首页与分类通过，首页分别 6/12 类、分类分别 48/70 卡片。
只读现有 AT 官方订阅返回 404，原订阅仍能解析 178 条、统一导出 173 站点；
因此当前交付是官方远程插件的本地合同适配，而非真实官方服务、性能等价或长播验收。
没有切换生产订阅、更新 AT/NAS 或增加鉴权；PowerList 单层加速的原定门禁不变。
