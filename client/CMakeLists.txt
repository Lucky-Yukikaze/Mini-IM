cmake_minimum_required(VERSION 3.16)
project(mini_im_client LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Widgets WebChannel)
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

set(MINI_IM_CLIENT_SOURCES
    main.cpp
    bridge/imbridge.cpp
    core/session/sessionmanager.cpp
    ui/mainwindow.cpp
)

set(MINI_IM_CLIENT_HEADERS
    bridge/imbridge.h
    core/session/sessionmanager.h
    ui/mainwindow.h
)

add_executable(mini_im_client
    ${MINI_IM_CLIENT_SOURCES}
    ${MINI_IM_CLIENT_HEADERS}
)

target_include_directories(mini_im_client PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(mini_im_client PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")

protobuf_generate(
    TARGET mini_im_client
    LANGUAGE cpp
    PROTOS ${MINI_IM_PROTO_FILES}
    IMPORT_DIRS "${MINI_IM_PROTO_DIR}"
    APPEND_PATH
)

target_link_libraries(mini_im_client PRIVATE
    Qt6::Core
    Qt6::Widgets
    Qt6::WebChannel
    protobuf::libprotobuf
)

if(TARGET msquic::msquic)
    target_link_libraries(mini_im_client PRIVATE msquic::msquic)
    target_compile_definitions(mini_im_client PRIVATE MINI_IM_HAS_MSQUIC=1)
    message(STATUS "Mini-IM: using MsQuic from CMake package target msquic::msquic")
elseif(TARGET msquic)
    target_link_libraries(mini_im_client PRIVATE msquic)
    target_compile_definitions(mini_im_client PRIVATE MINI_IM_HAS_MSQUIC=1)
    message(STATUS "Mini-IM: using MsQuic from CMake package target msquic")
else()
    message(FATAL_ERROR "Mini-IM: MsQuic CMake target not found, cannot build pure C++ QUIC runtime.")
endif()

if(Qt6WebEngineWidgets_FOUND)
    target_link_libraries(mini_im_client PRIVATE Qt6::WebEngineWidgets)
    target_compile_definitions(mini_im_client PRIVATE MINI_IM_HAS_WEBENGINE=1)
endif()
