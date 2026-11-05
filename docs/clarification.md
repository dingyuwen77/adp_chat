# 追问支持的范围与验收

本次需求来自用户要求：三类输入支持追问、正常答案停止、区分首问与补答、核对最新文档；后续新增 `18` 文字识别、引用会话中的独立业务状态方案，以及仅写文件的调试日志。该文档承载任务契约与验证记录，使用说明统一放在 README。

## 本轮增量：普通智能体的文字追问

用户进一步明确要求：`ReplyMethod=18` 的文字追问也应自动识别，同时避免把正常回答误判为追问。已确认原因是客户端此前只检查协议标记；`18` 本身只说明智能体回复，不区分答案与文字追问。无需改变 SSE 或追加一轮云端分类请求。

兼容模式使用保守的本地句式规则：正式协议优先；仅在 `18` 且无明确状态时检查完整回复中的直接补充要求、信息询问或选择确认。排除已测试的引用/围栏及行内代码/FAQ、条件建议、可选邀请和独立操作答案。首问跨补答保留，避免作品输出被误判。`clarification_source=text_rule` 标明推断，可通过配置关闭；不宣称覆盖任意自然语言。

方案比较：仅协议判断已被真实场景证伪不足；独立模型分类需要新增服务配置、请求成本和异常链，且也无法保证绝对零误判；本地保守规则适用于当前中文咨询场景，不增加依赖和外部调用。自然语言判定只能对已验证范围作结论，不能宣称任何措辞都绝对无误判。

本轮验收：原始漏判样例恢复；普通/图片/文件与 CLI 共用识别；首问/补答状态正确；正常答案、FAQ、引用、代码、建议和可选邀请不读取答案；关闭配置恢复协议判断；有界真实咨询验证识别与续答。新增测试先失败再实现；最终结果记录在本轮交付章节。

## 本轮增量：明确业务状态与文件日志

用户引用“设计ADP追问方式”后，明确业务契约作为推荐主路径：ADP 工作流/最终 Agent 负责输出 `interaction`，客户端显式开启 `structured_interaction` 后严格解析。支持 `ANSWER/COMPLETED`、`CLARIFICATION/WAITING_USER`、`CONFIRMATION/WAITING_USER`，保留原始 `ReplyMethod`。缺失、冲突、重复键或损坏输出报错，不回退文字猜测。只有把真正的参数完整性与确认状态输出为契约，才能减少客户端猜测；给文字猜测套 JSON 不会获得确定性。

本项目契约和云端配置说明见 [docs/adp-interaction.md](adp-interaction.md)，机器 Schema 见 [docs/interaction.schema.json](interaction.schema.json)。它是项目业务协议，不是腾讯云原生字段。当前未修改或发布云端应用，不能把本地构造 SSE 当作云端结构化适配通过。

日志采用独立文件 logger：北京时间、毫秒、真实源码位置及级别；UTF-8、有限轮转、进程内并发锁、每条记录释放文件句柄，不改宿主 root logger。关联ID、阶段、状态、耗时和安全错误分类之外，按后续用户要求完整保留当前问题、默认保留回答前100个Unicode字符；先脱敏再截断与单行转义，不额外记录配置对象、完整API请求或自动生成的附件签名链接。启动时不可写在请求前失败，运行中写入失败不转写控制台、不引发业务重试。实际输出为 `logs/adp_chat.log`，已加入忽略规则。

## 决策与范围

采用无持久状态的单轮函数，加一个回调驱动的自动续答函数。仅保留原有单轮函数无法完成终端追问；把 `input()` 写进底层函数会妨碍 Web、测试和其他 Python 程序接入。分层方案让调用方负责收集真实答案，组件负责会话和协议。

- `chat()` 保持原有参数和返回字段，追加追问状态及 `reply_to`。单轮函数始终只请求一次。
- `chat_until_complete()` 接收 `answer_provider` 和可选 `on_result`；普通结果直接返回，追问才调用答案提供器。
- 明确状态优先于推断；结构化模式使用业务契约及原生交互，兼容模式使用 `15`、未答问卷和可关闭的文字推断，单个问号不作判据。
- `reply_to` 复用原会话，并把当前输入标记为 `clarification_answer`。只传会话 ID 无法恢复此前的等待状态，不声称能自动识别。
- 自动续答只在首轮上传附件，每轮使用独立请求 ID。回调异常与取消直接交还调用方，不自动重试。
- 问卷以文字汇总答案；自定义 Widget 动作由调用方按实际卡片的事件契约提供，组件保留并发送正式 `widget_action` 字段，不推测动作名称。

