# Mini-IM 重构计划

## 0. 进度状态（更新于 2026-04-29）

* Phase 1：已完成
* Phase 2：已完成
* Phase 3：已完成
* Phase 4：已完成
* Phase 5：已完成
* Phase 6：已完成
* 阶段口径已与 `AGENTS.md` 对齐：当前进入联调稳定性修补，不再新增主功能

当前联调口径（2026-04-29）：

* Qt 客户端运行口径已切换到 `Qt 6.11.0 + msvc2022_64`
* 当前客户端构建目录统一为 `build/client_qt611`
* 旧 `build/client_qt6_vcpkg` 已清理，不再作为运行口径
* 控制流已改为 `4 byte big-endian length prefix + Envelope protobuf`
* 客户端控制流已支持缓冲区累积拆包，避免 QUIC 字节流粘包/拆包导致 `ParseFromArray` 失败
* 文件下载链路已修复：客户端显式放开 `PeerUnidiStreamCount=16`，并处理 `QUIC_CONNECTION_EVENT_PEER_STREAM_STARTED`
* 文件下载已实测通过：服务端主动下发单向文件流，客户端下载并落盘成功
* Web UI 已切换为现代聊天软件布局，消息渲染使用轻量虚拟列表
* 群聊支持建群、加群、退群、拉人、移除成员、改名；单聊支持通过已登录用户名创建
* 单聊已读态显示为 `未读/已读`，群聊未读态显示为 `x 人未读`
* 当前不引入正式账号系统，联调用“用户名”生成 `dev-token:<用户名>`，用户首次连接后写入用户表
* 文件上传完成后生成 `MSG_FILE` 消息；文件卡片显示源文件 ID，传输列表显示传输任务 ID
* 下载保存路径传目录时客户端自动补默认文件名
* 状态同步已支持 `SyncResponse.has_more` 自动续拉，前端对撤回、焚毁、已读、文件进度做乱序合并
* `unreadTotal` 与文件进度由 Pinia 统一维护
* 客户端日志默认开启，日志文件为 `build/client_qt611/Release/mini_im_client.log`
* 需要关闭客户端日志时可设置环境变量 `MINIIM_DEBUG_LOG=0`

当前最小联调命令：

```powershell
# 窗口1
# 在项目根目录执行
python .\server\main.py
```

```powershell
# 窗口2
Set-Location .\web
npm run build
Set-Location ..
```

```powershell
# 窗口3
$env:QT_DIR="<你的Qt安装目录>\6.11.0\msvc2022_64"
git clone https://github.com/microsoft/vcpkg .\thirdparty_install\vcpkg
.\thirdparty_install\vcpkg\bootstrap-vcpkg.bat
.\thirdparty_install\vcpkg\vcpkg.exe install msquic protobuf --triplet x64-windows
cmake -S .\client -B .\build\client_qt611 -DCMAKE_TOOLCHAIN_FILE="$PWD\thirdparty_install\vcpkg\scripts\buildsystems\vcpkg.cmake" -DVCPKG_TARGET_TRIPLET=x64-windows -DCMAKE_PREFIX_PATH="$env:QT_DIR"
cmake --build .\build\client_qt611 --config Release --target mini_im_client
& "$env:QT_DIR\bin\windeployqt.exe" --release --compiler-runtime --dir .\build\client_qt611\Release .\build\client_qt611\Release\mini_im_client.exe
.\build\client_qt611\Release\mini_im_client.exe
```

Phase 1 完成情况：

* 已完成 `proto/` 分域协议文件并可生成 Python 代码
* 已完成 SQLite `schema.sql`（WAL + 核心表 + 单写队列骨架）
* 已完成客户端 `Qt6 + CMake` 工程骨架，并通过本机编译
* 已完成 Web `Vue3 + TS + Vite + Pinia` 工程骨架，并可构建
* 已完成服务端 `aioquic` 工程骨架，具备 `Hello/Welcome/Heartbeat` 最小链路
* 已清理旧版 `WebSocket` 残留目录与旧脚本入口
* 已统一脚本入口：`bootstrap.ps1`、`dev_start.ps1`、`dev_stop.ps1`

---

## 1. 当前目标

本轮不是在旧版 `WebSocket` 原型上继续补功能，而是按新的技术路线重做基础架构：

* 客户端：`Qt 6 + C++ + QWebEngine + MsQuic`
* Web UI：`Vue 3 + TypeScript + Vite + Pinia`
* 服务端：`Python asyncio + aioquic + SQLite`
* 协议：`Envelope + 分域 proto`
* 存储：`SQLite + WAL + 单写队列`

重构优先目标：

