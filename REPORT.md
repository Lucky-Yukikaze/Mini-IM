# Mini-IM 设计说明

返回 [项目入口](README.md)。本文解释组件职责、主要数据路径和项目展示口径。
它不维护功能完成表；当前结果见 [重构执行记录](docs/refactoring-progress.md)，
必须遵守的目标约束见 [AGENTS.md](AGENTS.md)。

## 组件与职责

| 组件 | 职责与源码入口 |
| --- | --- |
| 桌面客户端 | [窗口宿主](client/ui/mainwindow.cpp) 承载页面；[Qt 会话管理](client/core/session/sessionmanager.cpp) 处理协议、登录、同步和文件任务，[原生连接](client/core/quic/connection.cpp) 管理 MsQuic 连接与流收发；[下载落盘组件](client/core/file/downloadsink.cpp) 负责校验与目标替换，[上传流组件](client/core/file/uploadstream.cpp) 随发送完成回调分批读取文件。 |
| 页面与接口 | [Bridge](client/bridge/imbridge.h) = Qt 向页面提供业务操作和事件的接口；[页面调用封装](web/src/api/bridge.ts) 转发操作并订阅事件，[Pinia 状态](web/src/store/session.ts) 保存界面展示数据。 |
| 服务端与存储 | [监听入口](server/quic/endpoint.py) 配置接收端口，[QUIC 入口](server/quic/server.py) 分派请求；[业务服务](server/services/) 校验业务；[仓储](server/storage/repo/) = 执行业务数据读写的代码；[SQLite 层](server/storage/sqlite/) 管理连接、表结构及升级。 |

[对象映射](client/core/model/eventmapper.cpp) 把协议字段转换为页面业务对象；
[原生状态存储](client/core/sync/statestore.cpp) 保存消息、会话、状态事件与同步位置；
[消息发送队列](client/core/message/outbox.cpp) 保存发送意图、确认状态和失败原因；
[文件任务存储](client/core/file/taskstore.cpp) 保存文件路径、稳定请求、校验信息及任务状态。
[连接恢复组件](client/core/quic/recovery.cpp) 管理重连等待和登录超时；
原生连接组件管理连接和流的生命周期、发送缓冲及接收暂停，向会话类交付控制数据和文件流事件。
会话类判断文件何时可落盘，再通知连接组件继续接收；登录、同步及文件任务调度仍由会话类协调。
拆分进度与验收缺口见执行记录。
历史 [Python 客户端脚本](client/core/quic/quic_client_worker.py) 未被当前 CMake 客户端目标引用，
原生运行路径以 [CMake 定义](client/CMakeLists.txt) 和 Qt 会话管理代码为准；桌面程序与原生联调驱动共用同一原生库。

## 消息与同步

控制消息使用 `Envelope`，即含路由信息和一种业务消息的协议对象。
每条控制消息前置 4 字节大端长度；接收端累积数据后拆出完整消息。
定义见 [envelope.proto](proto/envelope.proto)，服务端实现见 [codec.py](server/protocol/codec.py)。

发送消息的路径是：页面操作 → Qt 保存发送意图 → 原生连接发送 → 服务端业务校验 → 数据库写入 → 应用确认及事件推送。
事务 = 一组数据库写入共同提交或共同回滚；消息正文、用户投递记录和对应同步事件应在同一事务中保存。
ACK = 服务端返回的应用处理结果；成功 ACK 不能代表其他用户已经收到或读过消息。

幂等 = 重复执行同一写入意图，不增加重复业务实体或效果。
`request_id` 标识请求，`client_msg_id` 标识用户的发送意图；重试须复用原有标识。
`event_id` 标识一次同步事件，在同一用户的在线推送和历史重放中保持一致。

同步游标 = 已完整应用并保存的连续事件位置。
[SyncEvent.global_seq](proto/sync.proto) 表示事件在单用户全局同步流中的位置，
`conversation_seq` 表示消息在会话内的位置；请求序号、事件位置和消息位置分别使用。

当前原生存储的处理路径是：接收事件 → 合并业务状态并记录事件标识 →
推进连续位置 → 共同提交 → 向页面发送业务事件。
登录后从本地状态生成 `initialStateLoaded`，再从保存的位置补拉；
`syncProgress` 向页面传递展示用同步进度。持久化与恢复的验收结果见执行记录。

QWebChannel 的调用结果通过完成回调返回；连接方法的回调只表示请求是否被接受，
登录状态通过 `connectionChanged` 传递。发送消息的成功回调表示意图已保存到本地，
页面据此清空草稿；`messageSendsChanged` 更新待确认及失败列表，`retryMessage` 复用失败意图。
Qt 在同步追平后重发未确认消息，成功 ACK 到达后清空该意图保存的正文；
撤回和焚毁事件也会清理对应发送副本。
网络断开或控制流中断时，Qt 在旧连接关闭后重新登录；会话失效时清除旧会话标识，保留本地消息和同步位置。
主动断开会停止重连。恢复参数、重试规则及实际验证范围分别见开发指南和执行记录。

## 文件与消息状态

文件控制使用 [FileInit / FileFinish / FileUpdated](proto/file.proto)，文件内容使用独立 QUIC 流。
上传完成需由服务端核对实际文件大小和 SHA-256；SHA-256 = 根据文件内容计算的 256 位摘要，用于核对内容是否一致。
上传完成状态与对应文件聊天消息共同提交，避免中间失败导致任务完成但会话中没有消息。

下载时，服务端提供预期大小和摘要，Qt 先写临时文件；
收满并校验后替换目标文件，再通过 `FileFinish` 提交实际字节数和摘要。
发送队列接受数据、流结束、目标文件校验完成分别是不同处理步骤。
服务端下载按未确认缓冲量调度文件读取，Qt 接收按处理进度继续；上限与验证见执行记录。
协议扩展后需同时更新服务端和客户端；具体重建命令见开发指南。

文件任务在网络发送前保存到本地，重启和重新登录后继续原意图。
上传以服务端已保存位置续传，下载以本地临时文件实际长度续传；
完成确认单独保存并重试，已有目标文件在补发下载确认前再次核对。
页面可重试失败任务或取消本机任务；取消通过释放连接停止当前传输，其他任务随后恢复。
当前文件调度仍由会话类协调，服务端取消状态和磁盘故障下的数据协调仍需完善。

群聊使用独立成员关系，已读位置以 `conversation_members.last_read_seq` 为准；
消息未读人数保存为可重算的展示数据。撤回和阅后即焚通过同步事件传播，
已生效状态必须抵抗旧消息重放；正文清理须覆盖业务表、同步副本和客户端缓存。

## 展示与验证口径

演示时说明实际走过的客户端与服务端路径，并给出执行记录中的日期和覆盖范围。
业务测试、原生组件测试和真实网络交互分别记录；打包成功与类型检查分别记录。
独立文件流不能单独证明聊天延迟不受影响，文件恢复用例的输入大小也不能证明并发吞吐能力。

历史缺陷和测量保留在 [基线评估](docs/architecture-review-2026-09-07.md)；
后续修复不会改写当时结果。安装、运行、排查和测试命令统一见 [开发指南](docs/development.md)。