不改变依赖、COS 权限或 SSE 成功条件；新增配置具有兼容默认值，真实配置未改。没有数据库或部署迁移。`structured_interaction=false`、`detect_text_clarification=false` 可恢复仅平台标记的路径，`log_enabled=false` 可关闭文件日志。旧 `chat()` 参数保持兼容。

## 官方依据

2026-11-05 查询 [HTTP SSE V2 文档](https://cloud.tencent.com/document/product/1759/129202)（页面更新时间 2026-09-18），核对请求地址、会话和请求标识、回复方式、问卷以及 WidgetAction 的字段。另查 [澄清询问 Widget](https://cloud.tencent.com/document/product/1759/127034)（页面更新时间 2026-01-16）：真实追问取决于已发布应用的能力和澄清配置。

## Requirement Traceability

| 来源 | 要求 | 状态与证据 |
| --- | --- | --- |
| user:三类输入支持追问 | 共用流程、会话延续、附件首轮上传 | satisfied（本地协议/规则/业务契约范围）；真实附件工作流未验收 |
| user:18文字追问与正常答案 | 识别已测直接追问，避免已知误判 | satisfied（有限范围）；正反例/独立反例回归及真实两轮续答，不宣称任意自然语言零误判 |
| user:识别输入阶段 | 首问和追问作答可区分 | satisfied；input_kind、reply_to、original_question及多轮翻译回归 |
| user:引用业务状态方案 | 独立状态优先、保留18、损坏不猜测 | satisfied（客户端）；业务Schema、入口测试、配置文档；云端结构化发布未验收 |
| user:文件日志 | 仅文件、可定位、轮转、脱敏 | satisfied；日志测试、独立并发探测及真实日志检查 |
| user:查询最新文档 | 正式 V2 契约和构造场景 | satisfied；官方文档核验、真实咨询探测如下 |

## Validation Matrix

| 维度 | 要求与判据 |
| --- | --- |
| 行为 / Contract | required：普通答案停止、多次追问、问卷、Widget、非法输入与回调取消 |
| 用户 / Workflow | required：main 三类函数及 CLI 调用真实组件，通过边界 Fake 验证提示、输出和请求 |
| 文件系统 / 并发 | required：真实UTF-8日志、轮转/句柄释放、控制台隔离、路径/脱敏/写入失败 |
| 外部 Provider | required：本机配置进行有界文本探测，区分连接、普通答案和真实追问的实际证据 |
| Runtime / Package | required：公共导出和模块启动；新增模块的包包含情况 |
| Docs | required：README 与接口一致、链接存在、示例可解析 |
| Persistence / Migration / Deploy | not_applicable：此次无数据库、存储格式和部署变化 |

## 上一轮基线证据（仅平台标记）

- 初始红灯：`python -B -X utf8 -m unittest discover -s tests -p test_clarification.py` 因尚未导出 `chat_until_complete` 失败，符合新增能力缺失的基线。
- 2026-11-05，`$env:TEMP=(Get-Location).Path; $env:TMP=(Get-Location).Path; python -B -X utf8 -m unittest discover -s tests`：最终 35 项通过，退出码 0。离线验证包含公共函数、三个 main 示例函数、CLI、真实 SSE 解码和请求编码，外部 HTTP 边界为 Fake。
- `python -B -X utf8 -m adp_chat --help`：退出码 0，显示新 `--ask` 语义。
- 独立隔离 Reviewer 首轮实际运行 33 项测试，并发现两处未覆盖问题：交互/启动问题取消追问被吞掉，以及展示 Widget 遮蔽未填问卷。补充失败回归后修复，最终 Reviewer targeted re-review 实际运行 14 项追问测试通过，F1/F2 CLOSED，未发现本地协议能力范围的新阻塞代码问题。Reviewer 同时将真实应用自动追问闭环缺口保留为 BLOCKING / REPORT_ONLY；没有把本地测试视为云端验收。
- 构建使用本机缓存且经 wheel RECORD 逐项验证的 setuptools 84.2.1，没有更改项目依赖。`setuptools.build_meta.build_wheel('.validation/dist')` 成功；wheel 可重新打开、CRC 正常、包含新增 `console.py`，不含真实配置和测试文件。通过独立 venv 的 `python -B -m pip install --no-index --no-deps .validation/dist/adp_chat-0.1.0-py3-none-any.whl` 安装，退出码 0；从项目源码目录外导入安装包并以 Fake HTTP 完成两轮 `15 → 18`，会话延续与 `input_kind` 断言通过。pip 输出沙箱临时目录清理警告，不影响安装和消费验证。
- 所有源文件 AST、README Python 示例 AST、本地 Markdown 链接、配置模板 TOML 解析通过。构建、隔离环境和临时工具目录已清理，未删除用户 IDE 或已有缓存目录。
- 本地源码指纹（SHA-256 前 16 位）：`adp_chat/client.py=c510c5070d400044`、`adp_chat/console.py=c9774143d7d815b1`、`adp_chat/cli.py=8b49c56ca4588fc2`、`tests/test_clarification.py=f0db7bca9ba896fd`；它们不是 Git 提交号。
- 目录未初始化 Git，无分支、提交、PR 或 CI；不进行合并、发布、部署。

## Completion Audit

上游要求已按后续 steering 重读：推荐明确业务状态，文字识别仅兼容，日志仅写文件。反向核对覆盖 `业务/平台/文字来源 → 结果 → 是否等待 → 取答案 → 同会话续答`、三类入口、异常/取消、原始任务语境、日志去向和消费者字段。当前真实文本兼容续答已通过一次正向验证；结构化云端配置/发布、真实附件/问卷/Widget仍未验收，不宣称完整真实业务全流程完成。

## 本轮新鲜证据

- 各新增能力先红后绿：文字漏判、结构化配置/模块缺失、日志模块缺失。独立审查发现的操作答案/可选邀请误判、翻译补答丢失首问、模板名误伤及生成清单漏保护均补充失败回归后修复。
- 最终执行 `$env:TEMP=(Get-Location).Path; $env:TMP=(Get-Location).Path; python -B -X utf8 -m unittest discover -s tests`：**60 项通过，退出码 0**。HTTP/COS边界用Fake，公共函数、编码解码、main/CLI、真实临时日志文件均实际运行。
- 独立隔离 Reviewer 实际运行过41、46、57项测试，另外核验15个反例/相邻正例、多轮翻译、结构化冲突/重复键/错误布尔/尾随文本与结构化CLI；本轮F1–F4 CLOSED。独立日志验证覆盖4线程80条、轮转文件可立即重命名、异常释放、写入失败不输出控制台、不改root handlers、北京时间转换。
- 最终真实探测：虚构报销咨询首轮 `18/text_rule/needs_input=true`，补答后 `18/needs_input=false/input_kind=clarification_answer`，两轮同会话；另一次国内差旅出租车报销的明确咨询返回 `18/needs_input=false`，未调用答案提供器。三次请求无业务提交，库调用控制台为空，诊断文件非空，未含本机密钥或原始问题正文。此前首个真实措辞漏判已加入正例并修复，没有以一次成功掩盖它。
- `Draft202012Validator.check_schema` 和文档JSON示例验证通过；源码及Python示例AST、TOML配置/示例、本地Markdown链接均通过。代码与Schema合法不证明上游语义必然正确。
- 最终wheel由经RECORD逐项验证的本机缓存setuptools 84.2.1离线构建（未升级依赖），包含新增clarification/interaction/logging模块、无真实配置及日志，ZIP可重新打开且CRC正常。独立venv执行 `python -B -m pip install --no-index --no-deps .validation/dist/adp_chat-0.1.0-py3-none-any.whl` 成功（退出码0）；从源码目录外导入安装包，构造SSE完成结构化两轮18、同会话、单次补答、最终停止及文件日志，stdout/stderr均空。
- 相关源码SHA-256前16位：`client.py=3bb0732990408de9`、`interaction.py=5383da29d584f4da`、`logging.py=feb8856cd0fe39c2`、`clarification.py=f78a0b0b75de2aa5`。构建目录、临时工具、venv及无后续用途探测摘要已清理，真实诊断日志保留。源码指纹不是Git提交号。

## 上一轮真实探测与剩余限制

本机网络受限环境最初返回 `chat/network_error`。在获准的外网执行环境使用已有本机 AppKey，未输出凭据，未改变云端应用配置，进行四次文本请求：

1. 虚构天气查询：应用说明仅覆盖 IT、财务、采购，返回 `ReplyMethod=18`，无明确等待标记，自动流程一轮返回。
2. 虚构差旅报销咨询（仅询问流程、不提交操作）：回复询问要了解报销标准、单据要求还是系统操作流程，但返回 `18`、无问卷及 Widget。自动流程没有继续收集答案，说明当前规则无法覆盖这类自然语言追问。
3. 再次差旅报销咨询并观察完整 SSE 的分类、状态和回复方式：返回一段报销说明；过程中只观察到 `0` 和 `18`，未观察到 `15`。
4. 在第 3 次请求的相同会话 ID 下补充“想了解系统操作流程”：请求成功，但应用重新询问属于电脑/账号、报销还是采购。会话 ID 复用已确认，业务上下文及路由延续未验收。

以上探测证明当前真实文本接口能够接收并完成请求，不证明普通/图片/文件的真实业务全流程正确。图片和文件未新增真实上传；实际问卷和 Widget 作答未触发，离线契约验证不能替代它们的联调。

依据 [Agent 高级设置](https://cloud.tencent.com/document/product/1759/126992)（2026-04-16 更新），建议在相关 Agent 核对并发布澄清、上下文与业务JSON输出。客户端已具备业务契约解析，但未读控制台，不能推断当前配置已正确。默认兼容规则仍可能保守漏判，不能把有限样例扩大为任意自然语言正确。目录无Git、提交、PR、CI或合并；本轮未执行云端配置发布或生产数据操作。仅清理任务临时构建物，保留用户IDE与真实诊断日志。

## 后续诊断：460701及问答日志

用户报告 `chat/provider_error` 请求 `0e997594-8a27-469d-aa8b-4527b7c2b954`，要求日志保留完整问题及回答前100字符。实际日志第14行在2026-11-05 14:42:21.844记录 `provider_code=460701`；当日重新查询官方SSE错误码，含义为专属模型超出并发数限制。已确认错误类别，无法从本地得知云端当时并发占用及配额；旧日志没有问题正文，不能从当前main反推并补造历史问题。

本次只改进已知错误提示与问答可观测性，保留 `provider_error` 分类、provider_code及关联ID，不加自动重试，不改变同步请求方式，不修改云端资源。未知数值错误码也在提示中保留；服务端原始Error.Message不直接展示或写入日志。

问答日志复用原有logger。`user_question` 在上传/请求前记录当前轮问题，首问与补答分别保留；`adp_answer` 在成功解析后记录正文预览，配置 `[logging].log_answer_chars` 默认100，0关闭回答正文。按用户授权记录完整问题，但配置密钥和含签名/令牌的URL仍脱敏。先脱敏后截断，避免密钥跨100字符边界泄漏；再JSON转义换行和Unicode行分隔符，避免伪造日志记录。

适用验证：真实历史日志与官方错误码核验；公开chat失败/成功、Unicode长度、长问题不截断、控制台隔离、敏感URL与凭据、配置类型及首问/补答回归。现有文件轮转和模块打包规则未改，无新增依赖、Migration、提交、PR、CI或部署。改进提示及单次成功不等于永久解除云端限额。

- 先执行新增日志回归，按预期因缺少问答记录/配置失败；最终完整命令 `$env:TEMP=(Get-Location).Path; $env:TMP=(Get-Location).Path; python -B -X utf8 -m unittest discover -s tests`：72项通过，退出码0。包括460701原因可读、不重试、流关闭、问题完整、回答100字符、脱敏后截断、Unicode单行转义、IPv6、合法URL内部标点、fragment与编码嵌套签名URL回归。
- 另进行一次无业务操作的真实文本请求，`request_id=5a129f9c-704d-480d-8110-b7336c7b1652` 返回成功、ReplyMethod18；回答原长181字符，文件中预览100字符，问题完整，库调用stdout/stderr为空。这里只确认这个请求成功，不证明并发配额已改变。
- 源码AST、README Python示例、本地Markdown链接及配置模板TOML解析通过，`log_answer_chars=100`已验证。真实日志保留，历史记录未回写，用户main中的问题及已注释的图片/文件调用保持不变。
- 独立复核发现URL合法内部标点导致匹配提前截断的凭据遗漏，以及整query删除会误删无空格接续的中文正文。先补失败回归，再改为完整读取候选、按userinfo/query/fragment参数值脱敏，保留非敏感参数和问题正文。依据 [RFC3986](https://www.rfc-editor.org/rfc/rfc3986#section-2.2) 核对整类内部字符，不逐个增加分隔符例外；公开URL无敏感项时原样保留。
- 最终独立复核实际执行日志专项18项通过，另验证原5个泄漏反例、中文接续、相邻多URL、编码临时ID和凭据标点均通过；F1/F2/F3 CLOSED，结论为NO_FINDINGS_WITHIN_SCOPE。Reviewer未重复云端请求或全套72项，不将作者实测或本地通过写成云端容量恢复。最终源码指纹（SHA-256前16位）：client.py=129a0ada2824d7a8、logging.py=04600c39502b81fb、config.py=a2e0571017b0b32f。本轮没有新增构建目录或遗留临时物，真实日志继续保留。
