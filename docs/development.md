# Mini-IM 开发指南

返回 [项目入口](../README.md)。命令默认从项目根目录的 PowerShell 执行。
架构和编码规则见 [AGENTS.md](../AGENTS.md)，
本机环境、已执行检查与遗留问题见 [重构执行记录](refactoring-progress.md)。
以下安装与配置步骤按代码整理；已实际执行的组合和结果以执行记录为准。
本机已有目录的增量构建不能证明新环境安装成功。

| 任务 | 本文入口 |
| --- | --- |
| 首次启动 | [准备依赖](#准备依赖) → [生成协议与构建](#生成协议与构建) → [运行与数据](#运行与数据) |
| 日常调试 | [配置与排查](#配置与排查) · [消息发送与恢复](#消息发送与恢复) · [文件任务与恢复](#文件任务与恢复) |
| 检查改动 | [验证](#验证) · [真实原生客户端联调](#真实原生客户端联调) · [独立服务进程宕机联调](#独立服务进程宕机联调) |

## 准备依赖

需要 Python、Node.js/npm、Git、CMake、Visual Studio 的 C++ 编译工具，
以及包含 Core、Sql、Widgets、WebChannel、WebEngineWidgets 的 Qt 6；原生联调驱动还需要 Network。
WebEngineWidgets 缺失时 CMake 仍能构建占位窗口，但无法加载聊天页面。
Sql = Qt 访问数据库的模块；本地缓存使用其 QSQLITE 驱动，部署时须包含 SQLite 插件。
依赖与目标定义以 [CMakeLists.txt](../client/CMakeLists.txt) 为准。

Python 依赖约束见 [requirements.txt](../server/requirements.txt)；
Web 依赖由 [package.json](../web/package.json) 声明、[package-lock.json](../web/package-lock.json) 固定。
C++ 通过 vcpkg 查找 MsQuic 和 Protobuf；当前尚未固定 vcpkg 基线版本。

创建项目虚拟环境并安装 Python 与 Web 依赖：

~~~powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r ./server/requirements.txt
npm --prefix ./web ci
~~~

已有 vcpkg 工作目录时跳过克隆和引导：

~~~powershell
git clone https://github.com/microsoft/vcpkg ./thirdparty_install/vcpkg
& ./thirdparty_install/vcpkg/bootstrap-vcpkg.bat
& ./thirdparty_install/vcpkg/vcpkg.exe install msquic protobuf --triplet x64-windows
~~~

## 生成协议与构建

协议定义只在 [proto/](../proto/) 中修改；Python 生成文件纳入 Git，
C++ 生成文件由 CMake 放到构建目录。协议改变后重新生成 Python 文件并重建客户端，
不要直接编辑生成代码。

~~~powershell
& ./.venv/Scripts/python.exe ./server/tools/generate_proto.py
npm --prefix ./web run build
~~~

下面使用 Qt 6.11.0 和 Visual Studio 2022 作为配置示例。
`QT_DIR` 改为本机安装目录；切换 Qt、编译器或依赖路径时使用新的构建目录。

~~~powershell
$env:QT_DIR = 'D:/Qt/6.11.0/msvc2022_64'
$qtToolchain = Join-Path (Get-Location) 'thirdparty_install/vcpkg/scripts/buildsystems/vcpkg.cmake'
cmake -S ./client -B ./build/client_qt611 -G "Visual Studio 17 2022" -A x64 "-DCMAKE_TOOLCHAIN_FILE=$qtToolchain" "-DVCPKG_TARGET_TRIPLET=x64-windows" "-DCMAKE_PREFIX_PATH=$env:QT_DIR" -DBUILD_TESTING=ON
cmake --build ./build/client_qt611 --config Release --target mini_im_client mini_im_download_tests mini_im_upload_tests mini_im_state_tests mini_im_sync_tests mini_im_control_tests mini_im_native_driver
& "$env:QT_DIR/bin/windeployqt.exe" --release --compiler-runtime --dir ./build/client_qt611/Release ./build/client_qt611/Release/mini_im_client.exe
~~~

`windeployqt` = 将 Qt 运行时和插件复制到程序目录的部署工具；
MsQuic、Protobuf 等非 Qt 依赖由所用依赖配置提供，运行前需保证其 DLL 可被找到。
部署前先退出使用该构建目录的客户端和测试驱动；出现 DLL 无法覆盖时，处理占用后重新执行部署。
Qt 部署结果应包含 `Qt6Sql.dll` 和 `sqldrivers/qsqlite.dll`；缺少 SQLite 驱动会使缓存打开失败。
打包页面后，客户端可直接读取 `web/dist/index.html`。

## 运行与数据

在一个终端启动服务端：

~~~powershell
& ./.venv/Scripts/python.exe ./server/main.py
~~~

默认开发入口监听 `127.0.0.1:4433`。在另一个终端启动已部署的客户端：

~~~powershell
& ./build/client_qt611/Release/mini_im_client.exe
~~~

页面开发时，另开终端运行：

~~~powershell
npm --prefix ./web run dev -- --host 127.0.0.1 --port 5173 --strictPort
~~~

在启动客户端的终端中设置 `$env:MINIIM_WEB_URL = 'http://127.0.0.1:5173'`，
可让 Qt 加载开发页面；未设置时优先加载本地打包页面。
调试入口使用用户名连接，目标用户须至少登录过一次，再创建单聊或邀请入群。
浏览器独立打开页面时，[页面接口](../web/src/api/bridge.ts) 会在缺少 Qt 接口时生成本地演示事件；
其中的“已连接”和消息仅用于页面调试，真实网络联调须从 Qt 客户端进入。

停止服务端和页面开发服务时，在各自终端按 Ctrl+C；退出客户端时关闭其窗口。
本节描述本机开发方式；正式认证、证书验证与外网部署须先明确产品目标。

| 数据 | 当前默认位置或行为 |
| --- | --- |
| 数据库 | `server/storage/sqlite/miniim.db`；启动时创建或升级现有结构 |
| 服务端文件 | `server/storage/files/`；可用 `MINIIM_FILE_ROOT` 更改 |
| 开发凭据 | `server/quic/dev_cert.pem` 与 `dev_key.pem`；缺失时生成 |
| 客户端下载 | 保存到用户指定路径；未完成数据写入相邻的 `.miniim-<任务 ID>.part` 临时文件 |
| 客户端状态 | Qt 返回的当前用户应用数据目录（`QStandardPaths::AppLocalDataLocation`）下的 `state/`；可用 `MINIIM_STATE_ROOT` 更改；按服务端地址、用户和设备分别保存 SQLite 文件 |

默认仓库内运行数据与私钥已由 [.gitignore](../.gitignore) 排除，客户端状态默认位于用户数据目录。
自定义状态或文件目录请放在仓库外或已忽略的 `tmp/` 中，避免把运行数据提交入库。
测试使用独立临时目录；不要通过删除现有数据库处理结构升级问题。

数据库升级新增 `file_transfers.source_file_id`。
旧下载记录未保存来源，无法自动证明其续传目标；这类旧意图重试会返回冲突，
已有记录和文件保留，重新点击下载可发起新的意图。

每个服务端连接最多同时调度 8 条下载，每条下载待确认发送数据最多 131072 字节；
达到任务数上限时返回 `download queue is full; retry later`，完成已有任务后可重试。
客户端同时最多启动 8 个文件任务，其余保存到本地等待；任务完成后继续后续任务。

## 消息发送与恢复

连接后发送文本消息，Qt 先把发送意图保存到本地状态数据库，页面收到保存成功的回调后清空草稿。
保存失败会保留草稿；待确认和失败消息显示在会话的发送列表中，失败项可点击重试。
发送意图 = 一次发送使用的固定标识、目标会话和正文；重试沿用该意图。

退出或进程崩溃后，用同一服务端地址、用户和设备重新连接，Qt 会在同步追平后继续待确认消息。
网络断开后由 Qt 自动重新连接；点击“断开”会取消正在等待或尝试的重连。
其他业务操作见 [控制写入与重试边界](#控制写入与重试边界)，文件恢复方式见文件任务一节。
未连接时暂不接收新的发送意图。

| 情况 | 当前处理 |
| --- | --- |
| 等待服务端确认 | 按队列顺序发送，每次等待 1 条消息；定时器每 500 毫秒检查，距上次尝试至少 5000 毫秒才重试同一请求 |
| 返回错误 | 401、408、429 或不小于 500 的错误码保留为待重试；401 会清除失效会话并重新登录；其他失败显示原因并等待手动重试 |
| 服务端确认成功 | 清空发送意图保存的正文并移出待确认列表；收到自己消息的撤回或焚毁事件时也清理发送副本 |

重连失败后的等待时间依次为 1000、2000、4000、8000、16000、30000 毫秒，随后保持 30000 毫秒；
登录成功后重新从 1000 毫秒计时。每次连接尝试等待登录最多 10000 毫秒，超时后关闭连接再重试。
心跳按服务端给定间隔运行，0 使用 15 秒，超过 300 秒按 300 秒处理；
每次心跳检查距最近已解析响应的时间，达到两个心跳间隔则触发重连。
控制流关闭或复位也会触发恢复，页面等待状态为 `reconnecting`。

队列和失败原因保存在上方客户端状态目录，切换用户分别恢复各自记录。
发送确认仅说明服务端已处理，不能据此推断接收方已收到或已读。
消息保存和页面检查见 [发送意图验证](refactoring-progress.md#消息发送意图与确认恢复--2026-09-07)，
自动重连见 [连接恢复验证](refactoring-progress.md#自动重连与会话恢复--2026-09-07)。

## 控制写入与重试边界

服务端把建会话、增加/移除成员、加入/退出会话、改名、已读和撤回交给
[控制写入服务](../server/services/control/service.py)，共 8 类操作。
`control_write_results` 按用户和请求标识保存处理结果，数据库升级自动新增该表并保留已有数据。
操作类型和内容摘要必须一致；复用请求标识改变操作或内容返回 409，原结果保留。
同一用户重新登录或换设备后重放原请求，仍返回已保存的确认，不重复执行业务或新增同步事件。

业务变化、同步事件和请求结果共同提交后发送确认；写入或最终提交失败返回 503，回滚后允许原请求重试。
确定的业务失败也保存原确认；之后权限变化不会使旧失败请求自动变成新操作。
结果记录当前随数据库保留，未实现自动清理；升级前未记录的历史请求无法补建原确认。
客户端已通过 [控制写入协调](../client/core/session/writecoordinator.cpp) 和
[控制写入存储](../client/core/session/writestore.cpp) 保存这些操作；原生方法返回成功表示本地保存完成。
同步追平后按控制队列顺序发送，同时等待 1 个请求；独立计时器每 500 毫秒检查，
距上次尝试至少 5000 毫秒才重试。队列与消息、文件任务共用按地址、用户、设备隔离的数据库，
本机断连或进程重启后沿用请求标识、原业务内容和首次时间，重新登录时更新认证会话。
重复建会话在本地也核对原 `client_conv_id` 和内容，避免重复生成请求。

保存失败返回拒绝；已保存但发送尝试或本地确认写入失败会保留原意图、停止队列并断开连接，
恢复数据库后可重新连接。401、408、429 和不小于 500 的错误保留为待重试，确定失败保存原结果，
后续新的用户操作使用新请求；重复确认不能改写已确认或确定失败的状态。
`initialStateLoaded.controlWrites` 与 `controlWritesChanged` 提供待处理和失败操作、次数及原因，
不向页面提供协议字节。页面等待完成回调后才关闭建群、私聊和加群表单，保存失败保留输入；
提交期间禁用重复操作。右侧“操作状态”显示已保存但待确认的请求和确定失败原因；
临时失败由 Qt 沿用原请求重试，确定失败后用户按原因重新操作。
已读在本地保存失败后可点击“标为已读”重试；原已读请求确认后继续提交后来读到的消息。
切换用户清空所属操作展示，回到原用户时从其数据库恢复。
验证分别见 [服务端控制写入批次](refactoring-progress.md#控制写入持久去重与事务失败回滚--2026-09-08)
和 [原生控制写入批次](refactoring-progress.md#原生控制写入队列与重启恢复--2026-09-08)；
页面验证见 [页面控制操作批次](refactoring-progress.md#页面控制操作与真实桌面验证--2026-09-08)。

## 同步补拉与等待

[同步协调组件](../client/core/sync/coordinator.cpp) 在登录成功后从本地连续位置请求历史，
每页最多 200 条事件，同时只保留 1 个活动请求；独立计时器每 500 毫秒检查，
距上次发送至少 5000 毫秒才以原请求标识和相同内容重试。
分页请求采用新的标识，从已提交的连续位置继续。

在线事件和补拉事件共用状态存储；当前请求追平且没有缺失事件时，才继续消息、控制写入与文件任务。
空页或无法推进连续位置的响应若仍有缺口，会报告 `unresolved gap` 并保留原请求重试；
同一请求只报告一次该缺口。这个状态下发送意图仍保存在本地，不能直接清除缓存或推进位置。
断开连接停止补拉；重新登录后读取已保存的位置并建立新请求。
数据库写入失败会停止同步及断开连接，未提交的事件不会通知页面。
验证范围见 [同步协调批次](refactoring-progress.md#同步协调与无进展页恢复--2026-09-07)。

## 群成员变更与历史已读

服务端根据发送时保存的 `message_deliveries` 计算每条消息的收件人数与已读人数，排除发送者。
新成员读取历史不会消耗原收件人的未读人数；成员离开期间没有该用户投递的消息也保持原计数。
退群和移除成员时，`conversation_read_history` 保存该用户在该会话的已读位置；主动加入或重新添加时恢复。
成员权限仍由 `conversation_members` 判断，退出后的历史位置不会授予发消息或提交已读的权限。
历史位置、成员变化与对应事件共同提交，重复已读不重新计数或改变首次已读、焚毁时间。

[初始化升级](../server/storage/repo/read_state.py) 在 `schema_migrations` 中用 `membership_read_history_v1` 标记一次性修复：
保留现有成员位置，从读者自己收到的历史已读事件恢复旧版本退群丢失的位置，按投递记录重算展示计数。
此处标记是 `schema_migrations` 表内 `name` 字段的值；重复启动跳过本次数据修复。
修复数据与标记共同提交，失败后可重新启动重试；新建的空表可能保留，原业务数据不清空。
旧库缺少对应历史事件时只能保留已有证据中的位置，不推测丢失的读取范围。
另一次升级 `read_count_events_v1` 为已有消息的每个原投递用户追加 ReadCountUpdated，
即按原收件记录计算的单条消息实际未读人数；发送者离群后仍收到自己历史消息的计数。
计数纠正事件与升级标记共同提交，失败回滚后重试，不改变已有事件标识、已读时间或客户端同步位置。
收到真实已读请求时，仅为首次读取的原收件投递产生计数事件，与业务变化及请求结果共同提交；
新成员读旧消息、重复读取和离群期间无投递的消息不会产生计数变化。

Qt 将计数独立保存为 `objects.kind=readCount`，初始状态的 `readCounts` 和 `messageUpdated.type=readCount`
向页面提供人数与事件位置；消息先到时采用消息初始人数，计数先到时保留计数，旧事件不能回退新结果。
`readCountKnown=false` 表示旧缓存缺少可信计数，页面暂时显示“已读状态同步中”，补拉纠正事件后恢复人数。
Receipt 继续保存用户的已读位置及计算当前用户未读总数；页面不再用其他读者的位置扣减每条消息的人数。
计数事件不携带正文，不改变已撤回或已焚毁状态。

本次新增 SyncEvent 字段号 16，服务端、Qt 和内嵌页面须配套升级；旧客户端不支持该事件，
不能把旧版本与本版服务端混用作为支持组合。大规模旧库升级耗时和新增事件容量尚未测量。

[成员已读回归](../server/tests/test_membership_reads.py) 随服务端全量测试执行；只运行该专项可使用：

~~~powershell
& ./.venv/Scripts/python.exe -m unittest discover server/tests -p test_membership_reads.py -v
~~~

真实网络成员变更、原收件人计数及数据库重开检查位于
[控制写入网络测试](../server/tests/test_control_write_network.py)，运行服务端全量测试即可覆盖。
服务端与原生/页面的验收边界见 [执行记录](refactoring-progress.md#跨端成员已读计数与旧缓存纠正--2026-09-10)。

## 收件确认与旧库升级

服务端 [SyncApplied 处理](../server/services/sync/service.py) 接受接收设备已保存的连续同步位置，
设备身份取已认证会话；`sync_applied_cursors` 按用户和设备保存位置，`control_write_results` 保存原请求结果。
同一请求与设备、内容绑定，内容或设备改变返回 409；超过该用户事件流的位置返回 400，数据库提交失败返回 503。
合法确认只把位置范围内属于该用户的 `sent` 投递改为 `delivered`，保持第一次送达时间；
`read`、`failed` 不因迟到确认回退。确认不改变已读人数或启动焚毁计时。
确认位置、业务状态、送达事件和成功确认结果共同提交，在线推送与补拉复用同一事件标识及内容。

`sent_at_ms` 为服务端消息入库时间；收件人未确认前 `delivered_at_ms` 为空。
已有已读操作也构成收件证据，缺少送达时间时使用已读处理时间补齐；发送者仍在入库时视为已读。
[数据库升级](../server/storage/sqlite/init_db.py) 发现缺少 `sent_at_ms` 的旧投递表时，
在同一个事务中补列、从原消息回填入库时间，并把无已读证据的旧 `delivered` 改为 `sent`、清空伪送达时间。
旧 `read` 记录及其已读时间保留，以已读时间作为可证实的送达时间；升级后再次启动不重置真实确认。
迁移失败时列与数据一起回滚，不删除原消息、会话或投递记录。

Qt 已接入 SyncApplied 和 DeliveryUpdated。送达对象按消息与收件用户保存，并与事件标识、连续位置共同提交；
初始快照的 `deliveries` 和实时 `messageUpdated` 的 `delivery` 对象提供页面展示。
状态表的 `metadata.sync_confirmation` 保存唯一待确认请求及固定位置，`sync_confirmed_cursor` 保存已获服务端确认的位置；
登录后的同步计时器每 500 毫秒检查，只为已提交且尚未确认的连续位置创建意图。
保存成功后才发送；已有请求未确认时不扩大其范围，5000 毫秒未确认沿用原请求及内容重试。
断连停止计时，重连及进程重启恢复原意图并更新认证会话；本地确认写入失败保留原意图并停止连接。
成功确认须匹配原请求位置，完成位置保存与清除待确认记录共同提交；重复确认无重复业务效果。
401 触发重新认证，408、429 和不小于 500 的错误等待重试；其他拒绝停止连接，保留原意图供排查。
收件确认独立于历史分页请求，可确认缺口之前已完整保存的前缀，不能确认缺口之后的在线消息。

发送者页面显示“等待送达”或“已送达”，群聊显示已确认送达人数；已读按既有已读位置优先展示，
撤回及焚毁消息不显示投递标记。设备重复确认不会增加人数，旧消息重放不会丢失已有送达对象；
账户切换隔离展示，重新登录从原用户缓存恢复。
协议修改后重新生成 Python 协议并重建 Qt；新服务端与本批客户端配套使用，旧 Qt 不支持新送达事件。
验证分别见 [服务端收件确认批次](refactoring-progress.md#服务端收件确认与旧库升级--2026-09-10)
和 [Qt 收件确认批次](refactoring-progress.md#qt-收件确认持久重试与送达展示--2026-09-10)。

## 文件任务与恢复

[文件协调组件](../client/core/file/coordinator.cpp) 管理本机任务、初始化、完成与取消确认、
临时文件接收状态及重试计时器；同步追平后才调度任务，断开时停止计时并清理临时内存状态。
其计时器每 500 毫秒检查待处理任务，消息队列停止自身重试计时器不会停止文件检查。

上传、下载先保存任务，再通过完成回调告知页面已接受；输入框在保存成功后清空，
保存失败时保留。文件接口未就绪或缺少对应方法时返回未接受，桌面初始化失败不能产生本地模拟上传。
同一文件消息的“填入下载”可重复点击，每次都重新填入源文件 ID；成功提交后再次下载属于新的用户意图。
文件任务与消息状态共用按地址、用户、设备隔离的 SQLite 数据库。
`fileTasksChanged` 更新本机待处理任务，`fileProgress` 展示服务端进度，两者分别使用。

断线或客户端进程重启后，Qt 在同步追平后沿用原 `client_file_id` 和初始化请求，
上传依据服务端核对后的已接收字节数继续，下载依据相邻临时文件的实际长度继续。
上传在“等待完成确认”期间重连，也先重新初始化原任务以核对服务端现存内容；
服务端已完成时直接恢复完成，未完成时补传后沿用原完成请求确认。
上传重试前重新核对源文件大小和摘要；文件变化或缺失时标记失败，恢复原文件后可点击重试。
下载必须完整校验后才替换目标文件；已经替换目标但尚未收到完成确认时，
重启后核对目标文件，再用原完成请求重试确认。

| 操作或情况 | 当前行为 |
| --- | --- |
| 控制确认超时 | 初始化或元数据等待 5000 毫秒后重建连接，再查询原任务；完成确认按原请求间隔至少 5000 毫秒重试 |
| 上传完成请求早于数据 | 服务端返回 503，保持原任务且不发布文件消息；数据收齐后以原完成请求重试，校验通过才完成 |
| 失败重试 | 保留原意图与路径；下载摘要错误后，显式重试从 0 重新下载；若目标已经完整且摘要正确，可核对目标后直接确认；上传须恢复原内容 |
| 取消 | 先保存取消请求并显示“取消待确认”，再关闭当前连接停止传输；其他任务随重连恢复，成功确认后移出列表 |

取消通过 `FileCancel` 提交原文件意图，服务端把取消记录、任务状态、同步事件和请求结果共同保存后返回确认。
初始化前也能保存取消记录，迟到的初始化、进度和完成不得恢复已取消任务；已完成文件返回 409。
等待取消确认时，断线、重启或临时错误均复用原请求，每次重试间隔至少 5000 毫秒；
取消请求不占用 8 个文件传输名额，排队任务可在其他传输暂停时完成取消。
确定失败显示“取消未成功”，任务保持停止；用户点击“重试取消”创建新取消请求，文件意图保持不变。
旧客户端仅在本机标记取消的缓存会迁移为待确认任务，并在连接后补交服务端。
取消保留临时文件、服务端文件和已有记录，不删除已发布消息；文件清理策略仍待实现。
取消协议要求客户端和服务端一起更新，旧服务端拒绝该请求时页面会保留取消失败状态。

同一上传意图同时由一个连接写入。[上传占用登记](../server/quic/upload.py) 只保存当前连接的写入权限，
文件意图与已提交进度仍在 SQLite 中。占用 = 某个连接获准从指定位置写入一条上传流。
同设备的后建连接可通过初始化接管，旧连接不能抢回仍活跃的占用；设备标识取已认证会话中的值。
其他设备在占用未释放且未过期时收到 429，Qt 保留原任务并重试；其他设备的进度推送不会停止本机初始化重试。
不同设备在连接断开、流重置或上传空闲达到 `MINIIM_FILE_STALE_MS` 后可重新初始化接管；
默认 900000 毫秒，0 禁用空闲超时接管。正常完整流结束后保留占用，直到完成请求处理、断开或接管，
避免文件等待完成确认期间被其他连接改写状态。成功取消也释放占用，并阻止迟到数据恢复文件。
初始化失败不转移占用；旧流或旧连接结束不释放新连接已经取得的占用。

上传流头为 `MINIIMFILE2 <file_id> <offset>\n`，`offset` 为十进制起始字节位置；
服务端必须先在同一连接确认 `FileInit`，再接受与获准偏移一致的一条流。
每批数据的当前位置须等于已提交进度，重复流、过期流、错误偏移、非法或截断流头均被拒绝。
头部换行前最多 512 字节，旧上传格式 `MINIIMFILE1` 不再接受；下载仍使用既有 `MINIIMFILE1 <file_id>\n`。
这次上传格式升级需同步重建客户端和更新服务端，原数据库和本地任务无需改写。
服务端恢复未完成上传时，续传位置取“磁盘实际长度、数据库已提交字节数、声明文件大小”三者最小值；
文件缺失从 0 开始，超过数据库位置的未提交尾部被截去。进度回退增加版本，恢复记录与同步事件共同提交。
`fsync` = 请求操作系统把文件数据写到存储设备。
上传每批数据须完整写入、刷新文件缓冲并调用 `fsync`，随后提交进度；短写继续写，零写入或存储错误不推进数据库位置。
上传流存储失败会关闭该连接，客户端保留原意图重连；初始化、完成阶段存储异常返回 503，允许原请求重试。
大小或摘要校验失败的未完成上传标记为 `failed_integrity`，位置归 0，待用户重试原任务时重新传输；
已发布文件不因续传被截断或回退，发布后的文件丢失与损坏仍需独立处理。
恢复路径已验证客户端进程终止、服务端文件截短/丢失、未提交尾部和注入的写盘/数据库失败；
同意图双设备竞争与接管已纳入真实 QUIC 和 Qt 回归；独立服务进程恢复见下方专项入口，机器断电与多设备完整业务流程仍需另行验收。
原意图恢复见 [文件恢复批次](refactoring-progress.md#文件任务持久恢复--2026-09-07)，
任务协调及独立重试见 [文件协调批次](refactoring-progress.md#文件任务协调与独立重试--2026-09-08)，
磁盘与进度恢复见 [文件存储恢复批次](refactoring-progress.md#文件续传与存储故障恢复--2026-09-08)，
取消确认见 [文件取消批次](refactoring-progress.md#文件取消确认与跨重启恢复--2026-09-09)，
连接占用及跨设备接管见 [上传接管批次](refactoring-progress.md#上传连接占用与多设备接管--2026-09-09)。

## 配置与排查

环境变量须在相应进程启动前设置；当前实现入口是
[服务端启动代码](../server/quic/server.py)、[Qt 窗口代码](../client/ui/mainwindow.cpp)
和 [Qt 会话管理](../client/core/session/sessionmanager.cpp)。

| 变量 | 默认值与用途 |
| --- | --- |
| `MINIIM_FILE_ROOT`、`MINIIM_FILE_STALE_MS` | 文件根目录见上表；任务过期及上传空闲接管阈值默认为 900000 毫秒，0 禁用基于超时的接管 |
| `MINIIM_BURN_ENABLED` | 默认开启；设为 `0` 关闭后不恢复已经清理的内容 |
| `MINIIM_BURN_SWEEP_INTERVAL_MS` | 默认每 1000 毫秒执行焚毁扫描 |
| `MINIIM_BURN_SWEEP_BATCH_SIZE`、`MINIIM_BURN_PURGE_BATCH_SIZE` | 每批默认分别处理 200 条到期投递和 200 条正文清理 |
| `MINIIM_FAULT_FILE_DROP_AFTER_BYTES`、`MINIIM_FAULT_FILE_DROP_PROBABILITY` | 文件故障注入阈值和概率，默认均为 `0`；仅在独立验证环境启用 |
| `MINIIM_WEB_URL`、`MINIIM_WEB_SMOKE_TEST` | 前者指定页面地址；设置后者时只显示 WebEngine 测试页面，无聊天交互 |
| `MINIIM_DEBUG_LOG` | 客户端默认写日志；设为 `0` 关闭 |
| `MINIIM_STATE_ROOT` | 覆盖客户端状态目录；默认值及分文件规则见上方数据表 |

客户端日志为可执行文件旁的 `mini_im_client.log`，服务端日志输出到终端。
定位业务状态问题时，依次核对数据库及同步记录、服务端发送、Qt 接收、接口事件和页面状态。

## 验证

下面各组命令分别检查服务端、页面和原生组件；每个命令退出码均须单独检查。
页面的 `build` 当前只做打包，不包含类型检查。

~~~powershell
& ./.venv/Scripts/python.exe -m unittest discover server/tests -v
npm --prefix ./web test
Push-Location ./web
& ./node_modules/.bin/vue-tsc.cmd --noEmit
npm run build
Pop-Location
cmake --build ./build/client_qt611 --config Release --target mini_im_client mini_im_download_tests mini_im_upload_tests mini_im_state_tests mini_im_sync_tests mini_im_control_tests mini_im_native_driver
ctest --test-dir ./build/client_qt611 -C Release --output-on-failure
~~~

服务端测试包含业务与存储入口、使用真实 aioquic 发送器与受控确认的下载调度检查，
以及 [控制写入网络测试](../server/tests/test_control_write_network.py) 的真实 QUIC 重连、确认丢失和提交失败回滚。
该网络入口还覆盖双设备争用同一上传意图、同设备新连接、过期接管、旧流/旧完成请求、流重置及错误流头；
页面测试检查状态合并、异步回调和接口缺失时拒绝操作；CTest 运行 Qt 下载、上传缓冲、同步协调、控制写入恢复和状态持久化测试，覆盖消息发送意图及文件任务。真实双客户端网络路径使用下述独立入口。
类型检查失败、未完成的网络场景及性能测量统一记入执行记录。

## 真实原生客户端联调

构建 `mini_im_native_driver` 后运行：

~~~powershell
& ./.venv/Scripts/python.exe ./tools/test_native_flow.py --client ./build/client_qt611/Release/mini_im_native_driver.exe
~~~

该入口通常自动启动两个使用桌面 Bridge 和原生库的 Qt 进程及临时 aioquic 服务端；多设备消息场景启动四个客户端。
通过随机本机端口传输业务数据，测试数据、证书均隔离。
驱动需要与桌面程序相同的运行时 DLL，包括 Qt Sql 和 SQLite 插件。
测试为每个客户端指定独立状态目录；命令接口只监听本机，退出时清理测试进程。
结果及日志保存到 `tmp/native-integration/<运行时间>/`，可用 `--output` 改变输出目录。

测试覆盖消息、文件、已接收状态和待确认消息的程序重启恢复，以及确认丢失、重复确认、换用户、自动重连、会话失效和响应超时；
还覆盖文件原意图续传、完成确认丢失、源文件变化、任务排队、账号隔离和服务端取消确认，
以及下载或登录等待期间的正常进程退出与重新连接、跨页补拉和空同步页恢复；
文件场景还注入仅消息发送尝试记录的写入错误，核对下载完成确认仍能重试；
上传场景覆盖服务端文件截短、丢失、未提交尾部、写盘与数据库提交失败，以及摘要失败后原任务重试；
取消场景覆盖初始化前取消、确认丢失、保存失败、确定失败后重试、用户切换和 8 条下载暂停时取消第 9 条排队任务；
同意图双设备上传检查把原任务复制到另一个隔离设备缓存，实际连接使用不同设备标识；
它验证第二设备等待、第一设备进度推送、第一进程终止和超时接管后沿原位置完成。
该用例只在隔离服务中把占用超时改为 1000 毫秒，不修改生产默认值。
多设备消息场景分别使用单聊和固定成员群聊，验证两名用户各两台设备的发送、并发已读、撤回、
接收方设备离线和进程重启，以及恢复快照、未读总数和持久事件去重。
可追加 `--test test_multidevice_direct_reads_recall_and_restart --test test_multidevice_group_reads_recall_and_restart`
仅运行这两个场景。这两个场景等待各设备收件确认完成后核对缓存。
成员变化另有 `test_membership_read_counts_use_original_recipients_and_survive_restart`，覆盖新加入、退出重入、
离群期间消息、发送者离群后的计数和重启；`test_read_count_migration_corrects_legacy_native_cache_without_resetting_cursor`
覆盖旧服务数据补发纠正事件和已有 Qt 缓存恢复，均可通过 `--test` 单独运行。
收件确认另有五项专项：离线收件及发送端恢复、确认丢失、确认意图跨进程恢复、同步缺口、本地保存失败。
服务进程重启由下述独立入口验证；Vue 完整桌面页面、其他多设备业务流程及传输性能需另外验收。
可在上述联调命令后追加 `--test test_process_restart_fills_persisted_sync_gap` 单独复核一个场景，
重复 `--test` 可选择多个场景；省略时执行全部。当前覆盖及运行结果统一见执行记录。

## 独立服务进程宕机联调

构建 `mini_im_native_driver` 后运行 [服务重启检查](../tools/test_server_restart.py)：

~~~powershell
& ./.venv/Scripts/python.exe ./tools/test_server_restart.py --client ./build/client_qt611/Release/mini_im_native_driver.exe
~~~

此入口先启动两个真实 Qt 原生客户端，焚毁场景另启动同用户的第二设备与新缓存设备，通过 [服务进程夹具](../tools/server_restart_fixture.py)
直接调用 [生产启动流程](../server/quic/server.py) 的 `run_server(data_root=..., port=...)`。
默认开发启动参数和数据位置保持原值；上述函数参数仅用于选择隔离目录与端口，首次启动可传 0 分配端口。
测试清除继承的 `MINIIM_` 配置，使用独立证书、数据库、文件目录和随机本机端口；
重启复用原目录和已分配端口，服务进程编号必须变化。

检查在消息/控制事务提交前、提交后但确认发送前、上传落盘但进度提交前等明确位置暂停回调，
由父进程强制终止服务，重启后检查回滚或保留结果、原请求重试、连续同步和完整文件内容。
同时覆盖下载临时文件续传、文件完成与取消、已读和撤回，以及新会话下的恢复。
下载初始化保持请求与文件意图标识，但续传位置及服务端补齐的元数据会变化，按该语义核对。

业务边界用例将隔离服务的心跳间隔设为 1 秒以缩短失联等待，默认配置用例保留 15 秒心跳。
这些场景均使用原生自动重连，不代替用户调用重连接口。
可追加 `--test test_upload_uncommitted_disk_tail_is_not_counted_after_crash` 单独运行一个场景，
重复 `--test` 选择多个；省略时执行全部。

输出位于 `tmp/server-restart/<运行时间>/`，`--output` 可指定父目录；
每个场景保存服务进程日志、请求与中断位置、客户端事件、`lifecycle.json` 和最终数据库副本。
`lifecycle.json` 记录进程编号、端口、退出码和是否被强制终止，`results.json` 汇总结果。
结束时关闭测试进程并清理临时运行目录；上述证据被 Git 忽略，不作为新环境复现前提。
焚毁专项可追加 `--test test_burn_scan_rolls_back_before_server_commit --test test_burn_scan_commit_survives_lost_push`。
驱动的 `message` 命令接受可选 `burnMode`、`burnTtlSec`，省略均为 0；专项通过真实接口发送 5 秒焚毁消息。
服务按默认 1000 毫秒间隔扫描，在实际修改投递的事务提交前、提交后但推送前暂停；不改写业务时间或伪造过期。
恢复后检查发送者已焚毁而未读接收者仍可读，同用户第二设备的重复已读不延长截止时间。
随后在接收者到期前终止服务和该用户一个客户端，等待真实时间跨过期限后，用原数据和缓存重启。
核对各设备正文清理、历史同步副本、连续事件位置与确认、重复扫描的稳定事件标识及首次焚毁时间。
额外证据 `bob-crash.json` 记录客户端退出码、停止时间与截止时间，`burn-scan` 日志证明后续空扫描实际执行。
验证不覆盖机器断电、磁盘损坏、其他业务的客户端与服务端同时宕机、
默认 15 分钟上传接管等待或完整 Qt WebEngine 页面；实际结果见 [焚毁恢复批次](refactoring-progress.md#焚毁计时跨进程恢复--2026-09-10)。

## 真实桌面页面联调

[桌面隔离环境](../tools/desktop_fixture.py) 启动真实 Qt WebEngine 客户端和临时 QUIC 服务，
使用随机本机端口、独立证书、服务端数据库和客户端状态目录；加载已打包的 `web/dist/index.html`。
运行前完成 Web 打包及 `mini_im_client` 构建，并准备包含 `plugins/platforms` 的 Qt 安装目录。
该入口只在测试环境开放本机页面调试端口，默认 900 秒后退出，也可在其终端按 Ctrl+C 停止。

~~~powershell
& ./.venv/Scripts/python.exe ./tools/desktop_fixture.py --qt-root D:/Qt/6.11.0/msvc2022_64 --timeout 900
~~~

保持上述终端运行，在另一终端将两个示例路径替换为本轮输出的 `context.json` 和已安装的
Playwright CLI JavaScript 入口；Playwright CLI = 通过命令行驱动浏览器及 Qt 内嵌页面的工具。
该检查需要 Node 和 Playwright CLI，不会自动安装依赖。

~~~powershell
& ./.venv/Scripts/python.exe ./tools/test_desktop_ui.py --context ./tmp/desktop-integration/<运行时间>/context.json --playwright-cli 'C:/tools/playwright-core/lib/tools/cli-client/cli.js'
~~~

[页面检查脚本](../tools/test_desktop_ui.py) 通过 [浏览器操作步骤](../tools/desktop_ui.js)
点击真实界面，检查完成回调等待、保存失败保留输入、成员操作、改名重试、撤回、已读及用户隔离，
以及文件取消待确认、临时错误后自动确认、确定失败后手动重试。
默认检查中的文件取消在初始化被临时拒绝时进行；实际内容传输与恢复使用下述文件专项。
建群检查只暂缓真实 Qt 返回值的交付以观察等待状态，不伪造保存结果；
服务端故障和数据库写入故障仅注入隔离环境，消息从真实服务经 QUIC 推送。
检查须使用全新隔离环境；结果与日志保存在 `tmp/desktop-integration/<运行时间>/`，
截图在 `output/playwright/<运行时间>/`，均被 Git 忽略。
`ui-result.json` 的 `ok` 必须为 `true`，每个阶段和数据库断言均通过才可计为验收。
文件专项使用另一个全新的夹具环境，在上述命令末尾追加 `--files-only`。
该模式上传 2,056,192 字节的隔离文件，暂停部分内容接收后终止桌面进程，再从同一缓存登录恢复；
下载也在临时文件已有数据时重启，并检查切换账号后的待处理任务隔离。
后续通过真实页面验证重复填入同一文件、摘要错误后的原任务重试、传输中取消及取消后重启。
脚本比对上传存储文件、源文件和下载目标的字节及 SHA-256，核对原意图、请求标识、续传偏移和单条文件消息。
SHA-256 = 根据文件内容计算的固定摘要，此处配合逐字节比较检查内容一致。
故障只作用于夹具数据：临时暂停文件流/下载调度、拒绝本地文件任务写入、改变隔离服务文件的一个字节。
失败及取消保留原目标；取消后的部分文件按当前策略保留，自动清理仍待实现。
`fileAttempts`、`fileTasks`、`transfers`、`artifacts` 与 `clientExits` 保存在结果中，记录各次实际调用和终止退出码。
两种模式都要求独立的全新环境；使用同一个已执行过测试的夹具不算有效复现。
本入口不覆盖同时运行的多设备、独立服务端进程宕机与其他平台；跨进程服务检查仍用前述独立入口。
本批验收与限制见 [桌面文件批次](refactoring-progress.md#真实桌面文件传输与恢复--2026-09-10)。

## 现有脚本与历史环境

| 入口 | 当前使用边界 |
| --- | --- |
| [generate_proto.py](../server/tools/generate_proto.py) | 使用调用它的 Python 环境内的 `grpc_tools.protoc` 生成协议并修正包内导入 |
| [bootstrap.ps1](../tools/bootstrap.ps1)、[check_env.ps1](../tools/check_env.ps1) | 仍含 Qt 6.8.0、旧构建目录或本机绝对路径；本轮统一前使用上文显式命令 |
| [dev_start.ps1](../tools/dev_start.ps1)、[dev_stop.ps1](../tools/dev_stop.ps1) | 启动入口使用全局 Python 和独立终端；停止入口使用与 PowerShell 内置变量冲突的 `$pid`，暂不作为推荐启停方式 |

已有评估环境把 Python 依赖放在被 Git 忽略的 `tmp/architecture-review-deps`。
只有该目录已存在时，才可在单独终端用下述方式复核；新环境仍按本指南建立 `.venv`：

~~~powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'tmp/architecture-review-deps'
python -m unittest discover server/tests -v
~~~

历史临时目录和旧 Qt 路径均不是新环境的前置条件。
