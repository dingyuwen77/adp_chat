---
schema: coding-change/v1
id: CHG-20261109-152936-wecom-public-images
title: 修复企业微信图片来源与并发内存预算
level: L3
status: done
owner: Codex
branch: fix/wecom-public-image-downloads
created: 2026-11-09
updated: 2026-11-09
completion_gate: required
depends_on: []
affected_areas:
  - wecom
  - configuration
  - docs
affected_paths:
  - src/wecom/images.py
  - src/core/config.py
  - tests/test_wecom_images.py
  - config.example.toml
  - docs/wecom-integration/01_网关架构设计.md
  - docs/adp-chat/02_Docker部署与运维.md
contracts:
  - wecom.image_allowed_hosts
data_changes: []
---

# 变更摘要

生产图片短链跳转到腾讯云 COS，被默认域名白名单阻止。按用户决定默认允许任意公网 HTTPS 图片及其跳转；保留内网地址、TLS、端口、凭据和单图/单回复限制，按新增用户要求扩大图片共享准备预算。本次仅修改、验证本地源码，由用户自行打包上传。

# 背景、现状与问题

## 背景

用户提供真实企业微信降级截图及 SSH 连接，要求低负载、避免频繁断连排查；随后要求放开来源域名，并明确只需修复本地代码。

## 当前现状

基线 a0dd0ec35c444380d76abbc89cdbd63c4cbc6528。服务器镜像 v0.0.11 标注该 revision，容器健康。默认只允许 lke.cloud.tencent.com，服务器配置省略 image_allowed_hosts。

## 问题、根因或约束

15:15:39 的通知为 accepted_with_image_fallback，正式答案包含五张图片。生产下载器复现 302 到 qidian-qbot-1251316161.cos.ap-guangzhou.myqcloud.com，随后 ImageError=image_url_blocked。仅在诊断进程内添加该完整域名后五张 PNG 全部成功，合计 367945 字节、1.938 秒。此结果只证明下载边界，不代表上传或客户端显示已经验收。

## 不修改的后果

正常图片换用其他公网 CDN 时仍会降级，逐个追加入域名不能满足用户放开来源的要求。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | 默认域名列表仅含短链源 | src/core/config.py / ImageDownloader.download | 需要修改默认来源策略 |
| E2 | 真实短链跳转被白名单拦截 | 单个 SSH 会话 73924，生产下载器只读复现 | 保留逐跳检查，放开域名 |
| E3 | 临时精确放行后五张 PNG 均可下载 | 同会话、总计 367945 字节、1.938 秒 | 问题直接发生在域名策略 |
| E4 | 用户仅授权本地代码修复、自己打包部署 | user:aima-local-fix | 不部署、不重启、不上传素材或发消息 |
| E5 | 用户明确选择“采用保守预算，部署后验证峰值（推荐）”，该问题已说明51并发及非实际RSS硬上限 | user:aima-image-memory-budget-confirmed | decision_epoch=5，按2GiB预约预算验收而非图片物理内存硬限制 |

## 推断与待确认

未验证新代码的真实素材上传及客户端显示；由用户打包上传后验收，不阻塞本地修复范围。

# 目标、成功标准与非目标

## 目标

正常公网图片及跨域 CDN 跳转不再因默认域名列表失败，同时保持服务器资源和网络边界。

## 成功标准

- [x] 默认公网 HTTPS 首链及跨域跳转经生产下载器成功，发送器输出原生图片。
- [x] 私网、回环、链路本地、混合 DNS 和跳转到内部地址仍在请求前拒绝；既有资源预算保持。
- [x] 旧显式域名列表和空列表语义兼容，新通配配置及省略配置均有直接证据。
- [x] 相关回归、文档与实际配置检查完成；独立复核无阻塞问题。
- [x] 仅交付本地源码，不打包、提交、推送或修改服务器。

## 范围

图片下载与配置默认值、直接回归、配置示例和图文运维/架构说明。

## 非目标

不修改消息正文、ADP、发送协议/状态、员工锁、单张/单回复上限，不新增依赖、CI、部署工具或公网 HTTP 支持。图片全局准备预算按用户新增要求调整。

## 必须保持不变

main.py/CLI 不显示图片，/api/v1/chat 保留，v3 快照无迁移，旧合法配置可读。HTTPS/TLS 校验、443 端口、无 URL 凭据、逐跳公网 DNS 与 IP 固定、无代理/自动重试、下载/并发/超时上限继续生效。

# 约束与意图决策

