---
schema: coding-change/v1
id: "CHG-20261107-221911-wecom-stage-b"
title: "企业微信阶段 B 加密回调与可靠接收"
level: L3
status: ready_for_review
owner: "codex"
branch: "feature/wecom-stage-b"
created: 2026-11-07
updated: 2026-11-07T22:55:04+08:00
completion_gate: required
depends_on: []
affected_areas: 
  - "api"
  - "storage"
affected_paths: 
  - "src/api"
  - "src/storage"
contracts:
  - "GET/POST /wecom/callback"
  - "schema_version=1 消息快照"
data_changes:
  - "messages_dir 单消息 JSON 接收状态"
---

# 变更摘要

实现 Roadmap 阶段 B：正式 GET/POST 回调验签解密和身份校验，可靠快照后 ACK；先去重、同用户原子占用/忙碌拒绝。A 已通过 PR #12/#13 完整交付；本阶段不调用 ADP。

# 背景、现状与问题

## 背景

正式需求来自现有 Roadmap B1～B6 与架构，用户已授权 A～E 顺序研发和门禁合并。

## 当前现状

base=76c3d2f8fc20afb57f66fce6da6820a2ec9ebee7。FastAPI 只有旧单用户 API；core.identity 已实现并真实验证。尚无回调、消息存储或 AES 依赖。

## 问题、根因或约束

ACK 前必须保存接收决定，去重/用户占用须同一临界区并以消息 JSON 为唯一事实源；否则重复回调和同用户竞态会造成重复调用。

## 不修改的后果

后续异步 ADP 没有可信可靠入站边界。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | A 已合并且 main-fresh 93 tests 通过 | PR #12/#13、归档 A | 复用 identity/chat/config |
| E2 | 官方协议为 SHA1 排序签名、AES256 CBC、32字节 PKCS7、尾部 CorpID | 企业微信官方 path/90968，本机HTTPS实际读取正文 | 不绕过验签 |
| E3 | 官方 path/90238 支持空 ACK 与 MsgId 排重 | 本机官方正文 | ACK先落盘 |

## 推断与待确认

Windows fsync+replace 支持本任务进程崩溃恢复；整机断电/文件系统损坏不声称零丢失。

# 目标、成功标准与非目标

## 目标

合法协议消息只能可靠接收一次，同员工新消息忙碌立即记录拒绝；无成功落盘不 ACK。

## 成功标准

B1～B6 全部满足；旧入口/配置回归；独立 Review/CI/guarded merge/main-fresh/归档/cleanup。

## 范围

wecom crypto、回调与接收编排、storage.messages、同源配置、协议模拟脚本/测试和现有文档。

## 非目标

C～F 调度/ADP/发送/JSONL/正式接入、生产部署、压测真实 ADP。

## 必须保持不变

旧 CLI/API/会话、A 派生契约、依赖现有版本、ADP 零重试。

# 约束与意图决策

单进程/单 worker，OS 数据目录独占；JSON 临时文件 flush/fsync 后原子替换。去重优先；新消息占用检查、落盘和内存索引在同一 RLock 内。资源饱和或落盘失败不 ACK；不按 ADP 配额排队。未启用时不改变旧接口启动。敏感字段 repr 隐藏；回调日志不输出密文/签名/正文。

# 修改方案与决策依据

## 最小充分方案

先协议/接收失败测试，再复用现有 FastAPI lifespan/config/identity 和正式存储；B 不提交任何 ADP 请求；C 扩展同一状态 Owner。

## 证据到决策

E1 支持复用既有模块；E2/E3 决定必须验签解密、校验内外企业应用身份、先落盘快速空 ACK。

## 备选方案与取舍

