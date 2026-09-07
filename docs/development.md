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
| 检查改动 | [验证](#验证) · [真实原生客户端联调](#真实原生客户端联调) · [现有脚本与历史环境](#现有脚本与历史环境) |

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
cmake --build ./build/client_qt611 --config Release --target mini_im_client mini_im_download_tests mini_im_upload_tests mini_im_state_tests mini_im_sync_tests mini_im_native_driver
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

服务端当前固定监听 `127.0.0.1:4433`。在另一个终端启动已部署的客户端：

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
文件恢复方式见下节；建会话、成员变更、已读和撤回仍需补齐跨重启恢复。
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

## 同步补拉与等待

[同步协调组件](../client/core/sync/coordinator.cpp) 在登录成功后从本地连续位置请求历史，
每页最多 200 条事件，同时只保留 1 个活动请求；独立计时器每 500 毫秒检查，
距上次发送至少 5000 毫秒才以原请求标识和相同内容重试。
分页请求采用新的标识，从已提交的连续位置继续。

在线事件和补拉事件共用状态存储；当前请求追平且没有缺失事件时，才继续消息与文件任务。
空页或无法推进连续位置的响应若仍有缺口，会报告 `unresolved gap` 并保留原请求重试；
同一请求只报告一次该缺口。这个状态下发送意图仍保存在本地，不能直接清除缓存或推进位置。
断开连接停止补拉；重新登录后读取已保存的位置并建立新请求。
数据库写入失败会停止同步及断开连接，未提交的事件不会通知页面。
验证范围见 [同步协调批次](refactoring-progress.md#同步协调与无进展页恢复--2026-09-07)。

## 文件任务与恢复

上传、下载先保存任务，再通过完成回调告知页面已接受；输入框在保存成功后清空，
保存失败时保留。文件任务与消息状态共用按地址、用户、设备隔离的 SQLite 数据库。
`fileTasksChanged` 更新本机待处理任务，`fileProgress` 展示服务端进度，两者分别使用。

断线或客户端进程重启后，Qt 在同步追平后沿用原 `client_file_id` 和初始化请求，
上传依据服务端返回的已接收字节数继续，下载依据相邻临时文件的实际长度继续。
上传重试前重新核对源文件大小和摘要；文件变化或缺失时标记失败，恢复原文件后可点击重试。
下载必须完整校验后才替换目标文件；已经替换目标但尚未收到完成确认时，
重启后核对目标文件，再用原完成请求重试确认。

| 操作或情况 | 当前行为 |
| --- | --- |
| 控制确认超时 | 初始化或元数据等待 5000 毫秒后重建连接，再查询原任务；完成确认按原请求间隔至少 5000 毫秒重试 |
| 失败重试 | 保留原意图与路径；下载摘要错误后，显式重试从 0 重新下载；上传须恢复原内容 |
| 取消 | 先保存本机取消状态，再关闭当前连接停止传输；其他未取消任务随重连恢复，取消任务在本机重启后保持停止 |

取消作用于本机任务，保留临时文件和服务端已有记录；它不删除已发布的文件消息，
也未增加服务端取消状态协议。任务的服务端清理和暂停/取消状态同步仍见执行记录的剩余工作。
恢复路径已验证客户端进程终止；机器断电、磁盘数据损坏和真实服务端进程重启需另行验收。
本轮输入规模与验证证据见 [文件恢复批次](refactoring-progress.md#文件任务持久恢复--2026-09-07)。

## 配置与排查

环境变量须在相应进程启动前设置；当前实现入口是
[服务端启动代码](../server/quic/server.py)、[Qt 窗口代码](../client/ui/mainwindow.cpp)
和 [Qt 会话管理](../client/core/session/sessionmanager.cpp)。

| 变量 | 默认值与用途 |
| --- | --- |
| `MINIIM_FILE_ROOT`、`MINIIM_FILE_STALE_MS` | 文件根目录见上表；任务过期接管阈值默认为 900000 毫秒 |
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
cmake --build ./build/client_qt611 --config Release --target mini_im_client mini_im_download_tests mini_im_upload_tests mini_im_state_tests mini_im_sync_tests mini_im_native_driver
ctest --test-dir ./build/client_qt611 -C Release --output-on-failure
~~~

服务端测试包含业务与存储入口，以及使用真实 aioquic 发送器、受控确认和丢包的下载调度检查；
页面测试检查状态合并；CTest 运行 Qt 下载、上传缓冲、同步协调和状态持久化测试，覆盖消息发送意图及文件任务。真实双客户端网络路径使用下述独立入口。
类型检查失败、未完成的网络场景及性能测量统一记入执行记录。

## 真实原生客户端联调

构建 `mini_im_native_driver` 后运行：

~~~powershell
& ./.venv/Scripts/python.exe ./tools/test_native_flow.py --client ./build/client_qt611/Release/mini_im_native_driver.exe
~~~

该入口自动启动两个使用桌面 Bridge 和原生库的 Qt 进程及临时 aioquic 服务端；
通过随机本机端口传输业务数据，测试数据、证书均隔离。
驱动需要与桌面程序相同的运行时 DLL，包括 Qt Sql 和 SQLite 插件。
测试为每个客户端指定独立状态目录；命令接口只监听本机，退出时清理测试进程。
结果及日志保存到 `tmp/native-integration/<运行时间>/`，可用 `--output` 改变输出目录。

测试覆盖消息、文件、已接收状态和待确认消息的程序重启恢复，以及确认丢失、重复确认、换用户、自动重连、会话失效和响应超时；
还覆盖文件原意图续传、完成确认丢失、源文件变化、任务排队、账号隔离和本机取消，
以及下载或登录等待期间的正常进程退出与重新连接、跨页补拉和空同步页恢复；
Vue 完整桌面页面、其他写入的跨重启恢复及并发性能需另外验收。
可在上述联调命令后追加 `--test test_process_restart_fills_persisted_sync_gap` 单独复核一个场景，
重复 `--test` 可选择多个场景；省略时执行全部。当前覆盖及运行结果统一见执行记录。

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
