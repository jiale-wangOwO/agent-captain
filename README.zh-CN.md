# PRON Hub

<p align="center">
  <img src="assets/pron-hub-logo.png" alt="PRON Hub" width="640">
</p>

**Personal Remote Ops & Nodes Hub**：通过一个 Codex 主控对话，协调你选定的本机和远程 Linux 工作 agent。

[English](README.md) · [SSH 传输接口约定](docs/ssh-transport.md) · [Codex 后台维护](docs/codex-maintenance.md) · [安全说明](SECURITY.md)

PRON Hub 是单用户、本地优先的连接服务。每个工作 agent 使用一个真实终端，你在其中按平常方式启动 Codex，再选择哪些终端交给主控管理；你始终可以直接进入同一个终端对话。

**主控模型就是当前 Codex 对话里的模型**，担当你的 CEO：调查进展、传达任务、汇总证据和待决事项。用户保留最终决策权。Hub 不额外运行一个管理模型，不向工作 agent 注入 CEO 角色，也不会自动唤醒空闲的 Codex 对话。

## 能做什么

- 在 UI 配置本机或 SSH 机器，打开项目目录下的工作终端。
- 人与主控共用同一个终端，直接对话和管理操作可以并存。
- 只允许主控访问显式激活的终端，不在启动时扫描所有机器或历史。
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
| 主控 Codex | 支持插件和 stdio MCP 的本地 Codex 客户端 |

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

## 连接工作 agent

1. 选择或配置机器，点击「新终端」。可以浏览目录、输入该机器上的绝对路径，或留空进入用户主目录后自行 `cd`。
2. 在真实终端中运行 `codex`、`codex-switch` 或自己的启动器。供应商选择、登录、密钥、OTP、审批均由用户处理。
3. 继续旧对话时，按需查找历史并自行执行显示的 `codex resume <session-id>`。先退出原客户端；开一个新终端不等于转移原会话的控制权。
4. 连接好后激活终端。激活不启动模型；未激活的终端仍可由你直接使用。
5. 按下一节安装本地插件，在主控 Codex 对话中选择它，再交代管理目标。

可以对主控说：

> 作为我的 CEO，查看已激活的 PPT 和服务器 agent，汇总实际进展、证据和需要我决定的事项。先不要下发任务。

> 把这个修复任务传达给已激活的服务器 agent，之后汇总结果和验证情况。

一个本地 Hub 只有一张共享激活名单；多个主控对话看到同样的范围，不提供租户或主控之间的隔离。

## 接入 Codex 插件

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

## MCP 工具

| 工具 | 用途 |
|---|---|
| `list_active_agents` | 只读激活登记表 |
| `get_agent` | 核对指定终端的连接与前台状态 |
| `read_agent` | 分页读取该终端的准确会话输出 |
| `send_agent_message` | 传达普通用户任务 |
| `send_terminal_input` | 发送明确授权的原始终端输入 |
| `create_agent` | 在 UI 已连接机器上打开工作终端 |
| `get_delivery` | 核对持久化送达状态 |
| `poll_events` | 增量读取激活 agent 事件 |

服务不自动审批、不自行扩大激活范围，也不自动开启主控的新一轮推理。

## SSH 和 tunnel

远程接口始终是 OpenSSH。TunnelTerm 等外部工具可以提供 loopback SSH 端口或 `ProxyCommand`；Hub 不导入它的代码，也不管理它的登录和窗口生命周期。

接入需要验证**常驻显示连接与短 SSH 查询可以并发**，不能只测一次 banner 或 `ssh host true`。保留主机密钥校验。详见 [SSH 传输接口约定](docs/ssh-transport.md)。

## 本地数据与兼容

服务仅绑定 loopback。浏览器修改要求匹配 Host/Origin 和 HttpOnly SameSite cookie。MCP 用私有本地连接文件认证，拒绝浏览器 Origin，不通过代理或重定向转出 token。

机器、激活范围和送达记录保存在 `.runtime/hub.sqlite`。`config.example.json` 与可选、被 Git 忽略的 `config.local.json` 用于初始化；已有保存配置优先。

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
npm run test:terminal
npm run test:ui
python scripts/check_publication.py
```

真实 PTY/远程联调为 opt-in，普通单元测试不启用。只用可丢弃会话执行联调。回归覆盖激活、精确会话读取、幂等送达、草稿保护、终端渲染、认证与生命周期；不代表所有供应商或 tunnel 实现都已认证。

开发见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

## 许可证

项目使用 MIT 许可证，见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。OpenAI、Codex 及第三方服务名称属于各自所有者；PRON Hub 是独立项目。