内存去重跨进程丢失；独立用户锁文件和消息文件产生两步事务缺口；数据库/Redis超出已定首期范围。采用单 JSON 快照事实源+目录独占+临界区索引。AES 无标准库实现，使用 cryptography 50.0.2（官方稳定版、Apache-2.0 OR BSD-3-Clause，Python3.14/Windows/Linux ARM64 wheel），不自行实现 AES 原语或采用过时 pycrypto。Delegation Value=MUST_SPLIT（独立只读完成 Review）；Independence Requirement=REQUIRED；实现单写者。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | B1 正式 GET/POST 协议 | docs/wecom-integration/03_实施路线图.md#AC6 | satisfied | 正式 GET/POST、独立 AES 布局回归及官方 path/90968 向量通过；真实 HTTP smoke 通过 |
| R2 | B2 签名/解密/企业应用身份拒绝 | docs/wecom-integration/03_实施路线图.md#AC7 | satisfied | 签名/接收企业/内外企业和 AgentID/员工/MsgId/DTD/巨包/旧时间戳均在写盘前拒绝 |
| R3 | B3 同用户竞态一次获准及 BUSY 提示 | docs/wecom-integration/03_实施路线图.md#AC8 | satisfied | Barrier 双线程真实 HTTP：一条 RECEIVED、一条 REJECTED_USER_BUSY/PENDING；跨员工独立获准 |
| R4 | B4 重复消息一次接收 | docs/wecom-integration/03_实施路线图.md#AC9 | satisfied | 并发相同 MsgId 只有一条快照/一次接收序号；顺序重复和跨进程重复不改变原意图 |
| R5 | B5 落盘失败不能ACK | docs/wecom-integration/03_实施路线图.md#AC10 | satisfied | 真实 fsync/replace 故障和发布后结果未知均不 ACK；关闭后续接收，重启按已保存快照去重 |
| R6 | B6 非文字/事件不投ADP | docs/wecom-integration/03_实施路线图.md#AC11 | satisfied | 非文字和事件返回空 ACK，不创建状态、不调用 ADP（正式出站边界断言） |

# 计划改动

src/wecom、src/storage/messages.py、src/application/wecom_gateway.py、src/api、src/core/config.py/logging.py；requirements/pyproject/uv.lock；测试/模拟脚本；现有 README、架构、Roadmap、运维说明。每项对应 B1～B6 或兼容/安全边界。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 官方向量、坏签名/填充/长度、XML/身份、去重/占用 |
| 接口 / 契约 | required | 正式 GET/POST 空ACK与拒绝，旧API兼容 |
| 集成 / 持久化 / 运行依赖 | required | 真实临时文件、fsync/replace故障、线程竞态、目录独占/重启 |
| 用户 / 工作流验收 | required | 正式HTTP与加密模拟脚本 |
| 跨组件关键路径 | required | HTTP→crypto→身份→快照→ACK |
| 外部依赖 / 供应方探测 | not_applicable | B不调用ADP或企微；协议以官方资料/向量核验 |
| 构建 / 打包 / 运行 | required | 新namespace安装及AES依赖，当前回归 |
| 文档 / 治理 / 其他 | required | 同源配置、链接、Ready/独立Review/CI |

## 验证计划

新增test_wecom_callback/test_message_store targeted Red/Green；正式unittest discover完整回归；本机HTTP脚本；canonical Ready与governance检查；PR/head及main-fresh CI。

# 风险、兼容性、迁移与回滚

回调鉴权/文件一致性为 L3；只支持单企业单应用文字。timestamp 有界新鲜度，防过期重放；对重复已落盘记录先去重。消息schema v1拒绝损坏/未知版本；不得静默重新执行。新状态目录与旧会话文件分离。旧版回退先停网关、保留状态并移除新增配置；不得用旧版继续处理新消息。无生产迁移或部署。

# 文档、依赖、部署与发布影响

targeted 同步现有Owner，不新增重复文档主题。cryptography为协议AES必需新依赖，官方版本锁定并同步uv.lock，不升级现有依赖。实际根config保留旧值仅补必需字段，测试密钥独立且不提交。无Release/正式部署。

