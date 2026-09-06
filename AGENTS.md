# Mini-IM 系统 - AGENTS.md（Qt 宿主 + Web UI + QUIC + Protobuf 版）

## 1. 项目定位

Mini-IM 是一个基于 `Qt 6 + C++` 的桌面 IM 项目。

当前重构目标不是继续扩展旧版 `WebSocket` 原型，而是重建一套可持续演进的 IM 内核，满足以下约束：

* Qt 原生层持有 QUIC 连接
* Web UI 只负责界面与交互，不直接接触传输层
* 应用层协议统一使用 Protobuf
* 业务可靠性依赖应用层幂等、ACK、同步游标与状态恢复
* 第一阶段先用 SQLite 做单机版可靠内核，后续可迁移到 Redis + PostgreSQL/MySQL

当前实现口径（更新于 2026-04-29）：

* 客户端构建链路已接入 `MsQuic`（vcpkg）
* Phase 2 链路 `Hello / Welcome / Heartbeat / 会话恢复 / initialStateLoaded` 已打通
* Phase 3 链路 `send_message / Ack / message_push / SyncRequest / SyncResponse` 已打通
* 单聊消息闭环（发送、落库、幂等、补偿同步）已完成
* Phase 4 链路 `create_conversation / receipt / recall / conversation_updated` 已打通
* 群聊、成员落库、已读推进、撤回、多端恢复已完成
* 群消息未读标签采用 `message_read_counters` 缓存读模型
* 客户端运行时 QUIC 已切换为纯 C++ `MsQuic` 实现
* `quic_client_worker.py` 仅保留为调试参考，不参与默认运行链路
* Phase 5 文件双向闭环已打通：上传/下载 `FileInit/FileFinish`、独立 QUIC 文件流、断点续传、故障注入
* 文件状态事件 `FileUpdated` 已补充 `version/updated_at_ms`，客户端按版本单调接收
* `FileFinish(success=true)` 增加完整性门禁：`transferred_bytes == file_size` 且 `sha256` 校验通过
* `client_file_id` 已采用发送意图 ID（intent_id）语义，并增加同 intent 的 `file_size` 防错校验
* intent 僵尸任务已支持软回收：超时上传任务可标记 `failed_stale` 并由新 `FileInit` 接管
* 文件上传完成后可生成 `MSG_FILE` 消息事件（引用 `file_id`）
* `direction=DOWNLOAD` 已打通服务端主动下发文件流与客户端落盘闭环
* 已补充大文件与故障恢复自动化测试场景（Phase 5）
* Phase 6 阅后即焚已落地：`burn_mode/burn_ttl_sec` 全链路生效，发送者与接收者统一按已读后 TTL 焚毁
* `message_deliveries` 已增加 `burn_started_at_ms/burn_at_ms/burned_at_ms`，服务端按批扫描到期焚毁并写入 `system-burn` 事件
* 焚毁后内容清理已落地：文本消息正文清空，文件消息仅保留 `{"kind":"file","fileId":"..."}` 最小引用元数据
* 已增加 `MINIIM_BURN_ENABLED` 与焚毁扫描批次配置开关，支持运行时快速回退
* 已完成 Phase 6 自动化回归与 10 分钟稳定性压测（600 秒）
* Web UI 已重构为现代聊天软件布局：左会话、中消息、右详情、调试抽屉
* 群聊已补齐建群、加群、退群、拉人、移除成员、改群名；单聊可通过已登录用户名创建
* 当前不引入正式账号系统；联调用 `dev-token:<用户名>` 写入用户表，用户存在性以“登录过”为准
* 前端消息渲染已改为轻量虚拟列表，消息入库采用批量写入与二分插入
* 单聊已读态已在 UI 层展示为 `未读/已读`，群聊继续展示 `x 人未读`
* 文件消息卡片已展示源文件 ID，文件传输栏展示传输任务 ID，下载保存目录会自动补默认文件名
* 文件上传/下载发送缓冲区已统一为堆上下文，避免 `SEND_COMPLETE` 后释放错误导致闪退
* 状态同步已补齐 `SyncResponse.has_more` 分页续拉，避免大量 `file_updated` 阻塞后续状态事件
* 前端状态已补齐消息先后乱序兜底：撤回、焚毁、已读和文件进度可在消息后到时正确合并
* `unreadTotal` 与文件进度已迁入 Pinia 状态，按消息、receipt、`fileId/version` 单调更新

