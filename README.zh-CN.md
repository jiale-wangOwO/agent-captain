# PRON Hub

<p align="center">
  <img src="assets/pron-hub-logo.png" alt="PRON Hub" width="640">
</p>

**Personal Remote Ops & Nodes Hub**：通过一个 Codex 主控对话，协调你选定的本机和远程 Linux 工作 agent。

[English](README.md) · [管理会话实现与边界](docs/management-session-product.md) · [SSH 传输接口约定](docs/ssh-transport.md) · [Codex 后台维护](docs/codex-maintenance.md) · [安全说明](SECURITY.md)

PRON Hub 是单用户、本地优先的连接服务。**0.5 版本新增管理会话 MVP**：从 Hub 创建一条真实的本机 Codex 对话，自动准备管理角色、工具和明确的工作范围。该功能仍属实验性，单元测试不等于实际 Codex 版本、供应商或桌面交接已获得认证。

**主控模型就是这条原生 Codex 对话里的模型**，担当你的 CEO：调查进展、传达任务、汇总证据和待决事项。用户保留最终决策权。Hub 不另建模型服务，不向工作 agent 注入 CEO 角色，也不会自动唤醒空闲对话。每个工作 agent 仍使用真实终端，你可以像平常一样启动 Codex、直接参与对话。

## 能做什么

- 在 UI 配置本机或 SSH 机器，打开项目目录下的工作终端。
- 创建并恢复一条默认管理会话，使用本次原生启动的准确 Session ID，不按最新文件猜测。
- 准备专用管理工作区、持久角色指导和独立范围的 MCP 连接，受管会话不需要手动安装插件。
- 人与主控共用同一个终端，直接对话和管理操作可以并存。
- 新管理会话使用服务端强制的工作会话与项目目录授权，不在启动时扫描所有机器或历史。
- 读取准确关联的 Codex 会话输出，长消息分页，工具调用与输出可选。
- 记录幂等送达状态，保护人的输入草稿，不盲目重发不确定消息。
- 让终端生命周期独立于网页和 Python UI 服务。
- 沿用标准 SSH 别名，可由外部工具把 tunnel 转成 SSH。

项目以 **Codex 工作流**为中心，不是适配任意 CLI 的通用自动化系统，也不是多用户控制平面或托管云服务。它不会无缝接管其他位置已经运行的任意终端。

## 环境要求

| 位置 | 要求 |
|---|---|
| 主控电脑 | Python 3.11+、Node.js 22+、npm、现代浏览器 |
| 本机工作 agent | 已安装 Codex CLI，并完成自己的登录和供应商配置 |
| 远程工作 agent | Linux、OpenSSH、Python 3、tmux，以及该用户的 Codex |
| Hub 创建的管理者 | 已配置的本机 Codex CLI，支持 `exec --json`、精确 Session `resume` 和 stdio MCP |
| 可选的旧对话插件 | 支持插件和 stdio MCP 的本地 Codex 客户端 |

Python 服务仅用标准库。通过 npm 安装终端依赖，浏览器资源由本机提供，不依赖 CDN。若 `node-pty` 无可用预编译包，可能需要原生编译工具链。

## 快速开始

```sh
git clone https://github.com/jiale-wangOwO/pron-hub.git
cd pron-hub
npm ci
python app.py serve
```

