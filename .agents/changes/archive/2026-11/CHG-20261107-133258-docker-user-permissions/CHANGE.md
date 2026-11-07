---
schema: coding-change/v1
id: CHG-20261107-133258-docker-user-permissions
title: 有Docker权限的普通账户直接运行三个部署脚本
level: L3
status: done
owner: dingyuwen77
branch: main
created: 2026-11-07T13:32:58+08:00
updated: 2026-11-07T14:11:51+08:00
completion_gate: required
depends_on: []
affected_areas:
  - Docker离线部署权限
  - 原会话与目录保护
affected_paths:
  - scripts/build_image.ps1
  - tests/test_docker_package.py
  - scripts/run.sh
  - scripts/stop.sh
  - scripts/upgrade.sh
  - tests/deploy_smoke.py
  - README.md
  - docs/adp-chat/04_Docker部署与运维.md
contracts:
  - run.sh VERSION
  - stop.sh
  - upgrade.sh
data_changes: []
---

# 变更摘要

用户服务器ynnd账户已有Docker权限，但没有sudo bash权限，明确要求修改run.sh、stop.sh、upgrade.sh使日常部署无需宿主管理员。只改本地项目，不连接服务器、不改sudoers/Docker组或生产数据，不提交推送。保留既有dirty工作区和所有历史归档。Canonical已fetch并锁定ffd7ac58a52c7561a99bb31580e970debaa2744d；base main/026ed66cd040e3f020d872b224e14c743026180e。旧发布Change已归档，新需求新建单元，不改写旧记录。

# 目标与方案

- AC1：有Docker访问权且release可写的普通Linux账户可直接bash run/stop/upgrade，无sudo或EUID0硬性要求。
- AC2：只创建缺失目录；普通账户的新目录由本人管理，容器非root应用仍UID/GID10001，通过绑定文件/目录所需的补充组访问，不改已有数据属主/正文。
- AC3：状态文件由执行账户原子写入，无宿主chown；配置只读固定挂载、状态与数据权限错误失败关闭；原会话与切换/锁/归属/恢复机制保持。
- AC4：真实非root进程+Docker权限运行全部入口证明新建/重启/升级/失败恢复及原session字节不变；原root方式回归仍兼容。
- AC5：同步README/运维现有Owner，99项原回归与新验证适用通过、独立只读Review、Ready和本地归档按用户既有授权收尾。

方案比较：仅删EUID检查会在install/chown失败；改应用为调用者UID影响已有10001数据约定；选择保留应用身份，普通用户创建自有组可写专用目录，添加受限挂载组，状态单文件直挂/宿主自有0640。root入口保留现有10001新目录策略。无业务/API/依赖/会话Schema变化，无自动迁移已有数据。rootful Docker实测；rootless Docker用户映射不冒充已验证。

- AC6：用户确认服务器Ubuntu24.04.4/aarch64，并明确要求打包默认linux/arm64，只指定版本号即可；保留显式Platform覆盖及版本保护。

# 需求追溯

稳定来源为本聊天本轮原文：“我运行这些脚本不需要管理员权限吧”“ynnd这个账户是由docker权限的”“也修改run.sh stop.sh upgrade.sh脚本”。以下Source标识docker-user-permissions#AC1–5对应本轮归一的目标，原会话保护/保证正确/本地归档继承同一聊天此前明确要求；不引用本Change自身作为上游来源。

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 普通Docker账户运行三个入口 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/docker-user-permissions#AC1 | satisfied | scripts三入口已实现；真实UID/GID1000且有Docker权限进程全链exit0，无sudo（nonroot-green.log） |
| R2 | 缺失目录/状态无需宿主管理员 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/docker-user-permissions#AC2 | satisfied | 实际新目录owner1000/主组2770、state owner1000/0640断言通过；root旧目录策略和调用全链亦exit0 |
| R3 | 保留应用非root与原会话保护 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/docker-user-permissions#AC3 | satisfied | 应用UID/GID10001、仅挂载补充组、只读固定config/state；会话字节、错误保护、恢复均在两种真实调用中通过 |
| R4 | 真实非root验收及root兼容 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/docker-user-permissions#AC4 | satisfied | 正确root限制Red（8ef78a）→非root全链Green（da4ef5）；root兼容Green（9dcc68）；99 tests零skip（987821） |
| R5 | 文档/Review/本地归档 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/docker-user-permissions#AC5 | satisfied | README/运维Owner已同步，独立/root/user_permissions_review PASS；Ready与最终本地归档记录为收尾证据 |
| R6 | 默认ARM64打包只需版本号 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/docker-user-permissions#AC6 | satisfied | 两代PS默认ARM64/显式AMD64 Green；真实v0.0.5 ARM64 build/save/归档核验及模拟服务smoke exit0 |

