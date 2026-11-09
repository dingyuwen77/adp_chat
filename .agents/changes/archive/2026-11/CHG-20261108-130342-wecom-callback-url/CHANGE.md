---
schema: coding-change/v1
id: "CHG-20261108-130342-wecom-callback-url"
title: "企业微信回调URL配置与启动路由"
level: L3
status: done
owner: "codex"
branch: "main"
created: 2026-11-08
updated: 2026-11-08
completion_gate: required
depends_on: []
affected_areas:
  - "wecom"
  - "config"
  - "api"
  - "docs"
affected_paths:
  - "src/core/config.py"
  - "src/api"
  - "tests"
  - "config.example.toml"
  - "docs"
contracts:
  - "可选wecom.callback_url；省略兼容默认回调路径；GET/POST使用同一验签处理器"
data_changes: []
---

# 变更摘要

用户准备自行将服务部署到CentOS Stream 9/x86_64，然后保存企业微信后台回调。新增同一TOML中的可选对外回调URL，启动按其路径注册现有GET/POST处理器，保持两份配置项和注释同步，补充配置、打包和后台操作说明。本次只完成本地准备，不实施阶段F真实接入。

# 背景、现状与问题

## 背景

正式需求来自路线图阶段F及用户确认的公网HTTP地址、自己部署、后续Ubuntu迁移。A～E已在main交付，不能以新方案替换现有网关。

## 当前现状

基线main为bde45890b57541f3556d15a8816fc43cb0c7d79b。原回调路径和两个模拟消费者硬编码/wecom/callback；deployment负责宿主监听，不负责后台URL。实际配置被Git和Docker忽略。

## 问题、根因或约束

原配置没有对外回调URL，用户无法只改配置更换接收路径。公网地址与监听地址属于不同边界，不能根据公网IP自动绑定宿主或自动改后台。服务器只能先启动后保存后台GET验证。

## 不修改的后果

配置值与实际HTTP路由可能不一致；旧运维说明的测试内网IP容易误导当前公网部署；错误状态目录可能落到容器临时层。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | 原回调通过既有处理器验签解密后写JSON状态 | src/api/wecom_routes.py、tests/test_wecom_callback.py | 复用处理器，不增加调试绕过 |
| E2 | 配置加载有严格类型校验，监听由部署脚本读取 | src/core/config.py、scripts/run.sh | 同一TOML增加可选字段，URL不接管绑定 |
| E3 | 用户确认CentOS Stream9和x86_64，自己部署 | 本次用户输入；scripts/build_image.ps1 | 使用linux/amd64，不改默认ARM64 |
| E4 | 原访问日志过滤器删除任意路径的查询串 | src/core/logging.py CallbackAccessFilter | 自定义路径沿用脱敏，无新日志体系 |
| E5 | run.sh仅为session/log父目录建立持久挂载 | scripts/run.sh paths_and_config | 服务器消息目录应位于../runtime下 |

## 推断与待确认

待确认：目标机Docker、网络、SELinux及企业微信后台保存接受结果，属于用户后续真实部署验收，本地准备不作通过声明。

# 目标、成功标准与非目标

## 目标

用户可只改同一配置的URL，先部署启动再保存后台，且本机模拟和既有单用户入口继续工作。

## 成功标准

- [x] 两份TOML的配置项、分组、顺序和注释一致，实际值保留。
- [x] 省略URL保留旧路由；配置新路径后GET/POST真实验签，生命周期重启移除旧入口。
- [x] 本机模拟器采用配置路径，仍向显式loopback目标发送。
- [x] 提供CentOS x86_64打包和先启动后保存后台的可复用说明。

## 范围

配置校验、路由组装、两个本地模拟消费者、针对测试、正式架构/路线图/运维文档及本地Change。

## 非目标

不SSH、不部署、不配置后台、不真实发企微、不再调用ADP、不生产压测、不发布Release。本次未请求新的Git提交/PR/合并，不继承A～E已结束单元的交付动作。

## 必须保持不变

原main.py、CLI、单用户API、员工身份派生与消息状态模型、出站HTTPS地址、验签规则、发送模式安全边界、锁定依赖和默认ARM64打包参数。

# 约束与意图决策

| 决策维度 | 当前决定 | 依据 | 影响 |
| --- | --- | --- | --- |
| 范围与负责人边界 | Parent单写者，独立只读Reviewer | 用户自己部署、canonical独立Review | 本地准备与实际F验收分开 |
| 接口与契约 | callback_url可选，callback_path派生，默认/wecom/callback | E1/E2 | 重启后GET/POST共用原处理器 |
| 数据与迁移 | 无Schema或数据迁移 | E1/E2 | 新字段不进入身份作用域 |
| 错误与失败语义 | 非法URL启动前报安全配置错误 | E2 | 不回显凭据URL，不请求该URL |
| 兼容性 | dataclass字段追加末尾；保留缺省及位置参数 | E2 | 旧配置与既有入口继续工作 |
| 部署与回滚 | 自行打包和部署；回滚时恢复旧路由及同步后台 | E3/E5 | 状态目录必须落在已有持久挂载 |

