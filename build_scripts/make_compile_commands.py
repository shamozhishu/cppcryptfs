# 从 .vcxproj 的 Debug|x64 配置生成 compile_commands.json 供 clangd 使用。
# 用法: python build_scripts/make_compile_commands.py
# 新增/删除 .cpp 后重新运行一次。

import json
import os
import glob

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

OPENSSL = r"D:\Program Files\OpenSSL-Win64\include"
RAPIDJSON = r"D:\Code\rapidjson\include"
DOKAN = r"C:\Program Files\Dokan\DokanLibrary-2.3.1\include"
MSVC_VER = "19.51"

COMMON = [
    "--target=x86_64-pc-windows-msvc",
    "-std=c++14",  # vcxproj 未设 LanguageStandard，MSVC 默认 C++14
    f"-fms-compatibility-version={MSVC_VER}",
    "-D_UNICODE", "-DUNICODE",
    "-Wno-non-pod-varargs",          # 历史代码将 wstring 传变参函数（MSVC 扩展）
    "-Wno-inconsistent-missing-override",  # MFC 老代码不写 override
]

# 各项目 flags 取自对应 .vcxproj 的 Debug|x64（include 顺序照抄）
PROJECTS = {
    "libcppcryptfs": {
        "defines": ["-D_DEBUG", "-D_LIB"],
        "includes": [f"-I{ROOT}\\libcppcryptfs"],
        "systems": [f"-isystem{RAPIDJSON}", f"-isystem{OPENSSL}"],
    },
    "cppcryptfs": {
        # MSVC 用 /Yu"stdafx.h" PCH（stdafx.cpp 以 cppcryptfs/stdafx.h 创建），
        # 等价于强制前置该头；ui 下的 MFC 头文件依赖它提供 CString/afx_msg 等
        # （注意用 GCC 风格 -include 分离参数，clang++ 驱动不认 -FI）
        "forced_include": ["-include", f"{ROOT}\\cppcryptfs\\stdafx.h"],
        "defines": ["-D_DEBUG", "-D_WINDOWS"],
        "includes": [
            f"-I{ROOT}\\libcppcryptfs",   # cryptdokan.cpp 等引用 "util/..." 相对路径
            f"-I{ROOT}\\cppcryptfs",
        ],
        "systems": [f"-isystem{DOKAN}", f"-isystem{OPENSSL}"],
    },
    "cppcryptfsctl": {
        "defines": ["-D_DEBUG", "-D_CONSOLE"],
        "includes": [f"-I{ROOT}\\libcppcryptfs"],
        "systems": [f"-isystem{OPENSSL}"],
    },
    "libipc": {
        "defines": ["-D_DEBUG", "-D_LIB"],
        "includes": [f"-I{ROOT}\\libipc"],
        "systems": [],
    },
    "libcommonutil": {
        "defines": ["-D_DEBUG", "-D_LIB"],
        "includes": [f"-I{ROOT}\\libcommonutil"],
        "systems": [],
    },
}


def main():
    entries = []
    for proj, cfg in PROJECTS.items():
        # .cpp 条目；.h 也生成条目（带各自项目的 flags），使 clangd 打开头文件时
        # 直接命中命令，无需依赖后台索引从 .cpp 推断（时序不可靠）
        patterns = {
            "cpp": os.path.join(ROOT, proj, "**", "*.cpp"),
            "h": os.path.join(ROOT, proj, "**", "*.h"),
        }
        for kind, pattern in patterns.items():
            for src in glob.glob(pattern, recursive=True):
                if "\\x64\\" in src:
                    continue
                if kind == "cpp" and "\\res\\" in src:
                    continue
                # dokan/ 文件（.cpp 和 .h）不能强制前置 stdafx.h（MFC 前置会破坏
                # NTSTATUS 头包含顺序），且它们不引用 MFC 类型、也不使用 PCH
                forced = cfg.get("forced_include", [])
                if "\\dokan\\" in src:
                    forced = []
                # locutils.cpp 在 vcxproj 里是 NotUsing PCH：真实构建不含 MFC stdafx，
                # 它自己包含 <atlstr.h> 使用纯 ATL 的 ATL::CStringW；强制前置 stdafx.h
                # 会定义 _AFX，而 MFC 环境下 atlstr.h 不再定义 ATL::CStringW
                if src.endswith("\\ui\\locutils.cpp"):
                    forced = []
                args = ["clang++"] + COMMON
                args += forced
                args += cfg["defines"]
                args += cfg["includes"]
                args += cfg["systems"]
                args += ["--", src]
                entries.append(
                    {
                        "directory": ROOT,
                        "arguments": args,
                        "file": src,
                    }
                )

    out = os.path.join(ROOT, "compile_commands.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=1)
    print(f"wrote {out}: {len(entries)} entries")


if __name__ == "__main__":
    main()