* 建立可靠的连接、会话、同步、幂等基础
* 保证后续群聊、撤回、已读、阅后即焚、文件传输都能接入
* 明确 Qt 原生层、Web UI、服务端之间的边界

---

## 2. 已确认架构约束

### 2.1 客户端边界

* Qt 原生层持有 QUIC
* Web UI 不直接碰 QUIC
* Web UI 不处理 protobuf 字节流
* Web UI 只通过 `QWebChannel` 调用高层 API 和订阅高层事件

### 2.2 协议边界

* 控制面统一走 `Envelope`
* 文件控制面走 protobuf
* 文件数据面走独立 QUIC stream
* 所有写请求必须支持幂等

### 2.3 数据边界

* 先做 SQLite 单机可靠内核
* 消息正文单份存储
* 投递状态与阅读状态独立建模
* 游标采用多维游标

---

## 3. 第一阶段要完成的设计产物

这一阶段先产出“基础设计”，不急着写复杂业务逻辑。

### 3.1 协议

需要完成：

* `common.proto`
* `envelope.proto`
* `auth.proto`
* `conversation.proto`
* `message.proto`
* `file.proto`
* `sync.proto`

设计要求：

* `Envelope` 固定元信息
* `trace_id` 进入固定元信息
* `channel` 明确区分控制面和文件控制面
* `Ack` 使用统一确认结构
* 服务端推送统一带 `event_id`
* `event_id` 在单个用户同步流内全局唯一，`seq` 负责推进，`event_id` 负责去重
* `SyncResponse` 以事件流而不是纯消息列表返回
* `SyncResponse` 统一使用 `SyncEvent` 外壳
* `Welcome` 必须返回当前登录身份的 `user_id`
* `request_id`、`client_msg_id`、`client_conv_id`、`client_file_id` 语义明确
* 阅后即焚字段与处理已在 Phase 6 落地

### 3.2 表结构

需要完成：

* `users`
* `devices`
* `sessions`
* `conversations`
* `conversation_members`
* `messages`
* `message_deliveries`
* `attachments`
* `sync_cursors`

设计要求：

* `sync_cursors` 使用多维游标
* 全局游标使用固定哨兵值，不靠 `NULL`
* `messages` 具备消息幂等唯一约束
* `message_deliveries` 支持状态机和时间戳
* `message_deliveries` 冗余 `conversation_id` 与 `seq`

### 3.3 目录骨架

需要完成：

* `client/` 新目录骨架
* `web/` 新目录骨架
* `server/` 新服务端目录骨架
* `proto/` 文件拆分

---

## 4. 分阶段实施计划

## Phase 1：基础协议与工程骨架（已完成）

目标：

* 整理项目目录
* 固化 proto
* 固化 SQLite schema
* 建立生成代码与构建脚本

交付物：

* 新版 `proto/`
* 新版 `schema.sql`
* Qt 客户端工程骨架
* Web UI 工程骨架
* 服务端 QUIC 工程骨架

完成标志：

* proto 可生成代码
* schema 可初始化数据库
* 客户端、前端、服务端都能独立启动到基础状态

## Phase 2：连接、登录、会话恢复（已完成）

目标：

* 打通 Qt `MsQuic` 客户端和 Python `aioquic` 服务端
* 建立 `Hello / Welcome / Heartbeat`
* 打通 session 恢复骨架

交付物：

* `quic_client`
* `server/quic/server.py`
* `session_mgr`
* 基础 bridge API
* `initialStateLoaded` 初始化事件

完成标志：

* 客户端可连接服务端
* 登录可成功
* 心跳正常
* 主动断开后可走恢复流程

当前状态（2026-04-20）：

* 已完成 `Hello / Welcome / Heartbeat` 真实收发
* 已完成 `session_mgr` 会话恢复骨架
* 已完成 bridge `initialStateLoaded` 与 Web 状态初始化
* 已完成客户端构建链路 `MsQuic` 接入（vcpkg）
* 已完成客户端运行时纯 C++ `MsQuic` 替换（不再依赖 Python worker）

## Phase 3：单聊消息闭环

目标：

* 消息发送
* 通用 `Ack`
* SQLite 落库
* 幂等去重
* 基础同步补偿

交付物：

* `message_service`
* `sync_engine`
* 服务端 message/sync repo 与 service

完成标志：

* 单聊消息可发可收
* 重发不会产生重复消息
* 断线重连后可补拉未同步消息

当前状态（2026-04-20）：