# 验证矩阵

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Contract | required | 三个sh入口、失败码及原状态/会话保护；脚本解析 |
| 集成 / Persistence / Runtime Dependency | required | rootful Docker真实UID1000进程、专用目录、补充组、宿主状态归属 |
| 用户 / Workflow Acceptance | required | 不使用sudo运行run/stop/upgrade、升级重启及端口变更 |
| 跨组件 Golden Path | required | 两个正式版本产物到健康容器、会话一致、失败恢复 |
| 外部依赖 Probe | not_applicable | 不改外部协议，不连接服务器或ADP |
| Build / Package / Runtime | required | AMD64正式镜像验证普通账户/root部署链；默认ARM64真实build/save、tar/config架构和SHA256、模拟启动smoke；目标ARM64服务器原生未验收 |
| Docs / Governance / Other | required | 现有两文档同步、独立Review及Ready、本地归档 |

# 风险、兼容与回滚

用户已有Docker权限为前置条件，不修改系统授权。只针对受控bind路径添加应用补充组；不全局放开777或递归改已有数据。普通账户需要release可写，旧root专属部署锁/目录权限不自动接管。既有session内容/身份/路径与Schema不改；不以重建会话解决权限错误。回退脚本保留同一镜像与持久数据；旧root脚本不能承担新的普通用户入口。实现与验收仅本机隔离fixture，正式config/runtime/logs不用于测试。

# 完成审计

- [x] upstream_re_read：重读用户最新Docker权限及三个脚本修改要求。
- [x] change_coverage：逐AC核对实现与证据。
- [x] reverse_audit：从宿主普通用户→Docker→容器身份→数据→状态→恢复反查。
- [x] unresolved_cleared：无未解决阻塞项。

# 完成证据与状态

当前实现、验收与独立Review闭环。独立只读Reviewer已完成复核，证据及边界见下方完成记录。不提交、不推送、不连接服务器；完成后按已授权本地归档。

## 实际变更与验收

- run.sh去掉EUID0硬性门禁，改核验Docker访问和release写权限；普通用户仅创建缺失目录为本人/主组2770(setgid)，root创建10001/750保持。不递归chown/chmod已有数据。新增append_bind_groups仅用真实绑定配置、状态和数据目录的属组；应用主UID/GID仍10001。
- config固定单文件只读，state改独立单文件只读绑定/deploy/state.json，不要求开放宿主私有release父目录；宿主先拒绝state链接/非普通文件避免bind掩盖类型。元数据plan/manifest/cache只读root容器capdropALL+DAC_OVERRIDE可读私有上传归档和配置，无网络、无可写数据或Docker socket；实际数据预检/应用为10001。
- record_success由当前宿主用户写0640并原子替换，不再chown。stop/upgrade继续共用唯一实现并说明直接调用。README/现有运维Owner全部日常命令改为直接bash；config保留运行账户所有权/640，不要求root chown。明确release可写、旧root专属锁权限交接和未验证rootless/userns映射的范围。
- 镜像内生产代码/HTTP/ADP/会话Schema/配置字段/依赖未改；权限修改无需重打已有同架构镜像。后续用户明确要求打包脚本默认ARM64，按AC6另行实施；已有镜像无需重打，只需更新三个服务器脚本。没有远程服务器、sudoers或Docker组变更。

## 新鲜证据

