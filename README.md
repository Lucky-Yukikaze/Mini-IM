# Mini-IM

Mini-IM 是一个基于 `Qt 6 + C++ + QWebEngine + MsQuic + Protobuf` 的桌面 IM 项目。核心目标是把传输层、业务语义和 Web UI 边界拆清楚：Qt 原生层负责 QUIC 连接和 protobuf 编解码，Web UI 只负责展示和交互，服务端负责会话、消息、文件、同步和状态恢复。

> 截图占位：请在这里插入“主聊天界面”截图，建议包含左侧会话列表、中间聊天区、右侧详情栏和调试抽屉入口。

## 功能状态

已完成：

* QUIC 连接、`Hello / Welcome / Heartbeat`、会话恢复
* 单聊消息发送、ACK、服务端落库、幂等去重、增量同步
* 群聊、建群、加群、退群、拉人、移除成员、改群名
* 已读回执、单聊 `未读/已读`、群消息 `x 人未读`、消息撤回
* 文件上传、文件下载、独立 QUIC 文件流、断点续传、完整性校验
* 文件消息卡片、源文件 ID 展示、传输任务列表
* 阅后即焚，支持按已读后 TTL 焚毁并清理消息正文
* 现代聊天软件布局：会话栏、聊天区、详情栏、调试抽屉
* 前端消息虚拟列表、状态乱序合并、同步分页续拉
* 故障注入与稳定性压测：文件断点续传、过期上传接管、大文件恢复、阅后即焚批扫描

当前不引入正式账号系统。联调时用户输入用户名，客户端生成 `dev-token:<用户名>`，服务端在首次连接后写入用户表；拉人、加群和单聊会校验用户是否登录过。

> 截图占位：请在这里插入“群聊成员管理”截图，建议展示成员列表、邀请、移除、退群和改名入口。

> 截图占位：请在这里插入“文件消息与传输列表”截图，建议展示文件卡片、源文件 ID、填入下载、传输任务 ID。

## 技术栈

客户端：

* `Qt 6.11.0`
* `C++`
* `QWebEngine`
* `QWebChannel`
* `MsQuic`
* `protobuf`

Web UI：

* `Vue 3`
* `TypeScript`
* `Vite`
* `Pinia`
* `naive-ui`

服务端：

* `Python`
* `asyncio`
* `aioquic`
* `SQLite`
* `protobuf`

## 架构

```text
Qt Client
├─ MainWindow / QWebEngine
├─ QWebChannel Bridge
└─ MiniImSessionManager
   ├─ MsQuic 连接管理
   ├─ Envelope 编解码
   ├─ 消息/回执/撤回/同步
   └─ 文件上传/下载流

Web UI
├─ ConversationList
├─ MessageList
├─ MessageComposer
├─ ConversationDialog
├─ DebugDrawer
└─ Pinia Session Store

Python Server
├─ QUIC Gateway
├─ Auth Service
├─ Conversation Service
├─ Message Service
├─ Delivery Service
├─ File Service
├─ Sync Service
└─ SQLite Repo
```

关键约束：

* Web UI 不直接访问 QUIC
* Web UI 不处理 protobuf 字节流
* 控制面统一走 `Envelope`
* 文件二进制走独立 QUIC stream
* 写操作使用 `request_id / client_msg_id / client_file_id` 做幂等
* 状态恢复依赖 `sync_events` 和 `global_cursor`

## 目录结构

```text
proto/       Protobuf 协议定义
client/      Qt + C++ 桌面客户端
web/         Vue 3 Web UI
server/      Python QUIC 服务端
tools/       工具脚本
PLANS.md     阶段计划和联调口径
AGENTS.md    系统设计与工程约束
```

## 运行方式

启动服务端：

```powershell
# 在项目根目录执行
python .\server\main.py
```

构建 Web UI：

```powershell
Set-Location .\web
npm run build
Set-Location ..
```

首次配置 Qt 客户端：

```powershell
# 在项目根目录执行；QT_DIR 指向你的 Qt msvc 目录
$env:QT_DIR="<你的Qt安装目录>\6.11.0\msvc2022_64"

# thirdparty_install 不提交仓库，新环境首次需要安装 C++ 依赖
git clone https://github.com/microsoft/vcpkg .\thirdparty_install\vcpkg
.\thirdparty_install\vcpkg\bootstrap-vcpkg.bat
.\thirdparty_install\vcpkg\vcpkg.exe install msquic protobuf --triplet x64-windows

cmake -S .\client -B .\build\client_qt611 -DCMAKE_TOOLCHAIN_FILE="$PWD\thirdparty_install\vcpkg\scripts\buildsystems\vcpkg.cmake" -DVCPKG_TARGET_TRIPLET=x64-windows -DCMAKE_PREFIX_PATH="$env:QT_DIR"
```

构建并启动 Qt 客户端：