| 决策维度 | 当前决定 | 依据 | 影响 |
| --- | --- | --- | --- |
| 范围与负责人边界 | 本地单 Writer；独立只读 Reviewer | E4 | 无服务器变更 |
| 接口与契约 | image_allowed_hosts 默认 ["*"]；显式名单保留；[] 禁用 | 用户放开域名决定 / E1-E3 | 新通配值，旧值兼容 |
| 数据与迁移 | 无 | 仅下载准入策略改变 | 不写业务状态 |
| 错误与失败语义 | 保持既有降级和 UNKNOWN | 原始需求/架构 | 不重发历史答案 |
| 兼容性 | 保留显式完整域名限制 | 既有配置 Contract | 旧管理员设置不会静默失效 |
| 部署与回滚 | 用户打包；回退代码时将 ["*"] 恢复成旧列表 | E4 | 旧版本不识别通配值 |

# 修改方案与决策依据

## 最小充分方案

用户后续明确 2GiB 仅用于图片处理，并在 decision_epoch=5 明确接受保守预约、部署后验证峰值：将全局准备预算设为2GiB，以每张10MiB最大图片及缓冲/不可变bytes/HTTP与TLS等临时副本保守预留40MiB，从预算推导最多51张同时下载至上传。名额在下载前取得、上传完成/失败/取消后释放，等待者不持有二进制；下载连接上限与准备上限保持一致。此为应用层保守预约预算，不把它表述成操作系统统计的精确图片RSS或整个服务硬内存上限。保持每张10s/每回复30s准备时限，满额等待计入时限，超时原位置链接降级，纯文字不占图片预算。用户继续自行打包部署，不在服务器压测。

1. 配置新增单独的完整通配值 *，代码默认使用它；旧列表、空列表继续有效。
2. 下载器只在域名准入处识别通配，后续每跳公网 DNS、固定 IP/TLS 和资源约束不变。
3. 用生产下载器和发送器复现跨域 CDN、直接任意公网来源、回环/内网/混合 DNS 与恶意跳转；保留显式列表回归。
4. 同步当前文档、示例及被忽略的本机配置，实际身份/密钥/路径等其余值不变。

## 证据到决策

| 决策 | 依据证据 | 为什么采用这个方案 |
| --- | --- | --- |
| D1 | E1-E3 / 用户决定 | 放开域名解决 CDN 变化，无需逐个维护 |
| D2 | 既有安全与资源 Contract | 允许任意公网域名不需要开放服务器自身或内网访问 |
| D3 | E4 | 实际部署验证交由用户，不消耗服务器构建资源 |
| D4 | user:aima-image-memory-budget | 用户明确仅图片处理预算2GiB，51个保守预约名额替代原4名额；HTTP连接同步调整 |

## 备选方案与取舍

- 只追加这一个 COS 域名：可暂时解除当前拦截，但用户要求所有公网来源，后续 CDN 变化仍可能复发。
- 默认公网通配并保留可选显式列表：采用，兼容旧部署的主动限制。
- 删除所有 URL/DNS/资源校验：不采用，超出“放开域名”所需，图片链接会因此访问内部服务或耗尽资源。

# 需求追溯

来源稳定标识 AC1-AC8 分别绑定本轮用户对来源放开、保留已有安全边界、配置兼容、本地交付、main/CLI/API不变、低负载诊断、已明确确认的保守图片预算及容量满额行为的要求/约束。user: 标识定位本会话实际决定；R1-R8描述保持不变，不以本Change作为上游来源。R2的既有边界由正式图文架构说明交叉核对。

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 默认允许所有公网 HTTPS 来源及跨域图片跳转 | user:aima-public-images#AC1 | satisfied | 图片目标17项及最新相关149项回归通过，见 V2/V6/V7；AST/配置/文档见 V4/V8 |
| R2 | 保留内部地址及资源边界 | user:preserve-existing-image-boundaries#AC2 | satisfied | 图片目标17项及最新相关149项回归通过，见 V2/V6/V7；AST/配置/文档见 V4/V8 |
| R3 | 兼容旧显式列表及禁用配置 | user:preserve-existing-work#AC3 | satisfied | 图片目标17项及最新相关149项回归通过，见 V2/V6/V7；AST/配置/文档见 V4/V8 |
| R4 | 本地修复，由用户打包上传 | user:aima-local-fix#AC4 | satisfied | SSH 单连接已关闭；未改服务器或发送消息 |
| R5 | main/CLI 不显示图片且保留旧 API | user:wecom-local-cli-api#AC5 | satisfied | 图片目标17项及最新相关149项回归通过，见 V2/V6/V7；AST/配置/文档见 V4/V8 |
| R6 | 低负载、不频繁重连排障 | user:aima-log-diagnostics#AC6 | satisfied | 单连接； bounded log/record 读取及小图片下载；容器始终健康、restarts=0 |
| R7 | 仅图片处理使用2GiB保守预约预算，提高并发且预留临时副本空间，实际峰值部署后验证 | user:aima-image-memory-budget-confirmed#AC7 | satisfied | E5；2GiB/40MiB=51名额，V6/V7/V8 |
| R8 | 满额等待不占图片二进制；超时原位降级并继续尾文，纯文字继续 | user:aima-capacity-behavior#AC8 | satisfied | V6：100请求51活动，满额零下载/上传、超时降级保留尾文、释放后图片发送恢复 |

