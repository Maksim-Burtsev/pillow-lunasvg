cmake_minimum_required(VERSION 3.15)
project(pillow_lunasvg LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(LUNASVG_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(USE_SYSTEM_PLUTOVG OFF CACHE BOOL "" FORCE)
# Untrusted SVGs must not be able to read local files via <image href="/path">.
set(LUNASVG_DISABLE_EXTERNAL_RESOURCES ON CACHE BOOL "" FORCE)
set(CMAKE_POSITION_INDEPENDENT_CODE ON CACHE BOOL "" FORCE)

set(LUNASVG_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/lunasvg")
if(NOT EXISTS "${LUNASVG_DIR}/CMakeLists.txt")
  message(FATAL_ERROR "third_party/lunasvg is empty: run git submodule update --init")
endif()
add_subdirectory("${LUNASVG_DIR}" EXCLUDE_FROM_ALL)

set(PYBIND11_FINDPYTHON ON)
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(_lunasvg src/_lunasvg.cpp)
target_link_libraries(_lunasvg PRIVATE lunasvg::lunasvg plutovg::plutovg)

install(TARGETS _lunasvg DESTINATION pillow_lunasvg)