```powershell
cmake --build .\build\client_qt611 --config Release --target mini_im_client
& "$env:QT_DIR\bin\windeployqt.exe" --release --compiler-runtime --dir .\build\client_qt611\Release .\build\client_qt611\Release\mini_im_client.exe
.\build\client_qt611\Release\mini_im_client.exe
```

联调入口：

1. 打开客户端调试抽屉
2. 输入服务端地址
3. 输入用户名
4. 点击连接
5. 使用已连接过的用户名创建单聊或邀请入群

## 测试

服务端回归：

```powershell
python -m unittest discover server\tests
```

压测/稳定性相关用例：

```powershell
python -m unittest server.tests.test_phase5_file_flow server.tests.test_phase6_burn_flow
```

说明：当前压测没有独立脚本，压测场景写在 unittest 中，便于和功能回归一起跑。

前端构建：

```powershell
Set-Location .\web
npm run build
Set-Location ..
```

客户端构建：

```powershell
cmake -S .\client -B .\build\client_qt611 -DCMAKE_TOOLCHAIN_FILE="$PWD\thirdparty_install\vcpkg\scripts\buildsystems\vcpkg.cmake" -DVCPKG_TARGET_TRIPLET=x64-windows -DCMAKE_PREFIX_PATH="$env:QT_DIR"
cmake --build .\build\client_qt611 --config Release --target mini_im_client
```

当前已覆盖：

* 登录与恢复
* 单聊消息
* 群聊、已读、撤回
* 多用户 dev 登录
* 文件上传、下载、断点续传
* 阅后即焚
* SQLite schema

测试设计：

* 登录恢复测试：验证 `Hello / Welcome / SyncRequest` 能建立用户身份并按游标恢复事件
* 消息同步测试：验证消息落库、ACK、同步事件写入和离线补偿
* 群聊测试：验证建群、加群、退群、成员变更、群消息未读数和撤回事件
* 多用户测试：验证 `dev-token:<用户名>` 登录后，拉人和单聊只允许已存在用户
* 文件测试：验证 `FileInit / FileFinish`、断点续传、下载方向、完整性校验和文件消息生成
* 阅后即焚测试：验证已读后 TTL、生效扫描、重复扫描去重、内容清理和功能开关
* Schema 测试：验证 SQLite 核心表和索引可初始化

> 截图占位：请在这里插入“全量测试通过”截图，建议展示 `Ran 25 tests ... OK`。

压测与稳定性场景：

* 大文件上传恢复：自动化用 `2 MB` payload 分两段上传，中途把上传任务改成过期状态，再用同一个 intent 恢复并完成校验
* 文件断点续传：上传/下载都覆盖 `resume_offset`，校验最终 `file_size` 和 `sha256`
* 僵尸任务回收：过期上传任务会标记为 `failed_stale`，新 `FileInit` 可接管同一 intent
* 阅后即焚批扫描：通过批量扫描到期 `burn_at_ms` 生成 `system-burn` 同步事件，并验证二次扫描不会重复生成
* 同步堆积恢复：客户端支持 `SyncResponse.has_more` 分页续拉，避免大量 `file_updated` 阻塞后续 receipt 和会话事件
* 对应用例：`server/tests/test_phase5_file_flow.py`、`server/tests/test_phase6_burn_flow.py`

## 关键实现点

协议与事件：

* 控制面统一使用 `Envelope`，通过 `oneof body` 承载 `hello / send_message / receipt / recall / file_init / sync_request` 等请求
* 控制流帧格式是 `4 byte big-endian length prefix + Envelope protobuf`
* `seq` 用于同步游标推进，`event_id` 用于事件去重
* 普通请求用 `request_id` 做请求级幂等，消息用 `client_msg_id` 做实体级幂等，文件用 `client_file_id` 表示发送意图
* `SyncEvent` 是补偿同步外壳，消息、撤回、已读、会话更新、文件状态都通过同一条事件流恢复

服务端分层：

* `server/quic/server.py`：QUIC 网关，负责 Envelope 收发、在线 fanout、文件 stream 分发
* `server/services/*`：业务入口，负责参数校验、调用 repo、组装 ack 和同步事件
* `server/storage/repo/*`：SQLite 读写封装，负责幂等、状态更新、sync_events 写入
* `server/storage/sqlite/schema.sql`：核心表结构，包括 users、sessions、conversations、conversation_members、messages、message_deliveries、message_read_counters、file_transfers、sync_events

消息同步：

* 服务端每个业务事件写入 `sync_events`
* 客户端用 `global_cursor` 拉取增量事件
* `SyncResponse.has_more` 会继续分页拉取
* 前端对消息、撤回、焚毁、已读做乱序合并
* 在线用户通过 `OnlineHub.fanout_sync_events` 实时收到同步事件，离线用户下次连接后从 `global_cursor` 补拉

客户端实现：