# 计划改动

| 文件 / 模块 / 资产 | 计划修改 | 原因 | 对应要求 / 证据 |
| --- | --- | --- | --- |
| src/core/config.py / config.example.toml | 默认通配、兼容旧列表 | 默认来源开放 | R1/R3 |
| src/wecom/images.py | 识别公网通配 | 不拦正常跨域跳转 | R1/R2 |
| tests/test_wecom_images.py | 图片来源及 DNS 回归 | 防止修复及网络边界回归 | R1/R2/R3 |
| 两份图文专题文档及本机配置 | 同步用法与兼容说明 | 用户自行打包可操作 | R3/R4 |

- [x] 调查实现、根因和上游决定。
- [x] 建立 L3 路由及本地验证矩阵。
- [x] 建立并确认目标失败证据。
- [x] 完成最小实现及相关验证。
- [x] 完成新增2GiB预算、百人本地Fake及满额超时回归。
- [x] 同步文档并完成覆盖最新epoch的独立复核。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 默认任意公网及跳转、内部地址拒绝、现有资源边界 |
| 接口 / 契约 | required | 配置省略/通配/显式名单/禁用的合法性和兼容性 |
| 集成 / 持久化 / 运行依赖 | required | 复用现有完整图片工作流及真实临时快照回归 |
| 用户 / 工作流验收 | required | Fake HTTP 通过正式 Sender 验证文字/图片/尾文；真实客户端验收交用户 |
| 跨组件关键路径 | required | 现有 encrypted callback → Worker → Sender 回归 |
| 外部依赖 / 供应方探测 | required | E2/E3 当前服务器只读下载，禁止上传/发送 |
| 构建 / 打包 / 运行 | not_applicable | 不改打包/安装/依赖；用户明确自行打包上传 |
| 文档 / 治理 / 其他 | required | 配置内存比较、链接、Change Contract、Ready 和独立复核 |

## 验证计划

本机 .venv Python 3.14 运行 unittest 目标图片测试、配置/发送/工作流相关回归。检查修改文件和本机 TOML、文档链接，执行 canonical governance_contract 与 ready_check。无服务器测试或 CI 变更。

# 风险、兼容性、迁移与回滚

| 项目 | 结论 | 依据 / 处理方式 |
| --- | --- | --- |
| 主要风险 | 默认扩大公网来源 | 仍逐跳拒绝所有非公网 DNS 结果，固定 IP/TLS，单图/单回复资源上限不变；全局图片预算按E5明确扩大 |
| 兼容性 | 旧名单、[] 保持 | 新值为完整通配 *，不接受部分通配域名 |
| 数据 / Migration | 不适用 | 不改任何持久化字段 |
| 部署 / 运行 | 用户执行 | 省略图片字段的服务器升级后自动使用新默认；已有显式旧名单需改通配 |
| 回滚 / 恢复 | 回退代码与通配值 | 旧代码无法读取 *，需同时恢复旧列表 |

# 文档、依赖、部署与发布影响

- 长期文档：只更新图文架构与运维的来源策略和升级提示。
- 依赖 / Runtime：无增删升级。
- 配置 / Secret：仅图片来源默认及通配语义改变；不输出或提交本机密钥。
- 部署 / Release：用户自行打包部署；本次不执行。
- 兼容 / 消费方通知：显式旧名单保持收紧，放开需配置 ["*"]；main/CLI/API 行为不变。

# 完成审计

- [x] upstream_re_read：重读最新用户决定及当前图文约束。
- [x] change_coverage：覆盖来源放开、保留边界和本地交付要求。
- [x] reverse_audit：复核 DNS、跳转、IP/TLS、资源以及配置消费者。
- [x] unresolved_cleared：仅在所有本地 required 证据满足后就绪。

# 完成证据与状态

## 新鲜证据

