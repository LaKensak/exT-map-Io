cmake_minimum_required(VERSION 3.20)
project(RustESP CXX)

set(CMAKE_BUILD_TYPE Release)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_compile_options(/W4 /permissive- /utf-8)
add_definitions(-DUNICODE -D_UNICODE -DNDEBUG -D_CRT_SECURE_NO_WARNINGS)

# Include directories
include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}/cpp_src
    ${CMAKE_CURRENT_SOURCE_DIR}/imgui
)

# Source files
file(GLOB_RECURSE SOURCES
    "cpp_src/*.cpp"
    "imgui/*.cpp"
)

file(GLOB_RECURSE HEADERS
    "cpp_src/*.hpp"
    "cpp_src/*.h"
    "imgui/*.h"
)

add_executable(RustESP ${SOURCES} ${HEADERS})

# Windows System Libraries
target_link_libraries(RustESP PRIVATE
    d3d9
    dwmapi
    winhttp
    winmm
    psapi
)

set_target_properties(RustESP PROPERTIES
    WIN32_EXECUTABLE FALSE
)
