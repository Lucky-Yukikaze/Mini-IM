# Mini-IM 面试版项目报告

## 项目一句话

Mini-IM 是一个桌面端即时通讯系统，使用 `Qt 6 + C++` 承载原生 QUIC 连接，用 `Vue 3` 做聊天界面，用 `Python asyncio + aioquic + SQLite` 实现服务端可靠消息、群聊、文件传输和状态恢复。

## 为什么做这个项目

这个项目重点不是做一个简单聊天 Demo，而是验证 IM 系统最核心的能力：消息可达、状态可恢复、操作可重试、传输和业务解耦。旧版 WebSocket 原型很快能跑通聊天，但继续加群聊、文件、撤回、阅后即焚后会把状态逻辑揉在一起，所以重构成 QUIC + Protobuf + 同步事件流。

## 我的设计取舍

客户端分两层：

* Qt 原生层：持有 MsQuic 连接，负责 protobuf、ACK、同步、文件流
* Web UI 层：只通过 QWebChannel 调高层 API，订阅高层事件并刷新界面

服务端按业务拆服务：

* Auth：dev 登录和 session 恢复
* Conversation：群聊、单聊和成员关系
* Message：消息落库和投递事件
* Delivery：已读、撤回、阅后即焚
* File：文件元数据、上传下载、完整性校验
* Sync：按游标补偿同步

这个拆法的核心收益是：UI 不感知传输细节，传输层只负责到达，业务一致性由服务端事件和客户端同步恢复保证。

## 核心链路

登录恢复：

```text
Client Hello(global_cursor, resume_session_id)
-> Server Welcome(session_id, user_id)
-> Client SyncRequest(global_cursor)
-> Server SyncResponse(events, new_global_cursor, has_more)
```

工程落点：

* 客户端入口在 `MiniImSessionManager::sendHello` 和 `handleIncomingEnvelope`
* 服务端入口在 `MiniImQuicProtocol`，收到 hello 后由 `AuthService` 生成 session
* Welcome 后客户端发 `SyncRequest`，后续按 `SyncResponse.has_more` 分页追平
* 前端只收到 bridge 事件，不直接接触 session_id 以外的底层连接细节

消息发送：

```text
send_message
-> Ack
-> messages 落库
-> message_deliveries 投递状态
-> sync_events 写入每个成员的事件流
-> 在线成员实时收到 sync_response
-> 离线成员上线后按 global_cursor 补偿
```

工程落点：

* `MessageService` 处理发送请求，`MessageRepo` 写 messages、message_deliveries 和 sync_events
* 群消息只存一份正文，每个成员的投递状态单独写入 message_deliveries
* 在线用户由 `OnlineHub.fanout_sync_events` 推送，离线用户靠 `SyncService` 补拉
* Web 侧 `session.ts` 按 conversationId 分组存消息，插入时按 seq 排序

文件上传：

```text
FileInit
-> Ack(file_id)
-> 独立 QUIC stream 上传二进制
-> FileFinish
-> sha256 / size 校验
-> 生成 MSG_FILE 消息
-> 同步给会话成员
```

工程落点：

* `FileService` 处理 `FileInit / FileFinish`
* `FileRepo` 保存 file_transfers，记录 direction、received_bytes、version、status、source_file_id
* 二进制数据走独立 QUIC stream，控制面只传文件元数据和完成状态
* 上传完成后 `_append_file_message` 生成 `MSG_FILE`，消息内容是包含 fileId、fileName、fileSize、sha256 的 JSON
* 客户端文件进度按 fileId/version 单调更新，避免旧进度覆盖新进度

> 截图占位：请在这里插入“文件上传后生成文件消息”的截图，面试时用于说明控制面和数据面分离。

## 关键难点和解决方案

状态恢复：

问题：在线推送不能覆盖断线、重连、乱序和离线场景。  
方案：所有业务变化统一写入 `sync_events`，客户端只记 `global_cursor`，重连后补拉事件流；`has_more=true` 时继续分页拉取，避免大量文件进度事件阻塞后续状态。

单聊和群聊已读：

问题：单聊要显示 `未读/已读`，群聊要显示 `x 人未读`，但底层不能为 UI 做两套状态。  
方案：服务端统一用 `conversation_members.last_read_seq` 作为已读真值，`message_read_counters` 作为群聊展示缓存；前端按会话类型展示，单聊把 `unreadCount > 0` 显示为 `未读`，`unreadCount = 0` 显示为 `已读`，群聊继续显示未读人数。