| 证据 | 版本 / 环境 | 命令 / 检查 | 结果 | 证明了什么 |
| --- | --- | --- | --- | --- |
| V1 | a0dd0ec / 生产镜像 v0.0.11 | 单 SSH 连接内生产下载器诊断 | 默认 image_url_blocked；临时精确放行五张下载成功 | 已确认域名准入是当前下载失败原因 |
| V2 | 本地 Python 3.14 / 当前工作树 | .venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_images.py -v | 修复前17 tests：16 failures（含14 DNS子用例）/1 error；修复后17 tests/0.101s/OK；补空名单回归后17 tests/0.106s/OK | 默认公网首链、CDN原生图片、内部地址逐跳拦截、名单与资源兼容 |
| V3 | 本地 Python 3.14 / 相同产品实现 | unittest.TestLoader.discover 按 test_wecom*.py、test_chat.py、test_cli.py、test_api.py、test_single_turn.py、test_service.py、test_project_startup.py、test_employee_identity.py 组装现有 suite；TextTestRunner verbosity=1 | 148 tests/15.909s/OK/0 skips；进程退出0 | 企微回调/发送/快照/跨组件工作流及配置、CLI/API相关边界 |
| V4 | 最终 tracked diff ba4b758a23fa67bd17390664ef852784466ee3d8a7deb7fbec73e348bd02ad1f | AST解析；示例TOML附fixture身份加载通配与省略字段；实际本机配置内存比较；相关文档文件链接及 git diff --check | 全部通过，100个文件链接存在；其余真实配置值不变 | 本次配置/文档及未改消费者正确，未上传真实配置 |
| V5 | 当前新Change | canonical governance_contract.py validate-change --path 当前Change | PASS | 新实例结构合规；语义与独立复核另验 |
| V6 | 新资源方案 / 本地 Python 3.14 | 图片目标测试同V2命令；新100请求/51活动及满额超时恢复用例 | 旧4名额实现17 tests/2.222s/errors1；51名额实现17 tests/0.270s/OK；补满额恢复后18 tests/0.307s/OK | 行为红绿，未以小PNG模拟测试推导真实峰值 |
| V7 | 最终 tracked diff 01579a239df5d5e29e0c7844013c9b8c72fadd23f2f139c810d6a7ce209ffd7e | 与V3相同8组patterns组装suite | 149 tests/16.473s/OK/0 skips，进程退出0 | 最新资源策略下所有相关本地边界通过 |
| V8 | 与V7相同最终产品文件 | AST/TOML；51×40MiB≤2GiB，52×40MiB>2GiB；100个文档文件链接；git diff --check | 全部PASS | 保守预约与文档/配置一致，不证明真实RSS硬限制 |
| V9 | /root/public_image_review / decision_epoch=5 / 最终 tracked diff同V7 | 原生新建隔离只读Independent Reviewer；独立A1/A2、资源生命周期与证据复核；代码hash逐一核对；git diff --check | NO_FINDINGS_WITHIN_SCOPE，0 BLOCKING；复核证据载体SHA256=78dd0e4f32d58398e6548ae24a3140d8e0b87f501d496c89e5d570e9e3834236（随后仅回写门禁状态与本条结果） | 最新用户确认的2GiB预约、满额行为、来源兼容及范围闭环 |

本地完成门禁：最终新实例治理Contract PASS；补齐既有user来源稳定AC1-AC8后，全仓Ready gated=15/strict=15 PASS。该记录变化不改变01579a239df5d5e29e0c7844013c9b8c72fadd23f2f139c810d6a7ce209ffd7e产品diff与已通过测试。

Validation Asset Redundancy=clean：复用现有图片/配置/企微测试体系，仅新增公网首链、跨域Sender、内部地址及预算满额恢复的实际回归；百人容量场景升级既有测试，不新增重复Harness。

## 未验证内容与剩余风险

新镜像素材上传及真实客户端显示由用户打包部署后验证，不宣称生产已修复。100人真实并发未在服务器执行；单lifespan共享51个图片准备名额，每张10MiB，原始图片有效数据最多约510MiB、预约总量2040MiB，均不代表进程总RSS（还含复制/HTTP/ADP/消息状态）。本次仅扩大用户明确指定的图片预算，不以本地Fake百人测试保证生产无OOM。

## 交付状态

- 提交：不执行，最新用户要求仅本地修复。
- 拉取请求：不执行。
- CI：不执行；本地相关验证承担当前证据。
- 合并：不执行。
- Change 归档：本地验证与 epoch5 独立 Review 已通过，由 canonical archive 本地归档；不代表Git提交/合并。
- 发布 / 部署：由用户执行，不改变服务器。

## 备注

用户追问后明确仅图片处理预算2GiB，允许本地实现；未授权新增服务器压力测试。Parent核对单lifespan单Sender、覆盖下载/上传的Semaphore、每图片bytes生命周期及HTTPX multipart直接迭代bytes；Semaphore原为4，当前由预算推导51。不把图片字节预算当成整机内存保证。

Canonical Source 本任务锁定 dbae4a011f78ddeaf6e7a0411d3fb40df858fbed；未修改 Agent_Skills。实现单 Writer；独立 Reviewer 仅只读。

用户确认最终语义后，资源方案取舍：固定原4并发不满足用户吞吐要求；仅把原始图片总字节上限设为2GiB忽略临时副本，可能明显超出预期；采用2GiB总预约、每张保守40MiB、最多51个完整准备生命周期，满足有界并发并避免为此引入动态分块抢占或死锁风险。