打开 [http://127.0.0.1:8766/](http://127.0.0.1:8766/)。仓库仍为 private 时，克隆需要 GitHub 访问权限。

Windows 可双击 `run_pron.cmd`：在当前用户后台启动服务并打开 UI。`run_ceo.cmd` 保留为兼容入口。也可显式运行：

```sh
python service.py start
python service.py status
```

不会安装管理员服务或开机自启。**全新配置只有本机**；远程机器由你在 UI 中添加，例如 SSH 别名 `dev-workstation`，凭据仍由已有 SSH 配置管理。

## 启动管理会话

1. 按下一节配置和连接工作 agent。首次范围选择只列已有登记且已激活的对象，不自动发现历史；也允许先启动零范围管理者。
2. 点击首页 **「启动管理会话」**。使用默认名称或自己命名，明确勾选允许管理的工作会话。默认不允许主控新建工作；需要时，在可选设置中浏览已连接机器，为具体项目目录授权。
3. Hub 准备自己的管理工作区与 MCP 连接，执行一次**只读**的原生 Codex 初始化。从自己启动的原生 JSON 事件获得准确 Session ID，仅让模型调用 `get_controller_context` 核验管理身份与范围；首轮不读 worker 历史、不发送业务任务、不创建工作 agent。
4. 在真实管理终端中交流。**「可对话」**需要准确身份、已认证工具握手和可用终端；PID、欢迎文字或 PTY 连通不够。登录、信任和权限确认仍由你完成，不改写既有模型供应商或认证配置。
5. 此后使用 **「继续管理」**恢复原会话，通过 **「管理范围」**调整授权。重复点击、刷新或网络结果不确定时，保留原操作与原身份，不强行创建第二个 CEO。

这条流程**不需要手动安装下方旧版插件**，受管会话已经配置了独立范围的工具。启动前先确认本机 Codex 和日常账号/供应商配置可用。初始化失败或结果未知时，查看明确的阶段、错误与原身份；Hub 不为隐藏问题删除锁、fork 会话或重启共享 Codex 后台。

默认管理工作区位于源码仓库外：Windows 使用 `%LOCALAPPDATA%/PRONHub/management-workspaces/<controller-id>`；Linux/macOS 使用 `$XDG_STATE_HOME/pron-hub/management-workspaces/<controller-id>`，缺省为 `~/.local/state/...`。这样进入管理者时不必连带信任整个 Hub 仓库。Codex 仍可能要求你确认这个新目录的信任，Hub 不代替你批准。

首次初始化沿用你的模型与 reasoning 设置，可能需要数分钟；页面会持续显示原启动阶段，不因耗时而换模型或重复新建。初始化最长等待 10 分钟，超时后保留已取得的准确身份，先核查/恢复原会话。

恢复会保留初次核验过的 Codex 程序入口，不因换了启动后台的 PowerShell/PATH 而悄悄改用另一个旧 CLI。原安装入口被移除时，显示环境错误并停止恢复，不擅自降级或改变供应商设置。

可以对管理者说：

> 查看已授权的 PPT 和服务器 agent，汇总实际进展、证据和需要我决定的事项。先不要下发任务。

> 把这个修复任务传达给已授权的服务器 agent，之后汇总结果和验证情况。

### 在 Codex 桌面端继续（实验性）

**「在 Codex 中继续」**要求你明确确认管理者空闲、没有未发送草稿。Hub 核对自己拥有的终端、释放这个输入入口，并提供 `codex://threads/<exact-session-id>` 链接打开同一条已保存对话，不新建同名对话，也不自动输入 `/app` 或其他命令。

状态为 **`desktop_unverified`（桌面接管未核验）**，不是已经证明的无缝交接。打开链接不能证明桌面端取得会话，或重新加载了工具、项目信任、模型和供应商设置；需在桌面端核对。桌面入口使用期间，Hub 不继续写入。返回 Hub 前先关闭另一入口，再点击 **「继续管理」**，按准确原 Session 尝试恢复，不重新初始化或重发业务任务；占用或配置冲突会明确报出。

## 连接工作 agent

1. 选择或配置机器，点击「新终端」。可以浏览目录、输入该机器上的绝对路径，或留空进入用户主目录后自行 `cd`。
2. 在真实终端中运行 `codex`、`codex-switch` 或自己的启动器。供应商选择、登录、密钥、OTP、审批均由用户处理。
3. 继续旧对话时，按需查找历史并自行执行显示的 `codex resume <session-id>`。先退出原客户端；开一个新终端不等于转移原会话的控制权。
4. 连接好后激活终端，并在管理者的范围中明确勾选。激活不启动模型，也不代表所有独立管理者都获得该对象；未激活终端仍可由你直接使用。

管理者和工作 agent 使用分离的记录与终端服务，管理者不会登记成自己的 worker。服务端工具拒绝管理者到管理者的操作和递归创建管理者。这限制 PRON Hub 的接口，不提供操作系统级沙箱隔离。

## 可选：用旧版插件接入已有 Codex 对话

这是已有主控对话的兼容方式，不是新管理会话的必备步骤。**旧版插件凭据仍访问共享的全局激活名单**：多个旧主控看到同样的已激活工作对象，没有独立管理者隔离。需要独立身份和授权范围时，优先从 Hub 创建管理会话。`get_controller_context` 仅供独立范围凭据使用，旧版凭据不可调用。

先在仓库根目录生成**本机配置后的**插件市场：

```sh
python scripts/configure_plugin.py
```

脚本会打印 `marketplace_root` 和安装命令。注册打印出来的根目录，不是源码仓库：

```sh
codex plugin marketplace add "<printed marketplace_root>"
codex plugin add pron-hub@pron-hub-local
```

生成器只创建文件：不安装插件、不修改 Codex settings、不读取服务 token、不重启服务。MCP 启动命令使用运行生成器的 Python，以及生成目录中 bridge 的绝对路径。

**安装后必须保留生成目录**，默认在 `.runtime/plugin-marketplace/`。更新、移动仓库或更换 Python 时，选择新目录生成并重新注册安装：

```sh
python scripts/configure_plugin.py --output .runtime/plugin-marketplace-v2
```

已有输出不会被覆盖。仓库的 `pron-hub-source` 市场是未配置的 **skill-only** 模板，直接安装不能完整连接 Hub；不要用它代替生成的 `pron-hub-local`。这是本地集成，不是官方公共插件目录上架，也不向云端客户端暴露服务。

若已有对话没有新工具，在新对话中选择插件；只有必要时才重载 Codex 应用。安装不会替换已经运行的对话工具上下文。命令用法参见 [官方 CLI 说明](https://learn.chatgpt.com/docs/cli/reference)。

## 会话输出不是终端截图

终端画面用来核对前台程序，**看得到报告尾部不代表读到了完整报告**。「会话输出」面板独立读取准确关联的 Codex rollout。

关联依据限定在所选终端的进程树、打开的日志或显式 resume ID；不按目录里的“最新会话”猜测。如果无法确认，可以在同一工作终端中自行运行 `/status`，把 Session ID 填入「关联会话」。建立关联不向工作 agent 输入内容。

通过 MCP 读取时：

- 跟随 `next_cursor` 读取旧记录或长消息剩余分片；`after_cursor` 读取新追加输出。
- 检查 `content_complete`、`history_complete`、`has_more`、`read_issues`；部分分页不是全部历史。
- 完整工具调用与输出需要 `include_tools`；默认最近信号只是有限摘要。
- `live_session_verified=false` 仅表示明确选择或上次核验的关联，不证明它仍是 TUI 中当前会话。`/new`、`/resume` 后缺少直接证据时需要重新关联。
- 已断连或关闭终端的记录是历史证据，不证明 agent 在线或任务完成。

长正文分片而非静默截断。未写完或损坏的记录会明确标记；不会暴露隐藏 analysis/reasoning。

## 输入保护与生命周期

普通任务经 bracketed paste 和 Enter 发到同一个前台 Codex，不当作 shell 命令；shell、登录、审批和可识别的启动菜单会阻止任务输入。原始按键只用于用户明确授权的终端操作。

人的草稿优先：`human_busy` 不允许主控覆盖。`submitted` 只证明字节写入，**不证明任务已被接受或完成**。保留原 `request_key`，用 `get_delivery` 核对不确定送达，不用新键盲重发。

UI 支持复制粘贴、本地回滚、目录选择、刷新画面和明确确认的关闭。鼠标滚轮不会转成 agent 输入。粘贴不额外按 Enter，但剪贴板文本本身的换行仍可能由程序处理。

关闭网页、切换终端、取消激活或关闭 Python Hub，不停止独立终端 broker。broker 或主控电脑停止时，本机 PTY 会结束；远程 Hub 自有 tmux 可在 SSH 显示断线后重新连接。重新连接不自动 resume Codex、不重发任务。

**「关闭终端」不同**：确认后结束该终端的 shell/agent，远程也结束对应自有 tmux。不关闭其他或外部会话；结果不确定时不会显示成成功。

管理终端与工作终端分离。**「收起显示」**或关闭网页不发送草稿、不结束进程。**「结束管理会话」**经明确确认后结束管理终端进程并停用其工具凭据，不停止 worker；已保存的原生身份和历史保留，继续时明确恢复原会话。

## MCP 工具

| 工具 | 用途 |
|---|---|
| `get_controller_context` | 核验已认证管理身份与范围，仅供独立范围管理者 |
| `list_active_agents` | 只读当前身份范围内的已激活工作对象；旧版插件仍使用全局名单 |
| `get_agent` | 核对指定终端的连接与前台状态 |
| `read_agent` | 分页读取该终端的准确会话输出 |
| `send_agent_message` | 传达普通用户任务 |
| `send_terminal_input` | 发送明确授权的原始终端输入 |
| `create_agent` | 在明确授权的已连接机器与项目目录中创建工作终端，归属于发起管理者 |
| `get_delivery` | 核对持久化送达状态 |
| `poll_events` | 增量读取激活 agent 事件 |

读取、写入、送达查询和事件都校验已认证管理身份的范围。撤权会取消尚未提交的排队写入；MVP 中一个 worker 最多归一个独立范围管理者。服务不自动审批、不自行扩大范围、不递归创建管理者，也不自动开启主控的新一轮推理。旧版凭据的更广权限边界如上所述。

## SSH 和 tunnel

远程接口始终是 OpenSSH。TunnelTerm 等外部工具可以提供 loopback SSH 端口或 `ProxyCommand`；Hub 不导入它的代码，也不管理它的登录和窗口生命周期。

接入需要验证**常驻显示连接与短 SSH 查询可以并发**，不能只测一次 banner 或 `ssh host true`。保留主机密钥校验。详见 [SSH 传输接口约定](docs/ssh-transport.md)。

## 本地数据与兼容

服务仅绑定 loopback。浏览器修改要求匹配 Host/Origin 和 HttpOnly SameSite cookie。MCP 用私有本地连接文件认证，拒绝浏览器 Origin，不通过代理或重定向转出 token。

机器、激活范围、送达记录、管理身份、授权范围和操作键保存在 `.runtime/hub.sqlite`。工具连接文件位于私有运行目录，生产管理工作区使用上面说明的用户私有目录；显式指定开发/测试 runtime 时，工作区也保持在该命名空间。每位管理者使用独立凭据，原始 token 不进入提示词、URL 或命令行参数。`config.example.json` 与可选、被 Git 忽略的 `config.local.json` 用于初始化；已有保存配置优先。

网页后台与 MCP 共用 `.runtime/service.json`，显式连接文件覆盖仍保留。私有 `.runtime/worker-terminal-reference.json` 只记录已有工作终端服务描述文件的物理路径，不复制 token；默认终端创建流程核验成功后独占登记，只读状态查询不生成它。这避免 Windows 打包应用与普通 PowerShell 的本地状态命名空间差异造成假断线。已绑定服务不可达、引用损坏或丢失时，明确阻止新建第二个终端服务，需人工核查修复；后台网页服务重启不会终止已有工作终端。

已运行的旧桥接若仍读取旧位置，可以在私有 `.runtime/service-compatibility-references.json` 明确登记已有标准 Hub 描述文件。服务重启仅同步同一端点、结构合法的 Hub 文件，让原客户端继续使用轮换后的服务凭据；不自动发现目标、不复制账号认证，也不改动其他端口或非 Hub 文件。新安装使用统一描述文件。

每个管理工作区拥有自己的 `AGENTS.md` 和 `.codex/config.toml`。Hub 启动/恢复时还明确提供该作用域的工具连接，不修改用户全局配置。桌面客户端是否加载相同项目配置与信任，属于实验性交接的核验事项，不作默认保证。Hub 的接口授权不限制 Codex 其他原生工具、其他插件或用户的 SSH/系统权限。

改名保留旧协议和存储标识：`ceo_hub`、`ceo-hub`、`ceohub-`、`CEO_HUB_*`、`%LOCALAPPDATA%/CEOHub/`、`~/.local/state/ceo-hub/`。不要为改名删除已安装旧插件缓存或重写正在工作的终端 ID。

UI 按键不写入送达数据库，但终端输出仍可能包含你主动打印的秘密。主控提交的任务和送达记录也是本机私有数据，不应提交 `.runtime/`、连接文件、认证文件或个人配置。项目不构成多租户安全边界，不要暴露到公网。

## Codex 后台维护

CLI 更新不保证独立托管共享后台同步更新。可选 Linux 助手默认只读：

```sh
python3 scripts/codex_sync_daemon.py --check
python3 scripts/codex_sync_daemon.py --sync --timeout 60
```

在**工作机器本身**运行，使用当前用户原本信任的 CLI。任何已加载会话，包括空闲会话，都阻止同步。同步目标是当前 CLI，不另行选择最新版本。使用 `--sync` 前先读 [维护说明与验证边界](docs/codex-maintenance.md)。

## 开发与测试

```sh
python -m unittest discover -s tests -v
node --check web/ui.js
node --check web/controller.js
npm run test:terminal
npm run test:ui
python scripts/check_publication.py
```

真实 PTY/远程联调为 opt-in，普通单元测试不启用。只用可丢弃会话执行联调。测试覆盖授权隔离、原生初始化事件、稳定启动/恢复键、未知结果、管理者独立生命周期、桌面接管未核验 UI，以及激活、精确会话读取、幂等送达、草稿保护、终端渲染和认证。它们不证明所有 Codex 版本、供应商、tunnel 或桌面工具重载已通过实测。0.5 管理会话 MVP 在版本相关实机验证完成前仍属实验性。

开发见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

## 许可证

项目使用 MIT 许可证，见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。OpenAI、Codex 及第三方服务名称属于各自所有者；PRON Hub 是独立项目。
