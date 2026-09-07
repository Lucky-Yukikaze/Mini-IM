# Mini-IM

Mini-IM 是基于 Qt 6/C++、Vue 3/TypeScript 和 Python 的桌面即时通讯项目。
Qt 持有原生连接并承载页面，Vue 负责界面交互，Python 服务端处理业务，SQLite 保存数据。

QUIC = 在 UDP 上提供加密连接与可靠数据流的传输协议；客户端使用 MsQuic，服务端使用 aioquic。
Protobuf = 按 `proto/` 中的结构定义编码和解码应用消息；
QWebChannel = Vue 页面异步调用 Qt 对象方法并接收 Qt 信号的接口。

单聊、群聊、已读、撤回、文件和阅后即焚均有功能入口；
可用范围、当前任务及验证结果见 [重构执行记录](docs/refactoring-progress.md#当前阶段)。

## 阅读入口

| 需要做什么 | 阅读文档 | 文档职责 |
| --- | --- | --- |
| 安装并运行项目、执行检查、排查问题 | [开发指南](docs/development.md) | 依赖、命令、配置、数据位置与脚本使用边界 |
| 理解代码或参与开发 | [设计说明](REPORT.md) · [开发约束](AGENTS.md) | 组件与数据路径；架构、编码与协作规则 |
| 查看进度或追溯重构依据 | [重构执行记录](docs/refactoring-progress.md) · [历史基线评估](docs/architecture-review-2026-09-07.md) | 当前状态与验收证据；修复前的评估依据 |

首次运行从开发指南开始；参与修改前阅读设计说明和开发约束。
[PLANS.md](PLANS.md) 保留为计划入口，指向执行记录。

## 源码导航

| 目录 | 内容 |
| --- | --- |
| [client/](client/) · [web/](web/) | Qt 客户端与 Vue 页面 |
| [server/](server/) · [proto/](proto/) | Python 服务端与跨端协议定义 |
| [tools/](tools/) · [docs/](docs/) | 开发脚本与项目文档 |

开发登录使用 `dev-token:<用户名>`；目标用户须至少登录过一次。
当前运行方式与适用边界见开发指南，文档维护规则见开发约束。
