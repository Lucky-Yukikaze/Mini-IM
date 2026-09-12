# Mini-IM 开发指南

返回 [项目入口](../README.md)。命令默认从项目根目录的 PowerShell 执行。
架构和编码规则见 [AGENTS.md](../AGENTS.md)，
本机环境、已执行检查与遗留问题见 [重构执行记录](refactoring-progress.md)。
以下安装与配置步骤按代码整理；已实际执行的组合和结果以执行记录为准。
本机已有目录的增量构建不能证明新环境安装成功；新构建目录的本机验证也不代表重新安装了 Qt、编译器或 vcpkg。

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
更新依赖时一并提交声明和锁文件；使用 `npm --prefix ./web audit` 检查当前已知告警，
再用 `npm --prefix ./web ci`、类型检查、页面测试、打包与受影响的桌面专项验收升级结果。
审计结果只覆盖当次依赖及公告库，日期和结果记录在执行记录中。
C++ 依赖清单 = 由 [client/vcpkg.json](../client/vcpkg.json) 声明直接依赖及版本基线，配置时自动解析并安装所需包。
vcpkg 基线 = 该工具源码仓库中记录整套包版本的固定提交；更新基线须重新验证客户端。
当前清单以已验证的 MsQuic、Protobuf 组合为起点，版本及验收范围见执行记录。

创建项目虚拟环境并安装 Python 与 Web 依赖：

~~~powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install -r ./server/requirements.txt
npm --prefix ./web ci
~~~

首次安装 vcpkg 时使用清单中的提交版本引导工具；已有可用 vcpkg 的工作目录可跳过以下步骤，
清单仍按固定基线选择包。更新现有工具前检查其本地改动，不由构建脚本自动切换版本：

~~~powershell
git clone https://github.com/microsoft/vcpkg ./thirdparty_install/vcpkg
$dependencyManifest = Get-Content -Raw -Encoding UTF8 ./client/vcpkg.json | ConvertFrom-Json
git -C ./thirdparty_install/vcpkg checkout --detach $dependencyManifest.'builtin-baseline'
& ./thirdparty_install/vcpkg/bootstrap-vcpkg.bat
~~~

依赖和 Qt 安装完成后，可使用统一入口；脚本从自身位置定位项目，参数中的相对路径均相对项目根目录。

~~~powershell
$env:QT_DIR = 'D:/Qt/6.11.0/msvc2022_64'
& ./tools/check_env.ps1 -Prerequisites
& ./tools/bootstrap.ps1 -BuildDirectory build/client-manifest
~~~

[环境检查](../tools/check_env.ps1) 的 `-Prerequisites` 检查系统 Python、Node/npm、CMake、Qt 模块及可用 vcpkg 工具，无需预先安装 C++ 包；
默认还检查 `.venv` 及协议生成依赖，`-RuntimeOnly` 仅检查服务和 Web 运行所需入口。
缺失依赖或命令失败会抛出错误，使用 `pwsh -File` 调用时返回非零退出码。
[安装构建入口](../tools/bootstrap.ps1) 在项目 `.venv` 安装固定 Python 直接依赖，用 `npm ci` 安装锁定 Web 依赖，
生成协议、执行类型检查和打包，再配置 Visual Studio 2022/x64，按清单安装 C++ 依赖、构建 Release、部署 Qt 并执行组件测试。
默认构建目录为 `build/client-manifest`，依赖位于该目录的 `vcpkg_installed`，不复用工具目录中的全局安装树。
旧构建目录若使用过非清单模式，应另选新目录；vcpkg 不支持直接切换既有目录的模式，脚本不会删除旧构建。
网络或依赖安装失败会中止配置；已有二进制缓存可被 vcpkg 复用，不能据此宣称已验证无缓存源码安装。
`-QtRoot` 或 `QT_DIR` 指定 Qt；`-VcpkgRoot` 或 `VCPKG_ROOT` 指定 vcpkg，默认 `thirdparty_install/vcpkg`。
`-Python` 选择创建虚拟环境的解释器，`-VenvPath` 选择虚拟环境；`-SkipInstall` 要求 Python 与 Web 依赖已经安装，不执行 pip 或 npm 安装；CMake 仍检查并安装清单中的 C++ 依赖。
入口不安装 Qt、Visual Studio 或 vcpkg，也不提前创建业务数据库；数据库由服务启动时初始化。

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
cmake -S ./client -B ./build/client-manifest -G "Visual Studio 17 2022" -A x64 "-DCMAKE_TOOLCHAIN_FILE=$qtToolchain" "-DVCPKG_TARGET_TRIPLET=x64-windows" "-DCMAKE_PREFIX_PATH=$env:QT_DIR" -DBUILD_TESTING=ON
cmake --build ./build/client-manifest --config Release --target mini_im_client mini_im_download_tests mini_im_upload_tests mini_im_state_tests mini_im_sync_tests mini_im_control_tests mini_im_native_driver
& "$env:QT_DIR/bin/windeployqt.exe" --release --compiler-runtime --dir ./build/client-manifest/Release ./build/client-manifest/Release/mini_im_client.exe
~~~

`windeployqt` = 将 Qt 运行时和插件复制到程序目录的部署工具；
MsQuic、Protobuf 等非 Qt 依赖由所用依赖配置提供，运行前需保证其 DLL 可被找到。
部署前先退出使用该构建目录的客户端和测试驱动；出现 DLL 无法覆盖时，处理占用后重新执行部署。
统一脚本从 CMake 记录的 Visual Studio 安装目录设置临时 `VCINSTALLDIR`，部署后恢复原值；
输出的 `vc_redist.x64.exe` 是运行库安装程序，目标机器仍需具备相应运行库，不能只凭本机构建证明空机器部署完成。
Qt 部署结果应包含 `Qt6Sql.dll` 和 `sqldrivers/qsqlite.dll`；缺少 SQLite 驱动会使缓存打开失败。
打包页面后，客户端可直接读取 `web/dist/index.html`。

## Windows 独立桌面包

[打包入口](../tools/package_desktop.ps1) 使用已配置的 Visual Studio 2022/x64 Release 构建目录与对应 Qt kit，
先执行 Web 类型检查、页面构建和客户端构建，再创建独立发布目录。准备构建环境仍使用上方安装构建入口。
`-QtRoot` 必须匹配 CMake 缓存；默认从 CMake 记录的 Visual Studio 目录查找 x64 C++ 可分发运行库，
从 Windows SDK 注册表路径的 `Redist/D3D/x64` 查找 `dxcompiler.dll`、`dxil.dll`。
SDK 非标准安装时用 `-WindowsSdkRoot` 指定包含 `Redist/D3D/x64` 的根目录。