---

## 2. 技术路线

### 2.1 客户端

* `Qt 6`
* `C++`
* `QWebEngine`
* `QWebChannel`
* `MsQuic`
* `protobuf`

### 2.2 Web UI

* `Vue 3`
* `TypeScript`
* `Vite`
* `Pinia`
* `naive-ui` 仅作为辅助组件库

### 2.3 服务端

* `Python 3.10+`
* `asyncio`
* `aioquic`
* `protobuf`
* `SQLite`

---

## 3. 总体架构

```text
Qt Client
├─ UI 宿主
│  ├─ MainWindow / QWebEngine
│  └─ QWebChannel Bridge
├─ IM Core
│  ├─ QUIC Session Manager
│  ├─ Message Service
│  ├─ Sync Engine
│  ├─ File Transfer Manager
│  └─ Local Cache
└─ SQLite（本地可选缓存）

Web UI
├─ 会话列表
├─ 聊天页面
├─ 文件卡片
└─ Bridge Event Store

QUIC Server
├─ QUIC Gateway
├─ Auth Service
├─ Conversation Service
├─ Message Service
├─ Delivery Service
├─ File Service
├─ Sync Service
└─ SQLite
```

---

## 4. 核心边界

### 4.1 Qt 原生层职责

* 持有并管理 QUIC 连接
* 管理登录、重连、心跳、同步、文件传输
* 承担 protobuf 编解码
* 向 Web UI 暴露高层 API
* 向 Web UI 推送高层事件

### 4.2 Web UI 职责

* 展示会话、消息、文件、状态
* 响应用户交互
* 调用 bridge API
* 订阅 bridge 事件并刷新状态

### 4.3 明确禁止

* Web UI 禁止直接访问 QUIC
* Web UI 禁止处理 protobuf 字节流
* Web UI 禁止直接实现重连、ACK、同步状态机
* Qt 宿主层禁止把业务逻辑散落到页面脚本中

---

## 5. 客户端目录基线

```text
client/
├── core/
│   ├── quic/
│   ├── protocol/
│   ├── session/
│   ├── sync/
│   ├── message/
│   ├── file/
│   └── model/
├── bridge/
├── ui/
│   └── webview/
└── main.cpp

web/
├── src/
│   ├── api/
│   ├── store/
│   ├── components/
│   ├── views/
│   ├── events/
│   └── types/
├── index.html
└── vite.config.ts
```

---

## 6. 服务端目录基线

```text
server/
├── quic/
├── protocol/
│   └── handlers/
├── services/
│   ├── auth/
│   ├── conversation/
│   ├── message/
│   ├── delivery/
│   ├── file/
│   └── sync/
├── storage/
│   ├── sqlite/
│   └── repo/
└── main.py
```

---

## 7. 协议设计原则

### 7.1 统一信封

所有控制面请求统一使用 `Envelope + oneof body`。

固定元信息至少包括：

* `version`
* `request_id`
* `channel`
* `session_id`
* `device_id`
* `seq`
* `client_time_ms`
* `trace_id`

### 7.2 通道约定

* `CHANNEL_CONTROL`：登录、消息控制、ACK、同步、会话事件
* `CHANNEL_FILE`：文件控制面

说明：

* `channel` 是逻辑通道，不等于 QUIC stream id
* 文件数据分片不走 protobuf，不塞进普通消息发送队列
* 文件二进制走独立 QUIC stream

### 7.3 幂等原则

所有写操作必须支持幂等：

* `request_id`：请求级幂等键
* `client_msg_id`：消息实体级幂等键
* `client_conv_id`：建会话级幂等键
* `client_file_id`：文件初始化级幂等键

`request_id` 规则：

* 格式建议：`client_id + monotonic_counter`
* 在单个 session 内唯一
* 服务端保存短期去重缓存

### 7.4 服务端事件 ID