文件传输：

问题：文件数据不能塞进普通消息队列，否则会阻塞聊天控制面。  
方案：`FileInit / FileFinish` 走 protobuf 控制面，文件内容走独立 QUIC stream；完成后才生成 `MSG_FILE` 消息。客户端区分源文件 ID 和传输任务 ID，下载时用源文件 ID。

前端状态及时性：

问题：消息、撤回、已读、文件进度可能乱序到达。  
方案：前端 Pinia store 做统一状态入口，消息批量入库，撤回/焚毁/已读先到时先缓存或合并，文件进度按 `fileId/version` 单调更新。

压测和故障注入：

问题：只测正常路径无法证明 IM 状态可恢复。  
方案：把故障注入放在业务状态层，模拟上传中断、任务过期、文件分段恢复、焚毁扫描重复执行和同步事件堆积；用自动化测试验证幂等、断点续传、`sha256` 校验和 `SyncResponse.has_more` 分页续拉。

## 项目亮点

* 使用 MsQuic 做桌面客户端传输层，Qt 负责原生连接，Web UI 保持纯展示边界
* Protobuf Envelope 统一控制面协议，所有请求都有 request_id 幂等语义
* 用 sync_events 做离线补偿和状态恢复，避免只依赖在线推送
* 文件控制面和数据面分离，支持上传、下载、断点续传和完整性校验
* 群聊不是单聊多播，独立建模 conversation_members、message_deliveries、message_read_counters
* 单聊和群聊共用 receipt 语义，UI 按会话类型展示为 `未读/已读` 或 `x 人未读`
* 阅后即焚落到投递状态机，按接收者已读后 TTL 触发
* 前端做了虚拟列表和状态乱序合并，解决联调中的卡顿和状态不及时
* 压测覆盖 2 MB 大文件恢复、文件断点续传、过期上传接管、阅后即焚批扫描和同步分页续拉

> 截图占位：请在这里插入“群聊 x 人未读 + 撤回/焚毁状态”的截图，面试时用于展示状态同步能力。

## 遇到过的典型问题

连接后闪退：

原因：MsQuic `SEND_COMPLETE` 回调释放上下文时，控制流和文件流上下文不统一。  
修复：统一使用堆上的发送上下文，回调完成后释放。

上传文件后聊天区白屏：

原因：Vue 模板里文件 payload 变量作用域写法错误，文件消息渲染触发运行时异常。  
修复：提前解析 `filePayload`，模板只读稳定字段。

状态不及时：

原因：文件进度事件很多时 sync 分页没有续拉，后续 receipt 和 conversation_updated 滞留。  
修复：客户端处理 `SyncResponse.has_more` 自动继续拉取下一页。

离线撤回后仍看到旧消息：

原因：sync_response 中 recall 先到，message 后入前端 store，撤回操作变成 no-op。  
修复：前端增加 pending 状态，消息后到时合并撤回/焚毁状态。

四个关键 review 修复：

* 同步分页：`SyncResponse.has_more=true` 时继续用 `new_global_cursor` 拉下一页，解决 file_updated 堆积后状态滞留
* 初始状态：Welcome 不再发送空会话和空消息，避免连接成功后 UI 被清空
* 未读总数：`unreadTotal` 从“只初始化”改为随新消息和当前用户 receipt 增减
* 文件进度：从页面局部 ref 迁入 Pinia store，按 `fileId/version` 单调 upsert，重连和组件重建后状态更稳定
* 单聊已读：MessageList 接收 conversationType，单聊显示 `未读/已读`，群聊保留 `x 人未读`

实现细节可以补充：

* 服务端业务状态以 SQLite 表为准，客户端 Pinia 只是展示缓存
* `message_read_counters` 是展示缓存，`conversation_members.last_read_seq` 是已读真值
* `sync_events` 每个用户一条 seq 链，客户端用 global_cursor 单调推进
* 文件流和控制流分开后，大文件上传不会阻塞 receipt、recall、conversation_updated 这类控制事件
* 前端先 flush 消息批处理队列再处理状态更新，解决 message 和 receipt/recall 同一帧乱序的问题

## 测试和验证

