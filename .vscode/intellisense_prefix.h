// 仅供 VS Code C/C++ 扩展 IntelliSense 使用的强制前置头（c_cpp_properties.json 的 forcedInclude）。
// 不参与 MSBuild 编译，不影响工程任何行为。
//
// 背景：cppcryptfs 的头文件不自包含 —— 例如 crypt/crypt.h 使用的
// shared_ptr / string / wstring 依赖翻译单元先包含过带 "using namespace std;"
// 的头（如 file/openfiles.h）。在 .cpp 中编译正常，但单独打开 .h 时
// IntelliSense 缺少这些名字而报红。此文件补齐这些前置声明。

#pragma once

#include <windows.h>

#include <memory>
#include <string>
#include <vector>
#include <list>
#include <map>

using namespace std;
