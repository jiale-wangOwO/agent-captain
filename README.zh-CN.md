<p align="center">
  <img src="assets/brand/agentcaptain-mark.png" alt="AgentCaptain 标志" width="120">
</p>

# AgentCaptain · 队长

**一个主控，统筹本机与远程 Agent。**

*One captain for agents across your machines.*

通过一个主控对话，统一派发任务、跟进进度、协调协作和审查交付。你定目标，队长带队，最终决策仍由你掌握。

[![CI](https://github.com/jiale-wangOwO/agent-captain/actions/workflows/ci.yml/badge.svg)](https://github.com/jiale-wangOwO/agent-captain/actions/workflows/ci.yml)
[![MIT 许可证](https://img.shields.io/badge/license-MIT-64748b)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab)](docs/guides/operator-guide.zh-CN.md#环境要求)
[![Node.js 22+](https://img.shields.io/badge/Node.js-22%2B-43853d)](docs/guides/operator-guide.zh-CN.md#环境要求)

[English](README.md) · [快速开始](#快速开始) · [文档导航](docs/index.md) · [参与贡献](CONTRIBUTING.md) · [安全说明](SECURITY.md)

![AgentCaptain 工作台，使用虚构演示数据](docs/images/workspace-demo.jpg)

*截图中的机器、会话和工作动态均为演示数据，不是实际运行记录，也不代表真实项目已完成验证。*

[本机复现离线演示 →](docs/guides/visual-demo.md)

## 少切窗口，把精力放回目标

工作 Agent 分散在不同机器和项目里，逐个打开终端追问，很快就会成为另一份工作。AgentCaptain 为它们增加统一的管理层，同时保留你熟悉的真实终端。

| 你想要…… | AgentCaptain 帮你…… |
|---|---|
| 了解多台机器的进展 | 在一个主控对话里汇总相关工作、阻塞和待决事项。 |
| 把目标落到协作执行 | 派发给负责的 worker，补充约束、跟进纠偏并审阅交付证据。 |
| 深入某个项目 | 直接打开原共享终端，像平常一样与工作 Agent 对话。 |

**Captain 就是本机原生 Codex 管理对话里的模型**，启动时准备好角色、工具和独立身份。实际工程工作由 worker 完成；主控不懂某个领域时先问负责的 worker，做好协调、跟进和汇总。**人类用户保留最终决定权。** 不需要额外运行一个托管模型服务。

## 一个清楚的工作台

- **本机 + SSH。** 在 UI 配置机器、选择项目目录、打开终端；外部 tunnel 工具也可以提供标准 SSH 接口。
- **连接即纳入管理。** 已登记、未关闭的共享工作会话自动加入工作列表，支持命名、搜索、筛选和置顶，无需再逐个激活。
- **读真实会话，不猜屏幕。** 准确关联 Codex Session，分页读取长回复；终端画面里的报告尾部不会被当成完整报告。
- **人的输入优先。** 人与主控共用真实终端，保护未发送草稿；送达结果不明确时保留原操作身份核对，不盲目重发。
- **恢复边界明确。** 重连只接回原持久终端，不静默新建替代终端、恢复模型或重放任务。

## 快速开始

主控电脑需要 **Python 3.11+**、**Node.js 22+**、npm，以及配置好的本机 **Codex CLI**。远程工作机还需 Linux、OpenSSH、Python 3、tmux 和该用户的 Codex。见[完整环境要求](docs/guides/operator-guide.zh-CN.md#环境要求)。

```sh
git clone https://github.com/jiale-wangOwO/agent-captain.git agent-captain
cd agent-captain
npm ci
python app.py serve
```

打开 **[localhost:8766](http://127.0.0.1:8766/)**。仓库目前仍为 **private**，克隆需要访问权限。全新配置只有本机，不会自行发现或连接你的服务器。

Windows 可双击 `run_agentcaptain.cmd`，在当前用户后台启动服务并打开工作台；也可显式运行 `python service.py start`、`python service.py status`。不会安装管理员服务或开机自启。

1. **接入工作。** 按需添加 SSH 别名，选择项目目录、打开终端，自行运行 `codex`、`codex-switch` 或常用启动器。
2. **启动队长。** 点击 **「启动管理会话」**，Hub 为原生本机 Codex 对话准备角色、身份和工具。这条路径无需手动安装插件；登录、信任与权限提示仍由你确认。
3. **交代目标。** 已连接工作会话自动加入。默认不允许主控新建工作；需要时，在设置中明确选择允许新建的机器和项目目录。

先试一次只读汇总：

> 看一下已连接 worker 的最新真实会话输出，汇总进度、证据、阻塞和需要我决定的事。先不要发新任务。

再派发一个明确目标：

> 让负责 API 的 worker 调查这个回归失败，修复并给出测试命令、结果和剩余风险。证据不够时，继续向它追问。

[完整安装与操作指南 →](docs/guides/operator-guide.zh-CN.md)

## 使用前要知道

AgentCaptain 是**单用户、本地优先、面向 Codex** 的工具。服务保持在 loopback，不是多租户云控制平面，也不是适配任意 CLI 的自动化框架。管理会话初始化和桌面交接仍属**实验性**，不代表所有 Codex 版本、供应商和客户端组合都已通过实测。

Hub 不会自动唤醒空闲模型、代替人工审批，也不保证跨休眠、关机和断网保持连接；不能无缝接管其他位置已运行的任意终端。**已提交**或 worker **自报完成**，都不等于已验证完成。

Hub 创建的队长有独立工具身份，**新建工作终端**受明确的机器与项目目录限制。[已有对话插件接入](docs/guides/operator-guide.zh-CN.md#可选用旧版插件接入已有-codex-对话)使用独立的共享服务凭据兼容通道，**不执行**上述独立管理身份的新建目录限制。两者都不是操作系统沙箱；启用前请阅读[安全模型](SECURITY.md)。

## 按需深入

| 想了解什么 | 阅读入口 |
|---|---|
| 安装、日常操作、已有 Codex 对话接入 | [操作指南](docs/guides/operator-guide.zh-CN.md) |
| 会话命名、真实终端、历史与重连 | [Agent 工作台](docs/agent-workbench.md) |
| 管理、派发、验收、权限和生命周期 | [管理会话产品约定](docs/management-session-product.md) |
| 服务掉线、SSH 路由、Codex 后台包 | [恢复与维护导航](docs/index.md#recover-and-maintain) |
| 测试、提交安全检查、贡献流程 | [贡献说明](CONTRIBUTING.md) |

## 参与贡献

欢迎有仓库访问权限的协作者提交可复现的问题、测试和小范围 PR。请使用虚构测试数据，不要附上真实 token、连接文件、会话日志或个人配置。见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [SECURITY.md](SECURITY.md)。

项目采用 MIT 许可证，见 [LICENSE](LICENSE) 和[第三方说明](THIRD_PARTY_NOTICES.md)。AgentCaptain 是独立项目；OpenAI、Codex 及其他第三方名称属于各自所有者。
