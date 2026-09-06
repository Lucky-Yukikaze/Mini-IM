# Mini-IM 项目评估 · 2026-09-07

**建议保留现有技术栈，分模块重构可靠性实现。** 评估以项目文档中的“Qt 桌面客户端、原生 QUIC 连接、单机可靠内核”为目标；现有消息模型、业务服务和协议定义已有可复用的实现，更换框架仍需解决本次发现的状态恢复问题。

渐进重构 = 保持现有功能可运行，每次修正或替换一个模块，并用测试确认行为。

| 决策 | 范围与理由 |
| --- | --- |
| 保留 | Qt 宿主与 Vue 页面之间的职责边界、Protobuf 协议定义、Python 业务服务、SQLite 消息与成员模型；消息写入已把正文、投递记录和同步事件放在同一数据库事务中，见 [message_repo.py](D:/Codex/Mini-IM/server/storage/repo/message_repo.py:114)。事务 = 一组数据库写入共同提交或共同回滚。 |
| 优先重构 | 客户端会话与同步、文件任务的完成和恢复、事件重放与页面状态合并；这些路径已有具体错误，可以逐项复现和验收。 |
| 整体重写的代价 | 需要重新实现已经存在的群成员、已读、撤回、文件与同步行为，并重新验证 Qt 与原生传输的集成；本次缺陷均能定位到现有模块及模块之间的接口。 |

**实测结果：** 服务端现有 25 个测试全部通过；Web 构建通过；单独执行 `vue-tsc --noEmit` 报 1 处错误，位于 [chat-view.vue](D:/Codex/Mini-IM/web/src/views/chat-view.vue:200)，使用了 `.at(-1)`，而类型配置只包含 ES2020；Qt 客户端在现有 `build/client_qt611` 目录中增量构建通过。本次没有做从空目录开始的完整构建、真实双客户端网络中断测试或并发容量测试。

现有 25 个测试直接调用服务和存储代码，没有经过 Qt 客户端与 QUIC 网关。现有“大文件恢复”用例实际处理 **2,097,152 字节**，通过修改数据库时间和分两次调用服务模拟恢复，见 [test_phase5_file_flow.py](D:/Codex/Mini-IM/server/tests/test_phase5_file_flow.py:349)；它能验证业务入口，但无法验证网络中断、客户端重启或发送缓冲行为。文档中的 600 秒稳定性结果属于历史记录，本次未重跑。

**另用六个最小场景复现了以下问题。** 事件 ID = 一次业务变化的唯一标识。 数据均为临时生成；文件中断采用提交步骤之间注入异常后重新打开数据库，下载场景以只记录发送数据的替身替代网络接收端，因此这些结果属于针对性复现。

| 问题类别 | 已复现的行为 | 根因与定位 |
| --- | --- | --- |
| 用户与消息状态 | ① 切换到 Bob 后，仍保留 Alice 的 1 个会话和 1 条消息；② 已焚毁消息再次收到原始消息数据后，`burned`、`recalled` 都变回 `false`；③ 同一条消息在线推送与历史同步的 `event_id` 不相等。 | 初始状态更新直接替换用户 ID，未隔离旧用户数据；消息合并覆盖已经生效的撤回和焚毁标志；消息事件在数据库和在线推送中分别生成 ID。见 [session.ts](D:/Codex/Mini-IM/web/src/store/session.ts:143)、[消息合并](D:/Codex/Mini-IM/web/src/store/session.ts:67)、[事件生成](D:/Codex/Mini-IM/server/storage/repo/message_repo.py:234)。 |
| 文件完成与恢复 | ④ 文件完成状态提交后，在生成聊天消息前注入异常；重新打开数据库并重试，返回成功、任务为 `completed`，文件聊天消息数量仍为 **0**；⑤ 下载仅把 **26 字节**测试正文加入发送队列，无接收端确认也变成 `completed`。 | 完成文件与生成消息分两次提交，重试又受 `result.changed` 条件限制；下载发送循环结束即确认完成。客户端收到完成事件还会删除下载任务，跨流到达顺序会影响后续落盘。见 [service.py](D:/Codex/Mini-IM/server/services/file/service.py:230)、[server.py](D:/Codex/Mini-IM/server/quic/server.py:150)、[sessionmanager.cpp](D:/Codex/Mini-IM/client/core/session/sessionmanager.cpp:1508)。最后一项为代码路径推断，未做网络复现。 |
| 焚毁后的内容清理 | ⑥ 两个用户的焚毁事件生成后，清理了 1 行消息正文，正文长度为 **0**；随后从起始位置同步，仍返回完整原文。 | 清理只更新 `messages.content`，历史事件中的消息副本仍在 `sync_events.payload`，同步服务直接解码返回。见 [delivery_repo.py](D:/Codex/Mini-IM/server/storage/repo/delivery_repo.py:386)、[同步服务](D:/Codex/Mini-IM/server/services/sync/service.py:32)。 |