1. Source身份/干净main/worktree核对后git fetch+ff-only，HEAD=origin/main=ffd7ac58a52c7561a99bb31580e970debaa2744d（9193b8）。目标仍main/base026ed66cd040e3f020d872b224e14c743026180e，保留所有既有dirty/staged历史工作。
2. `tests/deploy_smoke.py --first dist/v0.0.2 --second dist/v0.0.3 --cli-binary dist/.deploy-tools/docker --unprivileged` 修改前在真实UID1000进程（Docker socket访问组、HOME自有）报“请使用sudo/root”而失败（8ef78a,exit1）；原始dist/.validation/nonroot-red.log。此前CLI挂载尚未就绪/误选WSL提示程序及HOME不匹配的失败不计Red。使用DockerDesktop实际Linux CLI，未安装新的运行依赖。
3. 相同公开入口修改后完整exit0（da4ef5），dist/.validation/nonroot-green.log，fixture ac48803a40b24cc589191a5feb3d6702。真实deploy进程UID/GID1000，不运行sudo；私有release700、config640、目录owner1000/2770、state owner1000、应用10001补充gid1000断言通过。运行、停止、升版、同版本改端口与state/真实绑定一致、无env重复run、重启、原session字节/CID、锁/归属、包/身份/路径/缺失/坏session、state000不可读/坏JSON/悬空链接、旧cache引用保护/成功后清理与归档保留、新CMD失败恢复旧版本、绝对路径/空格均PASS。
4. 不带unprivileged开关的同一真实Linux全链exit0（9dcc68），dist/.validation/root-compat.log，原root入口及10001/750新目录策略兼容；所有测试容器/fixture/故障镜像清理。非root第一个Green尝试最后在测试辅助函数拼写错误处失败，已修正fixture并重新完整通过，不作为产品完成证据。
5. `.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v`，Python3.14.7，99 tests、0 failed、0 skipped，10.272s，exit0（987821）；dist/.validation/nonroot-unittest.log。CLI/PowerShell/API/原会话逻辑回归保持。
6. 三sh UTF8无BOM/LF、内嵌Python编译、测试AST、两文档相对链接与无sudo直接运行说明通过（1d4044）；git diff --check exit0。当前run.sh SHA256=d3a66902cbb5f675c9b9f9543b74a4fee40a0ae01558498dfee383ea3cd0b27a；stop=39c9d0fb74698a5c375e3a0dc2c8ed478c8177c1346d6ee1074cb7f5125b3a6b；upgrade=0f66cbe326dc64b8a15ecf6b66190bc32715ae207b452eafb7fc7d65534060c0。
7. 只读精确delta基线：dist/.validation/permissions-before-run.sh由逆本轮hunks重建，并严格SHA匹配上轮已经独立审查的c8833f62405192bef0e2f4f3d42a4880c2573b9e6b57bb547c1ee17176430d19；证明端口/state旧防护未被算作本轮新增或丢失。非root测试driver的真实读取使用应用身份访问私有fixture，不通过chown或世界可读权限伪造普通用户能力。
8. 正式数据只计算hash未读取正文：config.toml=e7e85db6339f2419f5e90d8ce462891de9e92aafecd0b30a850cdbf1b2a31d3e；runtime/session.json=c3f9fb9a59f28a8ba3ae5e9ec733837d40fa68c158fd001fad6cd70092e92451；logs/adp_chat.log=fea40a9ef88ee4246deeb4f2f990f7f1fdf37f29a48f8bfff22efa9352613053，均与本次133258任务开工baseline一致。日志在两任务间用户可能有新增，本次不以更早a84...当基线。

## 独立Review及完成审计

Reviewer /root/user_permissions_review，原生新建fork_turns=none、未实现/写入、不再委派。实际核对canonical、精确前后脚本SHA/delta、普通账户权限/单文件状态bind/非root应用与补充组/只读元数据边界、全部nonroot-green与root-compat原始日志和测试断言、99项单测与两正式文档。最终结论PASS / NO_FINDINGS_WITHIN_SCOPE，AC1–4和文档一致性证据充分，无阻塞finding。Parent重新检查实际代码与底层输出，依最新用户“已有Docker权限，也修改三个脚本”逐AC及宿主→Docker→容器→会话→状态→恢复反查，未以setup控制器root冒充普通进程。

范围限定本机Docker29.6.2 Linux/amd64 rootful；服务器、rootless/userns-remap、ARM64及真实ADP未实机，不冒充已部署或业务可用。没有CI/workflow更改，无push授权，未运行远程CI；本地沿用CI正式99项命令。没有commit/push/PR/merge/生产数据迁移/Issue关闭。用户已经明确授权完成Change主Agent本地归档；完成态仅表示本地实施/验证/Review已闭环，不表示Git集成或发布。归档前后Ready与cleanup记录随后按实际结果补充。

## 最新上游澄清

服务器实报uname -m=aarch64、Ubuntu24.04.4 LTS。当前本机dist/v0.0.4清单为linux/amd64，服务器脚本正确要求linux-arm64.tar，非权限失败。用户随后明确原文：“打包默认就是linux/arm64就可以，不需要我再指定了”。因此在同一未归档单元增加AC6及打包入口/测试/现有文档影响，已有普通用户/root测试和Review对未变的权限域仍有效；默认目标新证据与独立增量复审完成后再Ready。生产镜像内容/依赖与业务Schema不改，ARM64构建产物和模拟运行不冒充服务器原生实机验证。

## ARM64默认目标增量验证

用户明确“打包默认就是linux/arm64就可以，不需要我再指定了”。build_image.ps1仅将Platform默认值改为linux/arm64，保留两个ValidateSet值和显式覆盖。不覆盖旧v0.0.4 AMD64产物；使用v0.0.5新版本。