~~~powershell
& ./tools/package_desktop.ps1 -QtRoot 'D:/Qt/6.11.0/msvc2022_64' -OutputDirectory build/packages/mini-im-release
& ./build/packages/mini-im-release/mini_im_client.exe
~~~

默认输出 `build/packages/mini-im-<时间>/`；指定目录必须尚不存在，脚本不覆盖既有包，也不自动清理失败输出。
包内包含桌面可执行文件、Qt 插件及 WebEngine 资源、应用依赖 DLL、C++ 运行库、图形编译库和 `web/dist`，
不打包测试程序或服务端。`qt.conf` 使用包内相对路径；客户端优先加载自身目录的 `web/dist/index.html`，再查找开发目录。
DLL = 程序运行时加载的共享库文件；运行无需额外配置本机 Qt、vcpkg 或编译器路径。
`package-manifest.json` 记录源码提交、工作区状态、逐文件大小与 SHA-256；清单自身不计入其文件列表。
发布目录可整体复制到含空格的新位置，从包内运行程序；连接服务端和开发认证约定见下节。

独立包验收使用 [桌面夹具](../tools/desktop_fixture.py) 的 `--portable`：清理 Qt/QML 环境变量、
把 `PATH` 限定为 Windows 目录及其 `System32`，并在隔离临时目录中启动客户端。
`--platform windows` 使用 Windows 原生窗口，默认 `offscreen` 用于离屏交互检查；两者均要求加载包内页面。

~~~powershell
& ./.venv/Scripts/python.exe ./tools/desktop_fixture.py --portable --platform windows --client ./build/packages/mini-im-release/mini_im_client.exe
~~~