* `MiniImSessionManager` 持有 MsQuic connection 和 control stream，Web UI 不直接接触连接
* `sendEnvelope` 负责控制消息编码和发送，`handleIncomingEnvelope` 负责按 body 类型分发
* 发送缓冲区统一使用 `StreamSendContext`，在 MsQuic `SEND_COMPLETE` 回调释放
* 文件上传时，`FileInit` 成功后保存 pending upload，再开启独立 QUIC stream 发送文件头和内容
* 文件下载时，服务端主动开单向 stream，下发 `MINIIMFILE1 <file_id>\n` 文件头，客户端按 file_id 写入 pending download
* `m_seen_event_ids` 做客户端事件去重，`m_latest_file_versions` 保证文件进度按 version 单调更新

文件传输：

* `FileInit / FileFinish` 走控制面
* 文件内容走独立 QUIC stream
* 上传完成后生成 `MSG_FILE` 消息
* 下载使用源文件 ID，下载任务会生成自己的传输任务 ID
* 客户端按 `fileId/version` 单调更新文件进度

已读与未读：

* 服务端真值是 `conversation_members.last_read_seq`
* 展示缓存是 `message_read_counters`
* 前端 `unreadTotal` 和单条消息 `unreadCount` 随 receipt 更新
* 单聊里自己发出的消息显示 `未读/已读`，群聊里自己发出的消息显示 `x 人未读`
* 对方进入会话后客户端发送 receipt，服务端推进 `last_read_seq`，前端收到 receipt 后把单聊标签从 `未读` 切到 `已读`

前端状态：

* `web/src/api/bridge.ts` 只封装 QWebChannel 调用和事件订阅
* `web/src/store/session.ts` 是唯一业务状态入口，保存连接、会话、消息、文件进度、已读进度
* 消息列表用轻量虚拟列表渲染，只渲染当前视口附近消息
* `messagePushed` 会先进入 `requestAnimationFrame` 批量队列，遇到 receipt、recall、fileProgress 时会先 flush 队列再处理状态
* 撤回/焚毁早于消息到达时写入 pending 状态，消息后到时合并
* 文件进度按 `fileId/version` upsert，避免旧进度覆盖新进度

压测设计：

* 故障注入点放在业务状态层，例如手动回退 `updated_at_ms`、修改 `status`、分段写入文件内容
* 压测不绕过业务入口，仍通过 `FileInit / append_file_chunk / FileFinish / SyncRequest` 验证完整链路
* 稳定性重点看三件事：幂等是否生效、同步事件是否可补偿、文件完整性是否通过

## 联调排查

查看日志：

```text
build/client_qt611/Release/mini_im_client.log
```

推荐按这条链路定位状态问题：

```text
服务端是否写入 sync_events
-> 在线 hub 是否 fanout
-> Qt 是否收到 sync_response / file_updated
-> Qt 是否推进 global_cursor
-> Web bridge 是否收到事件
-> Pinia store 是否 upsert
-> 组件是否从 store 派生展示
```

文件问题优先看：

* 上传源文件 ID：聊天文件卡片里的 `源文件 ID`
* 下载任务 ID：右侧文件栏里的 `传输任务 ID`
* 下载保存路径：传目录时客户端会补默认文件名
* 完整性条件：`transferred_bytes == file_size` 且 `sha256` 一致

状态同步排查：

| 现象 | 优先检查 | 当前处理 |
|------|----------|----------|
| 文件上传后状态卡住 | `SyncResponse.has_more` 是否继续拉取 | 客户端已按 `new_global_cursor` 自动续拉 |
| 连接后 UI 空白 | Welcome 是否发了空 initial state | Welcome 只更新用户身份和游标，不再清空会话消息 |
| 单聊不显示已读 | 当前会话类型是否传给 MessageList | 单聊显示 `未读/已读`，群聊显示 `x 人未读` |
| `x 人未读` 不刷新 | receipt 是否到达前端 store | receipt 会先 flush 待入库消息，再更新 `unreadCount` |
| 调试抽屉未读数不变 | `unreadTotal` 是否只初始化一次 | 新消息累加，当前用户已读递减 |
| 文件栏进度丢失 | 文件进度是否只存在页面局部变量 | 文件进度已迁入 Pinia，按 `fileId/version` upsert |
| 撤回/焚毁乱序 | recall 是否早于 message 到达 | 前端缓存 pending 状态，消息后到时合并 |

## 日志

客户端日志：

```text
build/client_qt611/Release/mini_im_client.log
```

关闭日志：

```powershell
$env:MINIIM_DEBUG_LOG="0"
```

服务端日志直接输出到控制台，文件上传/下载会打印 stream、file_id、bytes、finish 等关键信息。

## 当前限制

* 账号系统是 dev 口径，没有密码、注册、权限后台
* SQLite 是单机存储口径
* UI 侧主要服务联调验证，没有做产品级配置中心
* `quic_client_worker.py` 仅保留为调试参考，不参与默认运行链路