所有服务端推送事件都必须带可去重的 `event_id`。

适用范围至少包括：

* `MessagePush`
* `Recall`
* `Receipt`
* `ConversationUpdated`
* `SyncResponse` 中的同步事件

客户端必须基于 `event_id` 做去重，不能把去重逻辑散落在 UI 层。

补充约束：

* `event_id` 在单个用户的同步流内必须全局唯一
* `seq` 用于同步推进与排序
* `event_id` 用于事件去重
* 禁止混用 `seq` 和 `event_id` 的职责

### 7.5 三层确认

* 传输确认：QUIC 负责到达
* 应用确认：服务端成功处理请求后返回 `Ack`
* 业务确认：消息被投递、已读、撤回、生效后返回 `Receipt` 或控制事件

禁止把三层确认混为一层。

---

## 8. 数据模型原则

必须优先围绕以下实体设计：

* `User`
* `Device`
* `Session`
* `Conversation`
* `Message`
* `Attachment`

### 8.1 会话模型

* 一个用户可以有多个设备
* 一个设备可以有多次会话
* QUIC 连接断开不等于业务 session 立即失效

### 8.2 消息模型

每条消息至少要有：

* `client_msg_id`
* `server_msg_id`
* `conversation_seq`
* `request_id`

### 8.3 群聊模型

群聊不是单聊多播版，必须有独立实体与成员关系。

建议：

* 消息正文在 `messages` 中只存一份
* 投递状态在 `message_deliveries` 中按用户跟踪

---

## 9. SQLite 设计原则

### 9.1 基本要求

* 开启 `WAL`
* 写请求统一走单写队列
* 表结构先规范化，避免一表塞所有字段
* 时间统一使用毫秒时间戳

### 9.2 第一版核心表

* `users`
* `devices`
* `sessions`
* `conversations`
* `conversation_create_requests`
* `conversation_members`
* `messages`
* `message_deliveries`
* `message_read_counters`
* `attachments`
* `sync_cursors`

### 9.3 游标原则

采用多维游标：

* `per user + per conversation`

说明：

* 全局游标负责统一增量同步入口
* 会话游标负责按会话追平与历史补拉
* `conversation_id` 不使用 `NULL` 语义偷表达全局，建议使用固定哨兵值

### 9.4 投递状态机

`message_deliveries` 至少支持：

* `sent`
* `delivered`
* `read`
* `failed`

并记录：

* `delivered_at`
* `read_at`
* `failed_at`
* `failure_reason`
* `conversation_id`
* `seq`

### 9.5 群消息未读标签缓存

群聊消息上的 `xx 人未读` 标签采用“真值 + 缓存”双层口径：

* 真值：`conversation_members.last_read_seq`
* 缓存：`message_read_counters`

约束：

* `message_read_counters` 只是展示缓存，不得作为业务真值
* 已读推进必须以 `last_read_seq` 单调递增为准
* 缓存错误允许重算，真值错误不允许
* `member_count` 采用消息发送当时的成员数口径
* 默认排除发送者本人，不把发送者算入该消息的未读人数

---

## 10. 同步与恢复原则

IM 的重点不是“发出去”，而是“状态可恢复”。

### 10.1 重连必须带上

* `session_id` 或 `resume_session_id`
* `device_id`
* `global_cursor`
* 最近请求确认信息

### 10.2 服务端返回的信息必须支持判断

* 当前会话能否恢复
* 从哪个游标开始补偿
* 是否需要重新认证
* 当前会话对应的 `user_id`

### 10.3 心跳职责

心跳只做两件事：

* 保活
* 在线状态判断

禁止把业务逻辑塞进心跳。

### 10.4 同步语义

同步返回的是事件流，不只是消息列表。

至少要支持同步以下事件：

* 消息事件
* 撤回事件
* 已读推进事件
* 会话更新事件
* 文件状态事件

`SyncResponse` 必须以统一事件结构承载这些变化，避免后续再拆第二套同步协议。

实现上应统一采用 `SyncEvent` 外壳承载事件体，禁止回退到“消息列表 + 其他接口”的拆分方案。

---

## 11. 文件传输原则

### 11.1 控制面