按 [真实桌面页面联调](#真实桌面页面联调) 的控制与文件入口使用生成的 `context.json`；无需 `--qt-root`。
当前证据覆盖本机 Windows 11 独立目录、迁移目录、模块来源与真实页面交互；无 Qt/编译器的全新机器仍需单独验证。
结果见 [独立桌面包批次](refactoring-progress.md#windows-独立桌面包与迁移验收--2026-09-12)。

## 运行与数据

在一个终端启动服务端：

~~~powershell
& ./.venv/Scripts/python.exe ./server/main.py
~~~

默认开发入口监听 `127.0.0.1:4433`。在另一个终端启动已部署的客户端：

~~~powershell
& ./build/client-manifest/Release/mini_im_client.exe
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

也可用 [开发启动](../tools/dev_start.ps1) 和 [开发停止](../tools/dev_stop.ps1) 管理后台服务：

~~~powershell
& ./tools/dev_start.ps1
& ./tools/dev_stop.ps1
~~~

启动使用 `.venv` Python 和本地 Vite，后台窗口隐藏；服务就绪及页面 HTTP 响应通过后才报告成功，最多等待 20 秒。
默认服务端口 4433、页面端口 5173；`-ServerPort`、`-WebPort` 可覆盖，`-DataRoot` 默认 `server`。
`-RunDirectory` 默认 `tmp/dev`，保存日志和进程记录；停止时使用相同目录。
进程记录核对程序路径和开始时间，拒绝过期身份；重复启动不停止已有服务，启动失败清理本次启动的进程。
停止会强制终止匹配的进程及其已识别子进程，释放端口并移除记录，保留业务数据和日志。
旧 `tools/.dev_processes.json` 缺少身份信息，不自动导入；旧窗口仍需由原终端停止。
服务命令行另支持 `--data-root` 与 `--port`，默认行为保持原值，端口有效范围为 1 至 65535。

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
| 同一消息意图内容冲突 | 同会话、同用户复用 `client_msg_id` 但正文、类型或有效焚毁设置变化时返回 409，保留已有消息；客户端保留失败项，重试继续原意图 |

服务端 `messages.intent_fingerprint` 保存 32 字节 SHA-256 请求摘要；摘要 = 对请求业务字段计算的固定校验值，
此处不额外保存一份明文正文。发送者身份和会话/意图标识共同限定查找范围。
未指定类型按文本处理，关闭焚毁时忽略时长，系统消息忽略焚毁设置；未知协议字段不参与已解释的业务身份。
摘要在全局焚毁开关处理前计算，因此新记录可跨开关切换重试；正文清理后仍比较摘要，不重新发布正文或消息。
旧库首次启动时新增列并对未清理正文的消息回填，列变更与回填共同提交，失败可用原库重试。
旧版本已清理的正文无法恢复原摘要，复用返回 409 和 `original message intent unavailable after legacy content purge`。
旧版本因全局开关或默认处理而丢弃的原始参数同样无法追溯，回填使用数据库中的有效设置；
携带已丢弃参数的旧请求可能返回冲突，不能据此修改已保存消息或恢复已焚毁内容。
范围与验证见 [消息意图一致性](refactoring-progress.md#消息意图内容一致性--2026-09-10)。
消息发送同时通过 `control_write_results` 保存用户、请求 ID、操作、内容摘要与原确认。
同请求 ID 改用另一消息意图或复用为已接入该仓储的其他操作，返回 409；反向复用也拒绝。
确定失败保存原结果，修正输入属于新操作，应使用新请求；数据库提交错误返回可重试的 503 协议错误。
旧消息尚无请求结果时，先按发送者和请求 ID 核对原消息摘要；多个旧消息占用同一请求 ID 或原摘要未知时拒绝。
单条可验证旧消息首次成功重放才保存结果，无法恢复旧版本当时未保存的确认时间。
文件初始化通过独立身份记录检查消息/控制请求占用，并保留重新执行恢复的能力；文件完成请求复用共享请求结果存储，规则见 [文件任务与恢复](#文件任务与恢复)。
初始化已占用的 ID 不能用于消息、控制或文件完成。
本批验证见 [消息请求结果](refactoring-progress.md#消息请求结果持久恢复--2026-09-10)。

重连失败后的等待时间依次为 1000、2000、4000、8000、16000、30000 毫秒，随后保持 30000 毫秒；
登录成功后重新从 1000 毫秒计时。每次连接尝试等待登录最多 10000 毫秒，超时后关闭连接再重试。
心跳按服务端给定间隔运行，0 使用 15 秒，超过 300 秒按 300 秒处理；
每次心跳检查距最近已解析响应的时间，达到两个心跳间隔则触发重连。
控制流关闭或复位也会触发恢复，页面等待状态为 `reconnecting`。
控制帧 = 4 字节大端正文长度加一条 Protobuf Envelope；正文允许 1 至 16777216 字节。
服务端从客户端首个双向流接收控制帧，后续流交给文件处理；单向流不能作为初始控制流。
拆包时保留未收齐数据，合包时按顺序处理完整帧；非法长度、无法解析的 Protobuf 或控制流中途结束会关闭连接。
服务端关闭控制流时清空控制缓冲、注销在线推送并释放上传占用；完整控制流结束或对端要求停止发送同样关闭连接。
Qt 遇到非法帧使用已有重连流程，已保存的待确认消息沿用原请求重试；截断消息没有服务端提交结果，不能计为成功。
边界与恢复验证见 [协议批次](refactoring-progress.md#控制帧边界与异常连接恢复--2026-09-11)。

队列和失败原因保存在上方客户端状态目录，切换用户分别恢复各自记录。
发送确认仅说明服务端已处理，不能据此推断接收方已收到或已读。
消息保存和页面检查见 [发送意图验证](refactoring-progress.md#消息发送意图与确认恢复--2026-09-07)，
自动重连见 [连接恢复验证](refactoring-progress.md#自动重连与会话恢复--2026-09-07)。

## 服务端业务写入入口

[共享写队列](../server/storage/sqlite/write_queue.py) = 同一事件循环中按接收顺序执行完整业务操作的入口。
所有连接通过同一个 `OnlineSessionHub.writes` 排队处理控制请求、文件流和连接事件；
焚毁扫描也使用这个入口，启动建库及迁移在监听开始前完成。
操作调用现有服务与仓储完成事务，事务提交后再发送确认或推送；队列不增加外层事务。
任务必须同步完成，不能在事务中使用 `await`；单个任务失败会通知调用方，后续任务继续处理。

`enqueue(operation, payload_bytes=0)` 接受任务并返回可等待的结果；`submit(operation)` 等待结果且保护已接受任务免受调用者取消影响。
`stop()` 停止接收并处理完已接受任务，随后服务关闭传输和数据库；停机排空期间的晚到请求不再接受。
排队内容仅在内存中，进程宕机后依靠客户端保存的原请求与服务端持久结果恢复。
文件流结束标记在收到事件时保存，避免排队期间 QUIC 库清理流后再次要求停止该流。

默认最多等待 1,024 个操作、33,554,432 字节网络数据，任一限制超出即抛出 `WriteQueueFull`，该操作未被接受。
限制由 `SqliteWriteQueue` 构造参数指定，生产使用默认值，暂无环境变量配置；这些是保护阈值，尚未测定系统支持容量。
操作开始执行即释放队列预算，因此不包含正在执行的一项；字节数仅累计等待中的 `StreamDataReceived.data`，
不包含帧解析缓冲、文件缓冲、传输库内部缓存或 Python 对象开销，不能当作进程内存上限。

过载 = 新业务操作超过上述等待容量而无法入队。发生后停止接受该连接的新业务，
等待该连接已接受的操作执行完毕，再以 `write_queue_overloaded` 关闭连接，不为关闭额外占用队列。
客户端以已保存的原请求和消息/文件意图恢复；上传从服务端确认的持久位置续传，未提交缓冲不算进度。
上传定时提交遇到满队列也走同一路径；焚毁扫描遇到拒绝会记录错误，在下一轮重新尝试。
数据库、文件写盘和摘要计算仍在事件循环中同步执行；持续负载、公平性和其他缓冲容量继续待验证。
容量与恢复检查见 [队列容量批次](refactoring-progress.md#共享队列容量与过载恢复--2026-09-12)。
验证见 [共享写入批次](refactoring-progress.md#服务端共享业务写入队列--2026-09-11)；
[队列测试](../server/tests/test_write_queue.py) 与 [真实连接检查](../server/tests/test_control_write_network.py)
均由下方 [服务端验证命令](#验证) 执行。

## 控制写入与重试边界

服务端把建会话、增加/移除成员、加入/退出会话、改名、已读和撤回交给
[控制写入服务](../server/services/control/service.py)，共 8 类操作。
消息发送、文件取消及收件确认也使用同一个请求结果仓储；客户端仍由各业务队列管理重试。
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

服务端 `file_init_requests` 按用户/请求 ID 保存初始化身份摘要，与任务创建或恢复共同提交。
上传身份包含文件意图、会话、名称、大小、大小写统一后的 SHA-256、方向与来源；
下载身份包含文件意图、方向与来源，名称/大小/摘要由来源文件决定，指定会话仍由服务校验。
续传偏移和优先级可调整；每次初始化重新检查任务和文件并返回最新进度，不能用旧响应跳过重新建立传输。
未成功初始化不新增绑定；已接受的请求永久保留占用，当前尚无自动清理策略。
旧库先依据文件任务当前保留的请求 ID 核对身份；更早已被旧版本覆盖的请求 ID 无法追溯。
旧库首次换请求续传时，须在同一事务保存原请求绑定；有歧义的旧请求不能重新分配。
验证与升级边界见 [初始化请求身份](refactoring-progress.md#文件初始化请求身份--2026-09-10)。

完成请求通过 `control_write_results` 保存操作、内容摘要和原确认，文件状态、文件消息及事件共同提交。
同一用户的请求 ID 绑定文件 ID、成功标志、确认字节数与统一小写的摘要；跨操作或内容改变返回 409。
成功及明确失败结果持久保存；数据尚未收齐的 503 和存储异常允许原请求重试，不留下半完成记录。
重复请求返回原确认，不重新产生事件；附带的文件状态取当前任务，避免重放旧进度。
Qt 将明确完成拒绝保存为 `finishRejected`；用户点击重试时，在同一次本地写入中更新完成请求编号、待处理状态和拒绝标志。
文件意图、初始化请求、路径和元数据保持原值；旧完成请求不再映射到新尝试，保存失败保留原状态。
确认丢失或暂时错误仍保留原完成请求。新旧客户端应配套升级：旧客户端明确失败后继续原请求会持续收到原拒绝。
旧库沿用已有结果表；升级前未保存的完成请求内容及确认无法还原，首次到达新服务时按现存任务处理并保存结果。
验证范围见 [完成请求持久结果](refactoring-progress.md#文件完成请求持久结果--2026-09-10)。

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
下载初始化在校验会话成员权限后核对服务端源文件；文件丢失、非普通文件或大小不符时返回 409，
页面任务显示失败并保留重试入口，服务端不创建新的下载记录或修改已发布消息。
服务端文件恢复为原内容后，可对原任务点击重试；失败任务及请求身份在客户端重启后保留。
传输中源文件提前结束时，服务端重置该文件流，Qt 将对应任务保存为失败；连接中断仍按重连恢复处理。
同长度内容损坏由下载端 SHA-256 校验检出；显式重试从 0 下载，避免重复使用损坏片段。
下载必须完整校验后才替换目标文件；已经替换目标但尚未收到完成确认时，
重启后核对目标文件，再用原完成请求重试确认。
这些检查不提供丢失内容的副本；恢复原内容仍需可用的原始文件或备份，已完成上传不能通过旧请求覆盖。

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
服务端接收端每批最多提交 65,536 字节；不足一批的数据首次缓冲后安排 20 毫秒定时提交，收齐声明大小或流结束也立即提交。
20 毫秒是定时触发间隔，实际执行仍需等待共享业务队列；不作为最长延迟保证。
网络回调结束时仍保留的文件正文不足 65,536 字节；等待网络事件另受 [共享写队列](#服务端业务写入入口) 的操作数与字节限制。
定时回调重新检查流状态对象和上传占用，取消、重置、连接关闭会丢弃未提交缓冲；其他设备取消时立即要求发送端停止该流。
服务进程宕机或停机时尚未进入写入队列的内存字节不计入恢复位置，原任务从已提交位置重传。
已收齐数据但尚未结束流仍可提交，完成请求先到时继续返回可重试结果；批量处理不改变确认和原请求身份。
批次结果见 [上传批量提交](refactoring-progress.md#上传进度批量提交--2026-09-12)。
上传流存储失败会关闭该连接，客户端保留原意图重连；初始化、完成阶段存储异常返回 503，允许原请求重试。
大小或摘要校验失败的未完成上传标记为 `failed_integrity`，位置归 0，待用户重试原任务时重新传输；
已发布文件不因续传被截断或回退，发布后的文件丢失与损坏使用下方离线维护入口处理。
恢复路径已验证客户端进程终止、服务端文件截短/丢失、未提交尾部和注入的写盘/数据库失败；
同意图双设备竞争与接管已纳入真实 QUIC 和 Qt 回归；独立服务进程恢复见下方专项入口，机器断电与多设备完整业务流程仍需另行验收。
原意图恢复见 [文件恢复批次](refactoring-progress.md#文件任务持久恢复--2026-09-07)，
任务协调及独立重试见 [文件协调批次](refactoring-progress.md#文件任务协调与独立重试--2026-09-08)，
磁盘与进度恢复见 [文件存储恢复批次](refactoring-progress.md#文件续传与存储故障恢复--2026-09-08)，
取消确认见 [文件取消批次](refactoring-progress.md#文件取消确认与跨重启恢复--2026-09-09)，
连接占用及跨设备接管见 [上传接管批次](refactoring-progress.md#上传连接占用与多设备接管--2026-09-09)。

### 离线文件检查、恢复与清理

[维护入口](../tools/maintain_files.py) 使用已有数据库及文件目录，数据库只读，不执行初始化或迁移。
先停止服务；使用开发脚本启动的服务可通过 `tools/dev_stop.ps1` 停止。
`--data-root` 默认 `server`，文件目录依次使用 `--file-root`、`MINIIM_FILE_ROOT`、`DATA_ROOT/storage/files`，必须与服务实际配置一致。
全局目录参数写在 `inspect`、`restore`、`clean-cancelled` 子命令之前；下例文件 ID、备份路径和 7 天保留期均按实际需要替换。

~~~powershell
# 已发布文件核对大小与 SHA-256，未完成上传只报告现存字节。
& ./.venv/Scripts/python.exe ./tools/maintain_files.py --data-root ./server inspect
# 从检查结果选择 fileId；先验证替换内容和目标，默认预览不替换。
$repairFileId = '替换为实际文件ID'
$repairSource = 'D:/backups/original.bin'
& ./.venv/Scripts/python.exe ./tools/maintain_files.py --data-root ./server restore --file-id $repairFileId --source $repairSource
& ./.venv/Scripts/python.exe ./tools/maintain_files.py --data-root ./server restore --file-id $repairFileId --source $repairSource --apply
# 预览达到所选保留期的已取消上传；确认结果后显式执行。
& ./.venv/Scripts/python.exe ./tools/maintain_files.py --data-root ./server clean-cancelled --older-than-days 7
& ./.venv/Scripts/python.exe ./tools/maintain_files.py --data-root ./server clean-cancelled --older-than-days 7 --apply
~~~

维护和服务共用 [存储锁](../server/storage/access.py)，分别占用解析后的数据目录与文件目录。
同目录的第二个服务、在线维护及维护期间启动服务都会被拒绝；退出或被终止后自动释放。
`.miniim-storage.lock` 是持久锁文件 = 文件保留但占用随进程释放；它被 Git 忽略，不能通过删除文件解锁。
工具会创建所需的目录和锁文件，预览不改动业务记录或文件正文。
旧版服务和直接构造业务对象的脚本没有该锁，运行维护前也须停止；锁不能阻止外部编辑器或其他程序直接写文件。

`restore` 只恢复已完成上传；原始副本及临时复制文件都必须满足原大小与摘要。
临时文件位于目标同目录，写盘后再次校验，再替换目标；目标本来正确时返回 `already-valid`。
校验或写盘失败保留原目标，不创建新消息或新文件任务；重新启动服务后，页面对原失败任务点击重试即可。
异常退出可能留下 `.miniim-restore-*` 临时文件，本入口不自动删除这些文件；尚未验证替换边界的机器断电恢复。

清理只处理方向为上传、状态为 `cancelled` 且有匹配取消记录的文件。
取消时间与任务更新时间均须早于或等于当前时间减去所选天数，1 天按 86,400,000 毫秒计算；0 表示不额外保留，负数拒绝。
消息、附件、其他传输来源或共享路径任一引用存在就保留；数据库中的取消、任务、请求和同步记录始终保留，迟到重试仍被拒绝。
重复清理已不存在的文件返回 `already-absent`；单文件删除失败记为 `error` 并继续处理后续候选。
活动及失败上传、已发布文件、未登记文件、客户端下载片段保持原样；这些数据的自动清理需另行定义保留策略。
维护拒绝越界、链接及非普通文件路径，不跟随数据库路径删除目录。

命令以 JSON 输出结果：正常结果返回退出码 0；初始化、参数值或占用错误返回 1；检查发现缺失/损坏或逐文件处理出错返回 2。
未加 `--apply` 的恢复和清理只输出候选，不代表已经执行；解析器拒绝的命令语法错误使用其标准错误输出。
验证记录见 [离线维护批次](refactoring-progress.md#离线文件维护与保留策略--2026-09-12)。

## 客户端已取消下载的清理

连接账号后，在聊天页右侧“文件”区域点击“清理已取消下载”；核对文件名、字节数和完整路径，再点击“确认清理”。
预览只列出当前账号本地已确认取消、具有取消请求身份且未被活动任务占用的下载片段。
[Qt 清理实现](../client/core/file/cleanup.cpp) 从任务保存的目标路径推导对应 `.miniim-<文件 ID>.part`，不递归扫描目录。
正式目标、其他任务引用的文件，以及经过符号链接或目录联接的路径不会进入清理列表。

确认时再次核对任务、文件大小和修改时间；列表变化或删除失败会报告错误，须重新预览。
预览凭据仅使用一次，断开连接、切换账号或重启后须重新预览。
清理保留本地任务及服务端取消、请求记录；清理后重启不会重新发起已取消下载。
失败、暂停、待确认取消及未登记片段继续保留；没有按时间自动删除的后台任务。
外部程序同时替换文件的竞态和机器断电恢复尚未验收，文件元数据复核不等于内容摘要校验。
验证证据见 [客户端清理批次](refactoring-progress.md#客户端已取消下载清理--2026-09-12)。

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
页面的 `build` 当前只做打包，不包含类型检查；统一验证入口会单独执行类型检查。

~~~powershell
& ./tools/verify.ps1 -QtRoot 'D:/Qt/6.11.0/msvc2022_64' -BuildDirectory build/client-manifest
& ./tools/verify.ps1 -QtRoot 'D:/Qt/6.11.0/msvc2022_64' -BuildDirectory build/client-manifest -Integration
~~~

[统一验证](../tools/verify.ps1) 复用已有 Python/Web 依赖，依次执行协议生成、类型检查、清单依赖检查与安装、
构建部署、组件测试、服务端与页面测试、桌面快照回归及开发脚本验收。
`-Integration` 继续运行真实原生网络与独立服务宕机全量，使用本次构建目录中的驱动；不会替代完整桌面交互或性能验收。
[开发脚本验收](../tools/test_dev_scripts.ps1) 可独立运行，使用随机端口和 `tmp/dev-script-tests/` 隔离数据，
检查路径引用、缺失环境、命令失败、重复启动、过期进程身份、停止释放端口及占用端口时的清理。

~~~powershell
& ./.venv/Scripts/python.exe -m unittest discover server/tests -v
npm --prefix ./web test
Push-Location ./web
& ./node_modules/.bin/vue-tsc.cmd --noEmit
npm run build
Pop-Location
cmake --build ./build/client-manifest --config Release --target mini_im_client mini_im_download_tests mini_im_upload_tests mini_im_state_tests mini_im_sync_tests mini_im_control_tests mini_im_native_driver
ctest --test-dir ./build/client-manifest -C Release --output-on-failure
~~~

服务端测试包含业务与存储入口、使用真实 aioquic 发送器与受控确认的下载调度检查，
以及 [控制写入网络测试](../server/tests/test_control_write_network.py) 的真实 QUIC 重连、确认丢失和提交失败回滚。
该网络入口还覆盖双设备争用同一上传意图、同设备新连接、过期接管、旧流/旧完成请求、流重置及错误流头；
页面测试检查状态合并、异步回调和接口缺失时拒绝操作；CTest 运行 Qt 下载、上传缓冲、同步协调、控制写入恢复和状态持久化测试，覆盖消息发送意图及文件任务。真实双客户端网络路径使用下述独立入口。
类型检查失败、未完成的网络场景及性能测量统一记入执行记录。

## 真实原生客户端联调

构建 `mini_im_native_driver` 后运行：

~~~powershell
& ./.venv/Scripts/python.exe ./tools/test_native_flow.py --client ./build/client-manifest/Release/mini_im_native_driver.exe
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
2,056,192 字节的存储故障恢复检查允许最多 60 秒等待完成，并将实际等待时间记录为 `*-recovery-timing.json`。
该上限用于功能验收，不代表吞吐要求；服务端每次接收写盘及同步事件成本仍需专项测量。
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
& ./.venv/Scripts/python.exe ./tools/test_server_restart.py --client ./build/client-manifest/Release/mini_im_native_driver.exe
~~~

此入口先启动两个真实 Qt 原生客户端，焚毁场景另启动同用户的第二设备与新缓存设备，通过 [服务进程夹具](../tools/server_restart_fixture.py)
直接调用 [生产启动流程](../server/quic/server.py) 的 `run_server(data_root=..., port=...)`。
默认开发启动参数和数据位置保持原值；上述函数参数仅用于选择隔离目录与端口，首次启动可传 0 分配端口。
测试清除继承的 `MINIIM_` 配置，使用独立证书、数据库、文件目录和随机本机端口；
重启复用原目录和已分配端口，服务进程编号必须变化。

检查在消息/控制事务提交前、提交后但确认发送前、上传仅在内存缓冲及落盘但进度提交前等明确位置暂停回调，
由父进程强制终止服务，重启后检查回滚或保留结果、原请求重试、连续同步和完整文件内容。
同时覆盖下载临时文件续传、文件完成与取消、已读和撤回，以及新会话下的恢复。
下载初始化保持请求与文件意图标识，但续传位置及服务端补齐的元数据会变化，按该语义核对。

业务边界用例将隔离服务的心跳间隔设为 1 秒以缩短失联等待，默认配置用例保留 15 秒心跳。
恢复过程使用原生自动重连；取消共同退出用例的前置准备会主动断开并重新连接，以保证服务中断点先于取消请求启用。
可追加 `--test test_upload_uncommitted_disk_tail_is_not_counted_after_crash` 单独运行一个场景，
重复 `--test` 选择多个；省略时执行全部。`test_upload_uncommitted_memory_is_retransmitted_after_crash` 单独验证内存中至少 4096 字节未提交时终止实际服务、按原文件意图和数据库位置重传。

输出位于 `tmp/server-restart/<运行时间>/`，`--output` 可指定父目录；
每个场景保存服务进程日志、请求与中断位置、客户端事件、`lifecycle.json` 和最终数据库副本。
`lifecycle.json` 的 `pid`、`exitCode` 记录实际服务，`launcherPid`、`launcherExitCode` 单独记录启动器，另含端口与是否强制终止。
Windows 虚拟环境可能通过启动器创建 Python 子进程；测试从服务就绪事件取得真实进程号，保留其系统句柄，
直接终止并等待该进程退出，再读取数据库。中断位置的服务进程号必须与终止对象一致，`results.json` 汇总结果。
读库发生 SQLite 错误时，`sqlite-errors.jsonl` 保存扩展错误码、名称、数据库及日志文件状态与服务退出记录，随后继续抛出原错误。
结束时关闭测试进程并清理临时运行目录；上述证据被 Git 忽略，不作为新环境复现前提。
焚毁专项可追加 `--test test_burn_scan_rolls_back_before_server_commit --test test_burn_scan_commit_survives_lost_push`。
驱动的 `message` 命令接受可选 `burnMode`、`burnTtlSec`，省略均为 0；专项通过真实接口发送 5 秒焚毁消息。
服务按默认 1000 毫秒间隔扫描，在实际修改投递的事务提交前、提交后但推送前暂停；不改写业务时间或伪造过期。
恢复后检查发送者已焚毁而未读接收者仍可读，同用户第二设备的重复已读不延长截止时间。
随后在接收者到期前终止服务和该用户一个客户端，等待真实时间跨过期限后，用原数据和缓存重启。
核对各设备正文清理、历史同步副本、连续事件位置与确认、重复扫描的稳定事件标识及首次焚毁时间。
额外证据 `bob-crash.json` 记录客户端退出码、停止时间与截止时间，`burn-scan` 日志证明后续空扫描实际执行。
共同退出专项的方法名以 `test_joint_crash_` 开头，可通过重复 `--test` 选择。
其中消息、改名分别覆盖提交前和提交后，上传覆盖未提交磁盘尾部与已提交进度，下载覆盖已有部分文件，
文件完成、取消和收件确认覆盖提交前和确认丢失，已读与撤回覆盖各自提交后确认丢失。
取消专项为 `test_joint_crash_cancel_before_commit`、`test_joint_crash_cancel_after_commit`；
收件确认专项为 `test_joint_crash_delivery_before_commit`、`test_joint_crash_delivery_after_commit`。
取消前置准备在初始化确认丢失时先停止服务，客户端主动断开后保存取消，再启用取消中断点并连接；
目标中断发生后，服务和两个客户端均被强制终止。取消记录、任务状态、事件与请求结果必须共同提交。
收件确认核对退出前本地 `sync_confirmation` 的请求和位置，重启后原请求正文不变，首次送达时间不重写。
两类操作的 `durable-ack` 证据保存确认内容，提交后恢复必须与原数据库记录逐字节相同。
在服务中断点停止服务后，两个客户端也被强制终止；用原缓存启动新的客户端进程并重新登录。
初始消息、控制及文件队列中的请求和意图标识须与退出前数据库一致，同步位置不得退回。
该组额外保存 `joint-crashes.json`、退出前客户端事件与数据库、重启初始状态以及最终客户端数据库；
检查旧进程的非零退出码与新进程编号，再沿用原业务断言核对事务、重复执行、续传字节和连续同步。
验证不覆盖机器断电、磁盘损坏、默认 15 分钟上传接管等待或完整 Qt WebEngine 页面；
既有结果见 [共同退出批次](refactoring-progress.md#客户端与服务共同退出恢复--2026-09-10)，
取消与收件确认新增覆盖见 [补充批次](refactoring-progress.md#取消与收件确认共同退出--2026-09-10)。

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
快照中的 `artifactErrors` 记录正在被 Qt 占用或在枚举后消失的文件，暂不可读文件不生成大小或摘要证明；
最终目标文件仍须通过实际字节校验。[快照回归](../tools/test_desktop_fixture.py) 覆盖 Windows 独占锁和文件消失，
可用 `& ./.venv/Scripts/python.exe ./tools/test_desktop_fixture.py` 单独执行。
文件专项使用另一个全新的夹具环境，在上述命令末尾追加 `--files-only`。
该模式上传 2,056,192 字节的隔离文件，暂停部分内容接收后终止桌面进程，再从同一缓存登录恢复；
下载也在临时文件已有数据时重启，并检查切换账号后的待处理任务隔离。
后续通过真实页面验证重复填入同一文件、摘要错误后的原任务重试、源文件丢失/截短后的失败提示及重启恢复、传输中取消及取消后重启。
脚本比对上传存储文件、源文件和下载目标的字节及 SHA-256，核对原意图、请求标识、续传偏移和单条文件消息。
SHA-256 = 根据文件内容计算的固定摘要，此处配合逐字节比较检查内容一致。
故障只作用于夹具数据：临时暂停文件流/下载调度、拒绝本地文件任务写入、改变隔离服务文件的一个字节，以及删除/截短后恢复该文件。
失败及取消保留原目标；客户端下载片段继续保留，服务端已取消上传可使用 [离线清理](#离线文件检查恢复与清理) 按指定保留期处理。
`fileAttempts`、`fileTasks`、`transfers`、`artifacts` 与 `clientExits` 保存在结果中，记录各次实际调用和终止退出码。
两种模式都要求独立的全新环境；使用同一个已执行过测试的夹具不算有效复现。
本入口不覆盖同时运行的多设备、独立服务端进程宕机与其他平台；跨进程服务检查仍用前述独立入口。
本批验收与限制见 [桌面文件批次](refactoring-progress.md#真实桌面文件传输与恢复--2026-09-10)。

### 桌面文件并发

另建全新的桌面夹具，在页面检查命令末尾追加 `--concurrent-files`；该模式与 `--files-only` 互斥。
[并发检查](../tools/desktop_file_concurrency.py) 从真实页面提交 9 个上传，核对 8 个任务进入传输、1 个保留在本地队列；
取消一个上传后，等待中的第 9 个任务须开始。随后改用另一用户运行 6 个下载和 3 个上传，
取消一个下载后，等待的上传须开始，两组都验证文件等待时发送聊天消息。
新下载任务使用目标文件名显示；旧任务保留已保存名称及初始化请求内容，以保持原请求身份。

两组均在已提交部分进度后强制停止桌面进程，并从同一缓存重启；
核对初始化和完成请求、原文件意图、上传恢复位置与下载临时文件长度，以及取消任务不再初始化。
每个源文件为 2056193 字节，12 个源文件内容各不相同；总计 18 个任务应为 16 个完成、2 个取消，生成 11 条文件消息及 2 条聊天消息。
最终比较文件实际字节、取消下载的原目标内容、全部本地任务终态及两个用户的连续同步位置和确认。
暂停的上传数据在恢复时重新进入生产事件队列，避免夹具同步排空阻塞控制接口。

并发完成检查上限为 60 秒，普通状态检查仍为 15 秒、夹具命令响应为 10 秒；这些是测试等待上限，不是产品性能承诺。
阶段状态另保存在 `concurrent-*.json`，`ui-result.json` 中的 `completionMeasurements` 记录解除暂停到页面任务消失的耗时及同步事件增量。
该耗时包括等待、恢复和 CLI 页面检查；不等同于稳态吞吐、网络延迟或无其他负载的基准。
本检查仍使用夹具进程内的服务，只终止真实桌面客户端；桌面与独立服务共同退出须另行验收。
本批证据与首轮失败见 [并发批次](refactoring-progress.md#桌面文件并发与下载任务名称--2026-09-11)。

### 桌面与独立服务共同退出

[共同退出检查](../tools/test_desktop_restart.py) 自动建立隔离环境、启动真实 Qt WebEngine 桌面和独立服务进程，
通过相同 Playwright CLI 操作页面；无需先运行 `desktop_fixture.py`。
服务复用 [生产进程故障夹具](../tools/server_restart_fixture.py)，在指定业务提交或文件写盘位置暂停，
测试按服务自报身份直接终止实际服务，并同时终止桌面；两者退出码均须非零。

~~~powershell
& ./.venv/Scripts/python.exe ./tools/test_desktop_restart.py --qt-root 'D:/Qt/6.11.0/msvc2022_64' --playwright-cli 'C:/tools/playwright-core/lib/tools/cli-client/cli.js'
~~~

默认运行 17 项：消息、改名、文件完成、取消、已读、撤回及收件确认各覆盖提交前/确认发送前，
另有上传未提交磁盘尾部、上传已提交进度和下载已有部分内容三个场景。
取消场景保留已下载片段，核对重启后原取消请求继续确认、下载不重新初始化、目标文件及片段不变。
桌面进入会话会先发送已读请求；收件确认场景核对后续同步确认重试不会降低已读状态或改写首次送达、已读时间。
可重复追加 `--test test_message_before_commit` 等方法名选择场景；`--client` 指定另一桌面构建，`--output` 指定证据父目录。
默认客户端为 `build/client-manifest/Release/mini_im_client.exe`，命令中的 Qt 与 CLI 路径按本机替换。
重启复用原数据目录、缓存和服务端口，实际服务及桌面进程号均须变化；核对请求和意图身份、业务结果、页面内容、文件字节及连续同步确认。
已提交上传可从稳定的完成同步事件恢复，此时不要求桌面再次发送完成请求；原完成结果及事件内容必须保持一致。

证据保存在 `tmp/desktop-restart/<运行时间>/<场景>/`：`lifecycle.json` 分别记录服务与启动器退出码，
`joint-crashes.json` 记录中断点、两端退出与替换进程，另有两端重启前后数据库副本、`final-state.json`、文件摘要和页面阶段。
截图保存在 `output/playwright/<运行时间>/<场景>/`；所有运行证据均被 Git 忽略，测试结束清理隔离运行目录。
`results.json` 只有在全部选定场景通过后才标记成功。
这些检查不代表机器断电恢复或性能验收；
现有结果见 [共同退出桌面批次](refactoring-progress.md#真实桌面与独立服务共同退出--2026-09-11)
及 [控制操作补齐批次](refactoring-progress.md#桌面控制操作共同退出--2026-09-11)。

## 下载流结束与传输库兼容

FIN = QUIC 数据流结束标记；接收端须收到结束标记并完成落盘校验，再发送文件完成确认。
项目固定使用 aioquic 1.3.0，其空 FIN 生成路径会在包头空间不足时提前消耗结束标记，
随后包构建拒绝该帧，导致结束标记既未发送也没有重传回调；文件字节全部到达仍可能卡住。
[兼容适配](../server/quic/download.py) 在每条下载流的发送器取帧入口检查预算，
剩余正文预算小于 0 时保留待发送状态，等于 0 时允许空 FIN；正常确认、丢包重传及接收端校验保持原流程。
适配仅作用于该下载流实例，不修改已安装依赖；升级 aioquic 时须复核此私有接口并运行
[下载调度回归](../server/tests/test_download_scheduler.py) 及下方持续负载检查。

## 聊天与文件并发测量

[测量入口](../tools/measure_load.py) 在 Windows 上启动两个真实 Qt 原生驱动和独立生产服务进程，使用随机端口、隔离数据库和文件。
默认每个文件 1,048,576 字节，依次运行纯聊天、1 个上传加 1 个下载、4 个上传加 4 个下载，每组重复两次。
并发组先上传一个下载源；这段准备过程不计入测量。各上传按任务名称记录内容摘要，下载核对原始源的摘要。
聊天正文为 128 字节，从一端发给另一端；每次收到消息后再发送下一条，相邻发送起点至少间隔 0.1 秒。
默认只提交一轮文件，每组至少运行 5 秒并等到所有文件完成。
`--sustained` 按每方向指定并发数持续补充任务：每个任务完成后立即创建下一项，到 `--seconds` 后停止补充并排空已接受任务。
每项使用独立任务名称与发送意图，完成数量按实际轮换累计；记录每项开始、结束时间与所属并发位置。
慢消息会降低发送频率，慢文件会降低任务补充频率，因此不代表固定到达率的容量测试。

~~~powershell
& ./.venv/Scripts/python.exe ./tools/measure_load.py
& ./.venv/Scripts/python.exe ./tools/measure_load.py --concurrency 0 1 --repeats 1 --file-bytes 262144 --seconds 2
& ./.venv/Scripts/python.exe ./tools/measure_load.py --sustained --seconds 10
~~~

正式测量期间不要同时运行构建或其他测试。`--client` 可替换原生驱动，`--output` 可替换证据父目录。
输入限制：文件 1 至 2,097,152 字节、最短运行时间 1 至 30 秒、发送间隔 0.02 至 5 秒、重复 1 至 10 轮、每方向并发 0 至 8。
单个文件完成等待 75 秒，测量任务组等待 90 秒；驱动本身 120 秒退出。这些是工具等待限制，不是性能承诺。
任一业务核对或正常退出失败，命令非零退出，保留错误证据并清理隔离运行数据。
运行中失败时，尽可能先保存两端及服务端数据库备份、运行目录文件长度、已完成任务与服务端下载调度快照；
证据采集失败记录为 `evidenceError`，不覆盖原始错误，快照不能当作同一时刻的原子状态。

`tmp/load-measurement/<时间>/environment.json` 保存操作系统、处理器、依赖、Git 状态以及驱动和测量脚本摘要；
`summary.json` 保存已完成各组汇总，各组 `result.json` 保留逐条聊天耗时、每个文件耗时、原始资源采样和退出码。
端到端延迟 = 从控制器发起消息意图，到控制器观察到接收端原生消息事件的耗时，包含本地保存、传输和事件处理；
`chatAcceptedMs` 只到发送端返回已接受，不能作为服务端确认耗时。
`chatDuringFileWindowMs` 只统计最后一个文件完成前开始的消息；文件总吞吐按该窗口计算，另保留含最短运行时间的平均值。
`chatWhileTransferActiveMs` 只统计发起时至少有一项文件任务未完成的消息。
资源采样中的 `activeTransfers` 由各任务开始/结束时间重算，按上传/下载分别计数；
任务占用区间含命令接受、协议处理与完成事件观察，不能据此断言整个区间都在发送网络字节。
P95 = 将样本排序后取第 `ceil(0.95 × 样本数)` 项；P50、P99 同法，必须同时看样本数，少量样本不能推断长期尾延迟。

[测量夹具](../tools/load_server_fixture.py) 仅包装生产调用计时，保留原始落盘、事务和传输行为；
记录上传追加次数、字节数、对应同步事件数、Python `os.fsync` 次数/耗时、队列等待与执行、20 毫秒定时任务的延迟。
`maxQueuedOperations` 和 `maxQueuedPayloadBytes` 分别记录等待操作数、排队原始网络字节数的最大值；统计范围同共享写队列。
这里的 `fsync` 数量不包含 SQLite 在原生库内部执行的同步，追加耗时已包含其内部强制落盘耗时，不能相加。
[进程采样](../tools/process_metrics.py) 每 100 毫秒读取实际服务、两端驱动和控制器的 CPU 时间与工作集。
单核 CPU 百分比 = CPU 时间增量除以实测墙钟时间再乘 100；工作集 = 当前映射到进程的物理内存，报告采样最大值。
接口含义见微软的 [进程时间](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)
和 [内存计数](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters) 文档。
采样会漏掉两个采样点之间的瞬时峰值；计时包装、原生测试事件输出和控制器也会引入开销。

每组核对消息数量、每个上传和下载的实际摘要、任务完成状态、两端连续同步确认、数据库完整性及进程正常退出。
这是本机回环网络和原生核心的基线，未覆盖 Qt WebEngine 渲染、远程网络、其他操作系统、持续稳定负载和缓存增长。
结果与后续优化范围统一记录在 [执行记录](refactoring-progress.md#聊天与文件并发测量基线--2026-09-12)。

## 客户端缓存规模测量

[缓存测量入口](../tools/measure_cache.py) 使用 [Qt 测量驱动](../client/tests/cache_benchmark.cpp) 调用生产 `MiniImStateStore.open()` 和 `snapshot()`，
分别记录打开数据库、生成初始快照、转换为 JSON 的耗时与输出字节数；另记录完整进程墙钟时间。
墙钟时间 = 控制器从启动测量进程到收到结果并确认退出的时间，包含动态库加载、对象释放及进程退出。
驱动是 `BUILD_TESTING=ON` 下的独立构建目标，不进入桌面发布包。

~~~powershell
cmake --build ./build/client-manifest --config Release --target mini_im_cache_benchmark
& ./.venv/Scripts/python.exe ./tools/measure_cache.py
& ./.venv/Scripts/python.exe ./tools/measure_cache.py --messages 0 1000 --conversations 10 --repeats 1
~~~

默认消息数为 100、10,000、100,000，每个规模使用 10 个会话、128 字节正文，每条消息附一项投递及一项已读人数投影；
各会话附当前用户已读位置 0，全部消息来自另一用户，因此预期未读总数等于消息数。
数据由工具直接填充到隔离数据库，未通过业务事件写入；只测本地投影规模，不证明生产事件处理或同步恢复正确性。
每个规模先建库并填充，再启动三个独立 Qt 进程测量；操作系统文件缓存未清空，不能称为冷启动磁盘性能。
同组进程复用已填充数据库；输入生成时间不计入测量；失败非零退出，日志保留错误，临时缓存自动移除。

`--driver` 指定构建驱动，`--output` 指定证据目录；`--messages` 范围 0 至 100,000，
`--conversations` 范围 1 至 100，`--repeats` 范围 1 至 10，单个进程等待上限 120 秒。
结果位于 `tmp/cache-measurement/<时间>/`，保存环境、源码/工具/驱动摘要、每次分项耗时、对象数量和输出大小。
所有样本必须保留全部输入消息、投递和已读人数，并核对会话及未读总数；不使用丢弃输入的结果比较速度。
这项检查不包含登录、真实 QWebChannel 传递、页面渲染或进程内存采样，不能把 JSON 字节数当作内存占用。
当前结果见 [缓存规模基线](refactoring-progress.md#客户端缓存规模测量基线--2026-09-12)。

## 现有脚本与历史环境

| 入口 | 当前使用边界 |
| --- | --- |
| [generate_proto.py](../server/tools/generate_proto.py) | 使用调用它的 Python 环境内的 `grpc_tools.protoc` 生成协议并修正包内导入 |
| [bootstrap.ps1](../tools/bootstrap.ps1)、[check_env.ps1](../tools/check_env.ps1) | 可配置 Qt、vcpkg、虚拟环境和构建目录，命令失败即停止；操作见 [准备依赖](#准备依赖) |
| [dev_start.ps1](../tools/dev_start.ps1)、[dev_stop.ps1](../tools/dev_stop.ps1) | 管理隐藏的后台服务、就绪检查和进程身份，操作见 [运行与数据](#运行与数据) |
| [verify.ps1](../tools/verify.ps1) | 统一基础与可选全量网络验证，范围见 [验证](#验证) |

已有评估环境把 Python 依赖放在被 Git 忽略的 `tmp/architecture-review-deps`。
只有该目录已存在时，才可在单独终端用下述方式复核；新环境仍按本指南建立 `.venv`：

~~~powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'tmp/architecture-review-deps'
python -m unittest discover server/tests -v
~~~

历史临时目录和旧 Qt 路径均不是新环境的前置条件。