- TDD：dist/.validation/arm64-default-red.log，两个真实PS入口均因实际默认linux/amd64 != 要求linux/arm64失败（d2f808）；初次外层日志筛选误搜文件名导致未识别实际正确断言失败，后按原始平台断言重新核实，未将其当产品错误。最终dist/.validation/arm64-default-green.log 8tests OK（0b43ce，4.355s），包括PS5.1/7仅-Version默认产物及显式AMD64覆盖；替身Docker记录真正build--platform参数返回inspect架构，不从脚本文本反推期望。
- 完整回归：dist/.validation/final-unittest.log，100tests、0失败/0跳过、8.894s、exit0；权限域运行代码仍d3a66902...未改，已有非root/root真实完整Green与Review保持有效，无依据全量重跑该长链。
- 正式打包：仅执行scripts/build_image.ps1 -Version v0.0.5，未传Platform，真实Docker构建/save成功（dfd231），dist/.validation/build-arm64-v0.0.5.log；产物dist/v0.0.5/adp_channel_api-v0.0.5-linux-arm64.tar及manifest.json/SHA256SUMS。镜像ID sha256:7edf086fab85f1ce940ead853dcdf8054482baa2ef8cefacd7f0320b5d9725e9，config digest sha256:68f6dd13ac20b0428ab0b01815968f82de0eb866cedca54f66201585b9156284。
- 实际重新读取tar内配置确认os=linux、architecture=arm64、版本labelv0.0.5，配置内容digest及两项SHA256SUMS全部匹配；docker image inspect实际linux/arm64。最终builder SHA256 b78627d84ead8aeaecd797dd1b31f79398130d8b6b2ffadb45764b5c937c140c；test_docker_package.py SHA256 f4e27d16268249347b4ef84e634aca70bf2dae9d110140b35b5ab4965f480b3a。
- ARM64服务smoke：tests/docker_smoke.py --image adp_channel_api:v0.0.5，exit0（02e0bf），dist/.validation/arm64-image-smoke.log。正式安装/UID10001/只读配置/可写数据、两次health200/非法输入422/原session字节、真实HTTP→service→session→受控SSE→HTTP200全部PASS，fixture/容器清理，无真实ADP请求。此为本机amd64 DockerDesktop的ARM64模拟执行，目标Ubuntu24.04.4/aarch64服务器原生仍由用户上传后运行验收。
- 正式Owner文档已改默认ARM64且只指定版本号，示例与实际生成v0.0.5一致；保留本机AMD64部署harness所需显式AMD64测试包说明，避免将ARM镜像交给检查daemon架构的AMD测试。架构不匹配仅重新打包新版本，不改文件名掩盖清单。

当前全部AC具备实现与测试证据，独立Reviewer对新增AC6的增量复审已PASS / NO_FINDINGS_WITHIN_SCOPE；全部实现与验证完成，按Ready门禁及用户授权本地归档。Git、生产数据和服务器操作授权不变。

## 最终独立复审与完成核对

/root/user_permissions_review已完成新增默认ARM64范围的只读增量复审，PASS / NO_FINDINGS_WITHIN_SCOPE。独立读builder全文、测试和文档，实际核对b78627d8/f4e27d16及权限脚本d3a66902未变；读取正确Red→8项Green→100项全回归和完整真实build/save日志，独立重新计算v0.0.5校验和及tar配置OS/架构/版本/User，确认ARM64模拟服务smoke及清理PASS。没有新finding；原权限AC1–4 PASS继续有效。

Parent重新按最新用户“不需sudo”“已有Docker权限”“三个脚本”“默认ARM64仅版本号”核对AC1–6，反查四个公开入口→真实宿主用户→Docker权限→应用身份→持久数据/状态→失败恢复，及仅Version→平台参数→真实ARM64产物→模拟HTTP/会话路径。正式文档、required层Evidence及数据保护均闭环；目标原生服务器与外部ADP/远程CI未执行的边界保留，不做部署完成声明。只读复审及载体修正没有改变已验收的运行代码，不重复长链测试。

## 最终本地归档

主Agent按同聊天明确授权本地移动完成：.agents/changes/archive/2026-11/CHG-20261107-133258-docker-user-permissions/CHANGE.md，原active目录已不存在，归档文档已读回且状态done；归档前Ready exit0。未提交、推送、PR、合并或部署；归档仅代表本地已验证修改完成。

最终收尾核实：归档后Ready exit0；active为空；专用dist/.deploy-tools/Linux CLI已删除，测试容器/fixture已清理，验证日志、精确旧脚本和ARM64 v0.0.5交付包保留供追溯。已刷新project-context，正式config/session/logs三个hash与本轮开工相同；AST/文档链接/git diff --check均通过。仍main/026ed66cd040e3f020d872b224e14c743026180e，未提交/推送/合并。用户自行报告重新上传新版run.sh后成功，本Agent未连接服务器或独立核对服务器hash/health，不据此新增原生服务器验收声明。