通过 protobuf 控制消息完成：

* `FileInit`
* `FileFinish`

建议预留：

* `priority`

说明：

* `priority` 是传输层调度 hint
* `priority` 不代表业务优先级
* `priority` 不改变业务语义，只影响文件传输调度策略

### 11.2 数据面

* 文件内容通过独立 QUIC stream 传输
* 支持断点续传
* 支持校验
* 不与普通聊天消息抢同一调度队列
* `FileFinish(success=true)` 前必须满足 `transferred_bytes == file_size`
* `FileUpdated` 建议携带 `version` 与 `updated_at_ms`，客户端按版本单调处理

---

## 12. Bridge 约束

### 12.1 Web UI 可调用的高层 API

* `connect`
* `disconnect`
* `sendMessage`
* `recallMessage`
* `sendReceipt`
* `createConversation`
* `sendFile`
* `loadHistory`

### 12.2 Qt 可推送给 Web UI 的事件

* `connectionChanged`
* `initialStateLoaded`
* `messagePushed`
* `messageUpdated`
* `conversationUpdated`
* `syncProgress`
* `fileProgress`
* `errorRaised`

Bridge 层只传高层语义对象，不传底层字节流。

`initialStateLoaded` 至少应包含：

* 当前登录用户信息
* 会话列表
* 最近消息
* 未读数

---

## 13. 开发顺序

### 第一阶段

* QUIC 连接打通
* `Hello / Welcome`
* 心跳
* 工程骨架可运行

### 第二阶段

* 登录态
* 会话恢复骨架
* `initialStateLoaded`

### 第三阶段

* 单聊发送
* 应用层 `Ack`
* SQLite 落库
* 去重
* `SyncRequest / SyncResponse`

### 第四阶段

* 群聊
* 会话成员管理
* 已读
* 撤回

### 第五阶段

* 文件传输
* 分片
* 断点续传
* 故障注入与压测

### 第六阶段

* 阅后即焚
* 稳定性收尾与回归

---

## 14. 测试要求

必做：

* protobuf 编解码测试
* 登录与恢复测试
* 单聊消息测试
* 幂等去重测试
* 同步补偿测试
* 群聊测试
* 撤回与已读测试
* 文件断点续传测试
* 服务端宕机恢复测试

建议补充：

* 多设备并发测试
* 群消息高压测试
* 重复提交测试
* 重复 ACK 测试

---

## 15. 底线原则

后续所有设计和实现都不能破坏以下四条：

* 传输层只负责到达
* 应用层负责语义
* 每个写操作都可重试
* 每个状态都可恢复

如果新功能破坏这四条，优先回退设计，不要硬加。

## 16. 编码规范

### 16.1 命名

| 类型 | 规则 | 示例 |
|------|------|------|
| 文件名 | 全小写 | `applegamemodel.h` |
| 类名 | 前缀 + 驼峰 | `AppleGameModel` |
| 成员变量 | `m_` 前缀 | `m_appleList` |
| 静态成员 | `s_` 前缀 | `s_instance` |
| 全局变量 | `g_` 前缀 | `g_running` |
| 成员函数 | 小写驼峰 | `startGame()` |
| 非成员函数 | 大写驼峰 | `GetObjectCount()` |
| 宏 | 全大写下划线 | `MAX_APPLE_COUNT` |
| 命名空间 | 全小写 | `namespace apple {}` |

### 16.2 头文件

每个头文件必须带规范注释头和 include guard。

### 16.3 强制规则

- 单参构造必须 `explicit`
- 虚函数重写必须 `override`
- 无拷贝需求时必须 `= delete`
- 禁止使用 `NULL`，统一使用 `nullptr`
- 禁止 `malloc/free`
- 禁止 `new[]/delete[]`
- 禁止 `using namespace std`
- 禁止无意义重复代码
- 禁止在代码中硬编码路径分隔符
- 常量优先使用 `constexpr`
- UI 文案允许中文，除此之外禁止中文硬编码

### 16.4 格式要求

- 花括号独占一行
- 推荐单行不超过 `120` 字符
- 函数尽量不超过 `80` 行
- 复杂逻辑必须拆分

---