# 修改方案与决策依据

## 最小充分方案

配置增加callback_url和校验、派生path → lifespan加载后用路由工厂注册既有处理器 → 模拟消费者读取同一path → 同步配置模板和现有文档 → 测试、打包验证和独立审查。

## 证据到决策

| 决策 | 依据证据 | 为什么采用这个方案 |
| --- | --- | --- |
| 单一路由工厂 | E1/E2 | 保持一套验签和状态逻辑 |
| 公网host不改变bind | E2/E3 | 云NAT下公网IP未必属于宿主网卡 |
| 自定义路径沿用日志过滤 | E4 | 过滤器已与路径无关 |
| messages_dir使用../runtime | E5 | 位于正式脚本实际持久化目录内 |

## 备选方案与取舍

只保存URL而不驱动路由不能满足用户要求；由URL自动绑定宿主不适用于NAT和反向代理。没有引入这些方案，也没有额外配置来源或第二套业务实现。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 可选URL、旧配置兼容、模板注释同步与真实值保留 | docs/wecom-integration/02_实施路线图.md#AC31 | satisfied | test_wecom_callback_url.py及实际两份TOML37项/注释同构核对 |
| R2 | URL路径驱动GET/POST、验签存储、重启替换、保留原API | docs/wecom-integration/02_实施路线图.md#AC32 | satisfied | test_configured_callback_path_reuses_protocol_and_replaces_routes_on_restart；全171测试通过 |
| R3 | 模拟器采用路径且保留loopback安全目标，访问日志不泄密 | docs/wecom-integration/02_实施路线图.md#AC33 | satisfied | 本地客户端内存HTTP断言；既有CallbackAccessFilter与回调日志测试 |
| R4 | 先自行部署再后台保存，提供amd64打包命令并说明未部署 | docs/wecom-integration/02_实施路线图.md#AC34 | satisfied | 运维1.11/2.1；核对build_image.ps1参数和生成物逻辑 |

# 计划改动

| 文件 / 模块 / 资产 | 计划修改 | 原因 | 对应要求 / 证据 |
| --- | --- | --- | --- |
| src/core/config.py | 可选URL和派生path | 单一配置来源 | R1/R2 |
| src/api/app.py、wecom_routes.py | 启动组装与重启路由替换 | 同一验签入口 | R2 |
| tests/test_wecom_callback_url.py、test_wecom_callback.py | 校验/兼容/真实协议与持久化回归 | 证明真实接线 | R1/R2/R3 |
| tests/wecom_callback_client.py、wecom_dispatch_smoke.py | 消费同一path | 本机客户端兼容 | R3 |
| config.example.toml及被忽略实际配置 | 同步注释和新字段 | 保留真实值 | R1 |
| 架构、路线图、现有Docker运维文档 | 配置/启动/持久化/后台流程 | 可自行复现 | R4 |

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | URL格式、缺省、错误不回显、身份稳定；3目标测试通过 |
| 接口 / 契约 | required | 真实GET/POST与OpenAPI、无旧路由、healthz和原API回归；171 tests通过 |
| 集成 / 持久化 / 运行依赖 | required | TestClient lifespan、独立加密生成、真实JSON文件可靠保存，Worker暂停隔离外部请求 |
| 用户 / 工作流验收 | required | 本机客户端显式loopback+配置path；文档先启动再保存，不冒充后台验收 |
| 跨组件关键路径 | required | 配置→路由→验签解密→MessageStore→ACK，改路径并同app重启；仅更改的接线范围 |
| 外部依赖 / 供应方探测 | not_applicable | 未改变ADP/企微协议实现，本次禁止真实发送；云端、服务器验收留实际F阶段 |
| 构建 / 打包 / 运行 | required | 171测试验证ASGI启动；另核对wheel生产代码和PowerShell打包参数；Docker目标机由用户后续执行 |
| 文档 / 治理 / 其他 | required | TOML解析/同构/敏感值检查、git diff --check、源AC与Ready及独立Review |

## 验证计划

Validation Asset Redundancy=clean：3个URL测试覆盖不同配置/客户端边界，1个HTTP生命周期用例覆盖真实接线；未复制已有认证/并发/发送状态全套测试。

- Red：新URL测试在未实现时失败（3 tests，missing attribute/keyword），runtime/callback-url-red.log。
- Green：3 URL tests通过0.016s；1真实协议/重启测试通过0.116s，分别有runtime目标日志。
- 全回归：`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v`，171 tests/OK 28.399s，runtime/callback-url-regression.log。
- 打包：本地无隔离尝试因缺少setuptools失败；改为pyproject锁定版本的隔离构建，隔离构建成功，wheel SHA256为0847262546b8d7b202f92f3c07dc7efa6ee0761c510baf11a2fa2cebbe09798a；CRC及三份变更生产模块逐字节匹配源码。
- 就绪：canonical ready_check.py、独立read-only Review后本地归档。