复现脚本和原始输出：[服务端脚本](D:/Codex/Mini-IM/tmp/architecture-review/probe_server.py)、[页面状态脚本](D:/Codex/Mini-IM/tmp/architecture-review/probe_frontend.mjs)、[服务端结果](D:/Codex/Mini-IM/tmp/architecture-review/server-results.json)、[页面状态结果](D:/Codex/Mini-IM/tmp/architecture-review/frontend-results.json)。

**静态检查还发现三处结构性缺口：**

- 恢复信息的归属没有落实到 Qt：页面 [onConnect](D:/Codex/Mini-IM/web/src/views/chat-view.vue:296) 先清空会话 ID，再把它作为恢复参数传入；页面的 `globalCursor` 只在初始状态中赋值。游标 = 已处理事件的位置。Qt 断开时会清空待传文件任务，[再次上传](D:/Codex/Mini-IM/client/core/session/sessionmanager.cpp:1190) 又创建新意图 ID，下载请求的恢复偏移固定为 0；服务端的恢复能力尚未完整接入用户操作路径。
- 客户端 [sessionmanager.cpp](D:/Codex/Mini-IM/client/core/session/sessionmanager.cpp) 共 **1,864 行**，集中处理连接、编码、同步、文件读写与事件转换；[上传循环](D:/Codex/Mini-IM/client/core/session/sessionmanager.cpp:1375) 和服务端下载循环连续读取并提交文件，没有按待发送数据量暂停读取。事件循环 = 依次执行网络与定时回调的线程调度过程；同步阻塞代码会延迟同线程的其他任务，这是 [Python 官方文档](https://docs.python.org/3/library/asyncio-dev.html#running-blocking-code) 明确说明的行为。独立文件流仍需要应用层调度；[MsQuic 文档](https://microsoft.github.io/msquic/msquicdocs/docs/Streams.html) 也说明发送完成回调可能仅表示内部缓冲已复制数据。
- 单写队列 = 把数据库写任务交给一个执行入口顺序处理；当前服务端启动了它，却没有业务代码调用 `submit`，实际仓储直接访问连接。应以完整业务事务为任务单位，避免拆成逐条 SQL 提交；[SQLite 官方文档](https://www.sqlite.org/wal.html#concurrency) 说明 WAL 模式仍只有一个同时执行的写者；WAL = 先把变更写入日志文件，再合并到数据库文件的写入模式。构建方面，[bootstrap.ps1](D:/Codex/Mini-IM/tools/bootstrap.ps1:29) 仍写死 Qt 6.8.0 和旧构建目录，与 README 的 Qt 6.11.0 不一致，前端构建也未包含类型检查。

**建议按以下顺序推进，每一步保留可运行版本：**

| 顺序 | 实施范围 | 验收依据 |
| --- | --- | --- |
| 先修正确性 | 将六个复现场景补入正式测试；隔离用户状态，统一在线与历史事件 ID，保证撤回和焚毁状态不会被旧消息覆盖，清理历史事件中的正文；让文件完成与消息生成共同提交，并由下载端落盘、校验后确认完成。 | 六个场景验证修复后的正确行为；现有 25 个测试继续通过。 |
| 再整理客户端职责 | 从会话类逐步移出连接收发、同步恢复和文件任务；恢复参数由 Qt 管理，消息缓存与处理位置共同保存，待发送消息与文件意图在重启后可恢复；文件读取随发送进度分批继续。 | 真实两个客户端验证断线重连、切换用户、程序重启、重复确认和文件续传。 |
| 最后补工程保障 | 统一环境脚本和依赖安装方式，把类型检查与构建纳入自动检查，增加协议拆包和跨端测试，再测聊天与文件同时传输的延迟和资源占用。 | 从空构建目录可复现构建；类型检查通过；以实测数据决定是否替换单个组件。 |

`dev-token` 和关闭证书校验是现有文档明确采用的联调策略；正式账号与服务器身份验证应随产品目标单独安排。当前提供目录不是 Git 工作树，本次无法审查提交历史；评估产物为本文与临时复现脚本，现有业务源码未修改。

复核命令（从项目根目录运行；本次 Python 依赖安装在工作区临时目录）：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'tmp/architecture-review-deps'
python -m unittest discover server/tests -v
python ./tmp/architecture-review/probe_server.py
node ./tmp/architecture-review/probe_frontend.mjs
npm --prefix ./web run build
Push-Location ./web
& ./node_modules/.bin/vue-tsc.cmd --noEmit
Pop-Location
cmake --build ./build/client_qt611 --config Release --target mini_im_client
```