# 完成审计

- [x] upstream_re_read：重新读取 B1～B6。
- [x] change_coverage：上游要求完整映射。
- [x] reverse_audit：回调→可信身份→快照→ACK与失败。
- [x] unresolved_cleared：required证据完整。

# 完成证据与状态

## 新鲜证据

B-RED：`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_callback.py -v`，exit 1，storage.messages 不存在；此前未写生产实现。

B-GREEN：Windows Python 3.14.7，`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v`，110 passed / 0 failed / 0 skipped / exit 0，13.647s；新增 17 项接收/协议测试与原 93 项回归。完整底层输出 runtime/wecom-stage-b-regression.log，仅本机保存。

B-HTTP：`.venv/Scripts/python.exe -B -X utf8 tests/wecom_local_smoke.py --report runtime/wecom-stage-b-http.json`，exit 0；独立 Uvicorn PID 200、11720，前后两进程共用隔离测试目录；GET/POST→落盘→空 ACK，YNND 首条获准/第二条 BUSY、SIYU 独立获准、重启相同 MsgId 去重。3 条快照保持正确，最长客户端耗时 228.0ms（包含客户端进程启动，不是纯服务耗时）。正常 lifespan shutdown、两进程退出、监听释放、临时配置/数据清理均实查。没有调用 ADP/企微，测试身份不作云端认证证据。

B-PROTOCOL：本机 HTTPS 实际读取企业微信官方 https://developer.work.weixin.qq.com/document/path/90238、/90930、/90968 正文，使用官方公开测试向量经生产 WeComCrypto 解密并解析 Content=hello，结果 passed，不输出或提交向量里的示例密钥。自动回归使用动态随机测试密钥和独立 AES/SHA1 夹具，避免生产加解密自洽冒充协议兼容。

B-PACKAGE：`uv build --wheel --out-dir runtime/wecom-stage-b-package --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple --no-progress`，exit 0；zipfile 实读 adp_chat-0.1.0-py3-none-any.whl，确认五个新增正式模块和固定 cryptography==50.0.2 的 METADATA。正式 editable 安装也已更新。

B-DEPENDENCY：官方 PyPI 元数据核实 cryptography 50.0.2（2026-09-30，Apache-2.0 OR BSD-3-Clause），支持 Python3.14、Windows/Linux ARM64 wheels；新增 cffi 2.1.1、pycparser 3.12，uv.lock 对比现有版本变化={}。原官方源下载无进展后取消本任务 uv 进程并确认退出；临时使用当日核验的清华官方镜像说明 https://mirrors.tuna.tsinghua.edu.cn/help/pypi/，从既有 uv.lock 生成三包固定版本及全部允许 hash，`uv pip install --python .venv/Scripts/python.exe --no-deps --only-binary :all: --require-hashes --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -r runtime/wecom-crypto-install.txt --no-progress` 成功，保留 TLS/hash、canonical lock 来源和全局配置不变。非隔离 editable 构建曾因环境无 setuptools 失败，未改项目规则；按声明的隔离构建成功。

B-DOCS：六份受影响现有文档同步（README、docs 索引、运维、架构、日志、路线图）；七份当前文档的本地链接解析无缺失；实际 config 与 example 的 section/key 相同，只补六个新字段且保留既有值；函数中文文档 AST 检查无缺失；git diff --check 无错误。

## 未验证内容与剩余风险

B 实现和已选择验证完成，当前独立 Review/PR CI、guarded merge、main-fresh、归档及分支清理尚未完成。B 不包含 ADP 调度、真实企微接入、通知实际发送或 Docker 网关验收。

## 交付状态

implementation=complete；delivery=incomplete；validation=complete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=not_applicable（正式Roadmap）；cleanup=incomplete；end_to_end=incomplete。

## 备注

Source锁定5f406b3，本次A～E共用；不修改canonical。