* 已完成 `send_message -> Ack -> message_push` 链路
* 已完成 SQLite 持久化：`messages / message_deliveries / sync_events / sync_cursors`
* 已完成按 `conversation_id + sender_id + client_msg_id` 的幂等去重
* 已完成 `SyncRequest / SyncResponse` 增量补偿
* 已完成 Qt Bridge `sendMessage` 对接与 Web 端真实发送（移除仅本地假发送路径）
* 已补充并通过消息闭环与幂等测试

## Phase 4：群聊与消息控制事件

目标：

* 群聊建会话
* 成员管理
* 已读
* 撤回

交付物：

* conversation service
* receipt/recall 协议与处理
* Web UI 会话页更新

完成标志：

* 群聊可用
* 群消息可同步
* 撤回和已读状态可在多端恢复

当前状态（2026-04-20）：

* 已完成 `create_conversation -> conversation_updated` 链路
* 已完成群聊成员落库与发送成员校验
* 已完成群消息按成员 fan-out 写入 `sync_events`
* 已完成 `receipt / recall` 处理与多端同步恢复
* 已完成群消息 `xx 人未读` 读模型缓存：`message_read_counters`
* 已完成 Qt Bridge `createConversation / sendReceipt / recallMessage`
* 已完成 Web 端真实会话列表、撤回和未读标签展示
* 已补充并通过 Phase 4 服务端测试

## Phase 5：文件传输与故障注入

目标：

* `FileInit / FileFinish`
* 独立文件 stream
* 断点续传
* 故障注入开关
* 文件优先级 hint 预留

说明：

* `priority` 仅作为传输层调度 hint
* 不得把 `priority` 实现成业务优先级语义

交付物：

* `file_service`
* stream scheduler
* 压测与故障注入工具

完成标志：

* 文件可上传下载
* 断线后可续传
* 可模拟客户端断连、服务端宕机、恢复后补偿

当前状态（2026-04-21）：

* 已完成 `FileInit / FileFinish` 控制面链路与应用层 Ack
* 已完成独立 QUIC 文件流（文件数据不走控制流）
* 已完成上传断点续传、下载断点续传、`sha256` 校验与完整性门禁（`transferred_bytes == file_size`）
* 已完成 `FileUpdated(version, updated_at_ms)` 推送与同步，客户端按版本单调接收
* 已完成 intent_id 防误判增强：同 intent `file_size` 不一致拒绝
* 已完成僵尸任务软回收：`uploading` 超时可标记 `failed_stale` 并被新 `FileInit` 接管
* 已完成故障注入开关（按字节阈值/概率触发断连）
* 已完成上传完成后的 `MSG_FILE` 消息映射（引用 `file_id`）
* 已完成下载链路（`FILE_DIRECTION_DOWNLOAD`）：服务端主动下发文件流，客户端落盘
* 已补充 Phase 5 自动化验证：大文件上传恢复、下载恢复、完整性门禁、僵尸任务接管

## Phase 6：阅后即焚与稳定性收尾

目标：

* 实现 burn policy
* 做清理任务
* 补稳定性测试

交付物：

* burn policy 协议处理
* 后台清理任务
* 回归测试集

完成标志：

* 阅后即焚策略生效
* 清理机制稳定
* 核心流程具备回归测试

当前状态（2026-04-22）：

* 已完成 `burn_mode / burn_ttl_sec` 服务端与客户端全链路接入（发送、落库、同步、UI 展示）
* 已完成发送者与接收者统一焚毁语义：发送者在入库即启动 TTL，接收者在首次已读推进启动 TTL
* 已完成后台焚毁扫描与系统事件下发：到期后写入 `recall(operator_id=system-burn)` 并支持离线补偿
* 已完成二次内容清理：全员焚毁后文本正文清空，文件消息仅保留 `fileId` 引用元数据
* 已完成焚毁开关与批次配置：`MINIIM_BURN_ENABLED`、`MINIIM_BURN_SWEEP_*`、`MINIIM_BURN_PURGE_BATCH_SIZE`
* 已补充并通过 Phase 6 自动化测试（含边界 TTL、竞态、开关、内容清理）
* 已完成 10 分钟稳定性压测：600 秒连续循环，`loops=13915`，未出现异常与游标回退

---

## 7. 关键风险

### 7.1 Qt 与 MsQuic 集成风险

需要尽早验证：

* 事件循环接入方式
* stream 生命周期管理
* 与 Qt 信号槽和线程模型的配合

### 7.2 会话恢复复杂度

风险点：

* reconnect 后的 session 续接
* request 幂等缓存
* sync cursor 推进时机

### 7.3 文件传输调度复杂度

风险点：

* 大文件流量占满连接
* 控制消息优先级被压制
* 续传与校验状态不一致

### 7.4 多设备一致性风险

风险点：

* 已读推进粒度
* 群聊消息同步
* 控制事件补偿

---