自动化测试覆盖：

* 登录与恢复
* 单聊消息
* 群聊、成员管理、已读、撤回
* 多用户 dev 登录
* 文件上传、下载、断点续传
* 阅后即焚
* SQLite schema

测试设计口径：

* 正常路径：覆盖登录、建会话、发消息、收消息、已读、撤回、文件完成、阅后即焚
* 恢复路径：覆盖离线同步、断点续传、过期上传接管、焚毁任务重复扫描
* 异常路径：覆盖未知用户拒绝、文件大小冲突、sha256 不匹配、非法 burn TTL
* 一致性验证：服务端查 SQLite 真值，客户端侧依赖同步事件恢复展示状态

压测说明：

* 文件压测：使用 `2 MB` payload 做分段上传，模拟上传任务过期后同 intent 恢复，完成后校验 size 和 sha256
* 恢复压测：上传/下载覆盖 resume offset，验证断点续传后 sync_events 仍能补偿
* 状态压测：阅后即焚批扫描到期 delivery，生成 `system-burn`，二次扫描验证不重复生成事件
* 同步压测：大量 file_updated 场景依赖 `has_more` 分页续拉，避免后续已读和会话事件滞留
* 执行命令：`python -m unittest server.tests.test_phase5_file_flow server.tests.test_phase6_burn_flow`
* 当前压测口径：以 unittest 场景验证恢复能力和状态一致性，不是独立 QPS 压测脚本

> 截图占位：请在这里插入“压测测试输出”截图，面试时可展示 25 个服务端测试通过和大文件恢复用例。

常用验证命令：

```powershell
python -m unittest discover server\tests
Set-Location .\web
npm run build
Set-Location ..
$env:QT_DIR="<你的Qt安装目录>\6.11.0\msvc2022_64"
git clone https://github.com/microsoft/vcpkg .\thirdparty_install\vcpkg
.\thirdparty_install\vcpkg\bootstrap-vcpkg.bat
.\thirdparty_install\vcpkg\vcpkg.exe install msquic protobuf --triplet x64-windows
cmake -S .\client -B .\build\client_qt611 -DCMAKE_TOOLCHAIN_FILE="$PWD\thirdparty_install\vcpkg\scripts\buildsystems\vcpkg.cmake" -DVCPKG_TARGET_TRIPLET=x64-windows -DCMAKE_PREFIX_PATH="$env:QT_DIR"
cmake --build .\build\client_qt611 --config Release --target mini_im_client
```

## 可以怎么讲给面试官

我会先强调这个项目的目标是“可靠 IM 内核”，不是聊天 UI Demo。然后按三条线讲：

1. 传输线：Qt 持有 MsQuic，控制面 Envelope，文件走独立 stream。
2. 状态线：服务端所有业务变化写入 sync_events，客户端用 global_cursor 恢复。
3. 业务线：群聊、已读、撤回、文件、阅后即焚都统一落在消息、投递和同步模型上。

最后补充几个真实联调问题，例如 SEND_COMPLETE 释放错误、文件消息白屏、sync 分页不续拉。这些问题能说明项目不是只写了静态代码，而是经过了端到端联调。

压测部分可以这样讲：我没有只做吞吐数字，而是围绕 IM 的恢复能力设计压测，重点验证“中断后能不能接着传、状态事件堆积后能不能追平、重复扫描会不会重复发事件、文件完成后校验是否严格”。这个口径比单纯 QPS 更贴近 IM 项目的可靠性目标。

被追问“一致性怎么保证”时可以这样答：服务端以 SQLite 中的业务表为真值，以 `sync_events` 作为可重放事件流；客户端状态只是展示缓存，断线或乱序后靠 `global_cursor` 补拉，前端再按 messageId、fileId/version、lastReadSeq 做幂等合并。

被追问“为什么不用 WebSocket”时可以这样答：这个项目想验证桌面端原生传输和文件流能力，QUIC 允许控制面和文件数据面拆开，文件大流量不会和聊天控制事件抢同一条逻辑通道。

## 后续可扩展方向

* dev 用户升级为正式账号系统
* SQLite 替换为 PostgreSQL/MySQL，热状态接 Redis
* 增加多设备在线策略和端到端设备列表
* 增加文件秒传、缩略图、过期清理
* 增加更完整的 UI 自动化联调
