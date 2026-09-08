cmake_minimum_required(VERSION 3.16)
project(mini_im_client LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Sql Widgets WebChannel)
find_package(Qt6 QUIET COMPONENTS WebEngineWidgets)
find_package(msquic CONFIG REQUIRED)
find_package(protobuf CONFIG REQUIRED)

set(MINI_IM_PROTO_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../proto")
set(MINI_IM_PROTO_FILES
    "${MINI_IM_PROTO_DIR}/auth.proto"
    "${MINI_IM_PROTO_DIR}/common.proto"
    "${MINI_IM_PROTO_DIR}/conversation.proto"
    "${MINI_IM_PROTO_DIR}/envelope.proto"
    "${MINI_IM_PROTO_DIR}/file.proto"
    "${MINI_IM_PROTO_DIR}/message.proto"
    "${MINI_IM_PROTO_DIR}/sync.proto"
)

add_library(mini_im_core STATIC
    core/logging.cpp
    core/logging.h
    core/file/coordinator.cpp
    core/file/coordinator.h
    core/quic/connection.cpp
    core/quic/connection.h
    core/quic/recovery.cpp
    core/quic/recovery.h
    core/message/outbox.cpp
    core/message/outbox.h
    core/model/eventmapper.cpp
    core/model/eventmapper.h
    core/sync/coordinator.cpp
    core/sync/coordinator.h
    core/sync/statestore.cpp
    core/sync/statestore.h
    core/session/sessionmanager.cpp
    core/session/sessionmanager.h
    core/file/taskstore.cpp
    core/file/taskstore.h
    core/file/downloadsink.cpp
    core/file/downloadsink.h
    core/file/uploadstream.cpp
    core/file/uploadstream.h
)
target_include_directories(mini_im_core PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_BINARY_DIR}
)
protobuf_generate(
    TARGET mini_im_core
    LANGUAGE cpp
    PROTOS ${MINI_IM_PROTO_FILES}
    IMPORT_DIRS "${MINI_IM_PROTO_DIR}"
    APPEND_PATH
)
target_link_libraries(mini_im_core PUBLIC Qt6::Core Qt6::Sql protobuf::libprotobuf)

if(TARGET msquic::msquic)
    target_link_libraries(mini_im_core PUBLIC msquic::msquic)
elseif(TARGET msquic)
    target_link_libraries(mini_im_core PUBLIC msquic)
else()
    message(FATAL_ERROR "Mini-IM: MsQuic CMake target not found.")
endif()
target_compile_definitions(mini_im_core PRIVATE MINI_IM_HAS_MSQUIC=1)

add_executable(mini_im_client
    main.cpp
    bridge/imbridge.cpp
    bridge/imbridge.h
    ui/mainwindow.cpp
    ui/mainwindow.h
)
target_link_libraries(mini_im_client PRIVATE mini_im_core Qt6::Widgets Qt6::WebChannel)

if(Qt6WebEngineWidgets_FOUND)
    target_link_libraries(mini_im_client PRIVATE Qt6::WebEngineWidgets)
    target_compile_definitions(mini_im_client PRIVATE MINI_IM_HAS_WEBENGINE=1)
endif()

include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 REQUIRED COMPONENTS Network)
    add_executable(mini_im_native_driver tests/native_driver.cpp bridge/imbridge.cpp bridge/imbridge.h)
    target_link_libraries(mini_im_native_driver PRIVATE mini_im_core Qt6::Network Qt6::WebChannel)
    add_executable(mini_im_download_tests tests/downloadsink_test.cpp core/file/downloadsink.cpp)
    target_include_directories(mini_im_download_tests PRIVATE .)
    target_link_libraries(mini_im_download_tests PRIVATE Qt6::Core)
    add_test(NAME download_integrity COMMAND mini_im_download_tests)
    add_executable(mini_im_upload_tests tests/uploadstream_test.cpp)
    target_link_libraries(mini_im_upload_tests PRIVATE mini_im_core)
    add_test(NAME upload_backpressure COMMAND mini_im_upload_tests)
    add_executable(mini_im_sync_tests tests/synccoordinator_test.cpp)
    target_link_libraries(mini_im_sync_tests PRIVATE mini_im_core)
    add_test(NAME sync_coordination COMMAND mini_im_sync_tests)
    add_executable(mini_im_state_tests tests/statestore_test.cpp core/sync/statestore.cpp core/message/outbox.cpp core/file/taskstore.cpp)
    target_include_directories(mini_im_state_tests PRIVATE .)
    target_link_libraries(mini_im_state_tests PRIVATE Qt6::Core Qt6::Sql)
    add_test(NAME state_persistence COMMAND mini_im_state_tests)
endif()