# 风险、兼容性、迁移与回滚

| 项目 | 结论 | 依据 / 处理方式 |
| --- | --- | --- |
| 主要风险 | URL路径变更需同步后台及入口路径控制 | 启动替换旧路由，不双入口兼容；保留查询串验签 |
| 兼容性 | URL缺省兼容；字段追加，身份不变 | 目标及全回归证据 |
| 数据 / Migration | 不适用，状态Schema和身份算法未改 | diff只涉及配置和路由组装 |
| 部署 / 运行 | 用户在CentOS amd64自行部署 | Docker、SELinux、入站4560及真实回调尚待验证 |
| 回滚 / 恢复 | 恢复旧版本/URL并重启，同步后台 | 不自动改后台，不删除状态，不重投UNKNOWN |

# 文档、依赖、部署与发布影响

- 长期文档：更新现有架构、路线图和Docker运维1.11，不建立重复计划。
- 依赖 / Runtime：无新增或升级；Python3.14和正式镜像基础保持现状。
- 配置 / Secret：新增可选callback_url；实际配置同步只改变此字段和模板注释，既有值保留，未纳入版本控制。
- 部署 / Release：本次未执行；用户完成镜像构建/上传/实际配置/启动后再保存后台。

# 完成审计

- [x] upstream_re_read：已重新读取用户决定、路线图F1-C1～C4及正式架构；阶段F真实接入仍未验收。
- [x] change_coverage：AC逐项对应R1～R4与配置/API/消费者/运维diff，无遗漏。
- [x] reverse_audit：核对配置→lifespan→GET/POST→验签→JSON→ACK；客户端loopback路径；OpenAPI/旧路由移除；正式挂载与打包参数。
- [x] unresolved_cleared：需求表没有not_satisfied；各required验证层已获得本次范围证据，独立只读Deep Review已PASS且无Finding，Parent复核11份文件hash与底层证据后允许本地归档。

# 完成证据与状态

## 新鲜证据

| 证据 | 版本 / 环境 | 命令 / 检查 | 结果 | 证明了什么 |
| --- | --- | --- | --- | --- |
| V1 | bde4589+当前源码，Windows/Python3.14.7 | 上述unittest目标和完整离线回归 | 3+1目标、全171通过 | 新URL接线、真实验签存储、既有功能兼容 |
| V3 | 当前源码/锁定setuptools | pip wheel --no-deps . --wheel-dir runtime/callback-url-wheel | 隔离构建成功；ZIP CRC及模块匹配 | 正式wheel包含当前生产代码 |
| V6 | 当前11文件hash和受审Change SHA d25aea3b | 独立Reviewer /root/callback_url_review；Parent回读diff/hash/原始证据 | PASS / NO_FINDINGS_WITHIN_SCOPE；无blocking；runtime/callback-url-evidence.json留存 | A1/A2与代码质量通过，范围仅本地准备 |
| V7 | 当前Change与文档 | canonical ready_check.py --root . --require-active-ready；文档本地目标检查 | Ready 13/13；本地链接目标均存在 | 完成追溯与文档机器检查 |
| V5 | 当前CallbackAccessFilter | 自定义路径LogRecord实际过滤；receipt custom_path_access_query_redaction | 查询签名与密文不进入格式化日志 | 新路径沿用查询脱敏 |
| V4 | Windows PowerShell | Parser.ParseFile scripts/build_image.ps1 | 0语法错误；参数支持linux/amd64 | 文档命令使用真实脚本参数 |
| V2 | 当前两份TOML | tomllib解析，归一化赋值后比较全文 | 37字段、字段顺序和注释一致 | 同步性和实际值安全保留 |

## 未验证内容与剩余风险

未构建Docker镜像，未在CentOS/Ubuntu启动，未保存企业微信后台，未完成真实收发；阶段F仍未验收。无隔离wheel尝试缺少构建后端；标准隔离构建成功，使用项目锁定后端，未改依赖。wheel不替代Docker目标机验证。

## 交付状态

- 提交：本次未执行，main上的本地修改。
- 拉取请求 / CI / 合并：本次未执行；当前范围为本地准备。
- Change归档：本地准备范围已完成，经canonical archive归档到archive/2026-11/同ID；Git及真实阶段F状态分别记录。
- 发布 / 部署：未执行，用户自行部署。

## 备注

Ready初检指出inline列表不受当前carrier解析器支持，已改为标准块列表；F1-C1～C4增加与既有AC序列连续的AC31～AC34锚点别名，以符合官方稳定Acceptance绑定语法，需求文本未改变。

canonical Source锁定dbae4a011f78ddeaf6e7a0411d3fb40df858fbed，当前任务前已核对origin/main。测试伪造身份/密文不代表企业微信云端认证。本地归档与Git/服务器交付状态分开。
