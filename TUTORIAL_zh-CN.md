# cppcryptfs 快速学习指南（复刻自研文件系统向）

> 本文档面向想**快速吃透 cppcryptfs 架构、进而复刻出自研文件系统**的开发者。
> 所有代码引用都是可点击的链接（VSCode 中 `Ctrl+点击` 即可跳转）。
> 配套阅读：[README_zh-CN.md](README_zh-CN.md)（使用方法）、[INSTALL.md](INSTALL.md)（编译）、[CLAUDE.md](CLAUDE.md)（AI 辅助开发摘要）。

---

## 0. 一句话理解这个项目

cppcryptfs = **Dokany（Windows 用户态文件系统框架）** + **gocryptfs 磁盘格式**（逐文件加密、文件名加密）的 C++ 实现。

你在磁盘上看到的是一堆密文文件（文件名也是密文），挂载后你得到一个新盘符，里面全是明文——加密解密在读写时实时进行，对应用程序完全透明。

```
┌─────────────────────────────────────────────────────────────┐
│  记事本/资源管理器  →  读写 R:\docs\报告.txt (明文)           │
├─────────────────────────────────────────────────────────────┤
│  Dokany 内核驱动 (文件系统 IRP) → dokan2.dll (用户态分发)     │
├─────────────────────────────────────────────────────────────┤
│  cppcryptfs/dokan/cryptdokan.cpp   ← 20多个 Dokan 回调函数    │
│    路径翻译: \docs\报告.txt → C:\vault\Xk9f.H2q\gocryptfs...  │
├─────────────────────────────────────────────────────────────┤
│  libcppcryptfs (纯库, 无 Dokan/UI 依赖)                      │
│    文件数据: AES-256-GCM/SIV 按 4KB 块加解密                  │
│    文件名:   EME 广义块加密(以目录IV为 tweak) + base64        │
├─────────────────────────────────────────────────────────────┤
│  Win32 API (CreateFileW/ReadFileW/FindFirstFileW...)         │
│    → C:\vault\ 下的密文文件 (gocryptfs 格式, 跨平台兼容)       │
└─────────────────────────────────────────────────────────────┘
```

**复刻自研文件系统时最重要的认知**：这个项目把"文件系统逻辑"（libcppcryptfs，纯算法层）和"文件系统接入"（cryptdokan.cpp，Dokan 适配层）彻底分离了。你想换加密方案、换存储后端、甚至换掉 Dokan，都只需要动其中一层。这就是它的架构精髓。

---

## 1. 仓库布局（5 分钟）

| 项目 | 类型 | 职责 | 入口 |
|---|---|---|---|
| **libcppcryptfs** | 静态库 | 全部核心：加密、磁盘格式、文件名处理、缓存。**不依赖 Dokan 和 UI** | [cryptconfig.cpp](libcppcryptfs/config/cryptconfig.cpp)、[cryptfile.cpp](libcppcryptfs/file/cryptfile.cpp) |
| **cppcryptfs** | MFC GUI | Dokan 回调胶水层（`dokan/`）+ 属性页界面（`ui/`） | [cppcryptfs.cpp:230](cppcryptfs/cppcryptfs.cpp#L230) `InitInstance` |
| **cppcryptfsctl** | 控制台 | CLI 工具：本地执行 init/改密码；mount 命令通过 IPC 转发给 GUI | [cppcryptfsctl.cpp:621](cppcryptfsctl/cppcryptfsctl.cpp#L621) `wmain` |
| **libipc** | 静态库 | 命名管道 IPC（带 Authenticode 双向签名校验） | [client.cpp](libipc/client.cpp)、[server.cpp](libipc/server.cpp) |
| **libcommonutil** | 静态库 | 杂项工具（版本信息、错误字符串、RAII） | [commonutil.h](libcommonutil/commonutil.h) |

依赖关系：两个 exe 各自链接全部三个静态库；三个库之间互不链接。

### libcppcryptfs 内部分区（核心中的核心）

```
libcppcryptfs/
├── crypt/          加密原语
│   ├── cryptdefs.h     ★ 所有磁盘格式常量（先读这个！）
│   ├── crypt.cpp       AES-GCM (OpenSSL EVP)、HKDF、GCM 字符串加密
│   ├── eme.cpp         EME 文件名加密（宽块）
│   ├── siv.cpp         SIV 密钥编排
│   └── aes-siv/        RFC 5297 AES-SIV 纯 C 实现（CMAC+CTR）
├── config/         gocryptfs.conf 的创建/解析/主密钥加解密
├── context/        CryptContext —— 每个挂载点的"总管家"
├── file/           文件数据读写引擎（块级加解密）
├── filename/       文件名/路径加解密 + 三个缓存
└── util/           密钥安全存储、base64、pad16、fileutil(目录枚举)
```

---

## 2. 磁盘格式：一切的基础（必读）

要理解任何文件系统代码，先理解它的磁盘格式。全部常量定义在 [cryptdefs.h](libcppcryptfs/crypt/cryptdefs.h)：

### 2.1 加密卷根目录长什么样

```
C:\vault\                          ← basedir（存放密文的目录）
├── gocryptfs.conf                 ← 配置文件（含被密码加密的主密钥）
├── gocryptfs.diriv                ← 根目录 IV（16字节随机数）
├── Xk9f.H2q_A/                    ← "docs" 加密后的目录名
│   ├── gocryptfs.diriv            ← 该目录自己的 IV
│   ├── Jz3kQ9x2mN.../             ← 加密文件名（≤175字节，base64url）
│   └── gocryptfs.longname.AbC123  ← 超长名的短哈希替身
│       └── (同名+".name" 旁边文件存完整密文名)
```

**关键设计**：每个目录有自己的随机 16 字节 IV（存在 `gocryptfs.diriv` 文件里）。同一文件名在不同目录下密文完全不同；泄露某个目录 IV 不影响其他目录。

### 2.2 单个密文文件的内部布局

```
偏移 0:   [版本号 2B(大端)][fileid 16B 随机]        = 文件头 18 字节
块 i:     [IV 16B][密文 ≤4096B][GCM标签 16B]        = 4128 字节/块
          块 i 位于偏移 18 + i*4128
          AAD(附加认证数据) = 块号(8B大端) ‖ fileid(16B)

明文块 4096B (PLAIN_BS) ↔ 密文块 4128B (CIPHER_BS)，一一对应
```

- 常量见 [cryptdefs.h:32-46](libcppcryptfs/crypt/cryptdefs.h#L32-L46)：`FILE_HEADER_LEN=18`、`PLAIN_BS=4096`、`CIPHER_BS=4128`、`CIPHER_BLOCK_OVERHEAD=32`
- **为什么这样设计**：随机访问任意明文偏移只需一次定位——`密文偏移 = 18 + (明文偏移/4096)*4128`；块号和 fileid 进 AAD，防块重排/块拷贝攻击（把 A 文件的块贴到 B 文件会验签失败）
- 明文↔密文大小换算函数：[fileutil.cpp:70-135](libcppcryptfs/util/fileutil.cpp#L70-L135)（`adjust_file_size_down/up` 等，`adjust_file_size_down` 在列目录时用来把密文大小减去 18 字节头显示为明文大小）
- SIV 模式（[cryptdefs.h](libcppcryptfs/crypt/cryptdefs.h) 中 `BLOCK_SIV_LEN`）：布局为 `[IV 16B][SIV 16B][密文]`，SIV 本身就是认证标签

### 2.3 gocryptfs.conf（配置文件）

JSON 格式，由 [CryptConfig::create](libcppcryptfs/config/cryptconfig.cpp#L855) 生成、[CryptConfig::read](libcppcryptfs/config/cryptconfig.cpp#L111) 解析：

```json
{
  "Creator": "cppcryptfs 1.4.4.10",
  "EncryptedKey": "iv+密文+标签(base64)",     ← 主密钥被 scrypt 派生的密钥加密
  "ScryptObject": { "Salt": "...", "N": 65536, "R": 8, "P": 1, "KeyLen": 32 },
  "Version": 2,
  "VolumeName": "...",
  "FeatureFlags": ["GCMIV128","DirIV","EMENames","LongNames","Raw64","HKDF"]
}
```

**FeatureFlags 语义**（决定整个系统的行为分支，[cryptconfig.h:51-71](libcppcryptfs/config/cryptconfig.h#L51-L71)）：

| 标志 | 含义 |
|---|---|
| `PlaintextNames` | 文件名不加密（与 EMENames 互斥） |
| `DirIV` | 每目录随机 IV 文件（"确定性命名"选项会关掉它） |
| `EMENames` | 文件名用 EME 加密 |
| `LongNames` | 支持 `gocryptfs.longname.*` 超长名机制 |
| `LongNameMax` | 触发长名机制的阈值（62~255，默认 175） |
| `AESSIV` | 内容用 AES-SIV 而非 GCM（**反向模式必须**） |
| `Raw64` | base64 不带填充符 |
| `HKDF` | 主密钥经 HKDF 派生出各用途子密钥 |

⚠️ 读取时遇到**未知 FeatureFlag 直接报错**（[cryptconfig.cpp:321-331](libcppcryptfs/config/cryptconfig.cpp#L321-L331)）——这是刻意的向前兼容策略，防止新版本 gocryptfs 卷被旧代码误解。

---

## 3. 六条核心数据流（跟代码走一遍 = 学会 80%）

### 3.1 创建加密卷

UI 入口 [CreatePropertyPage.cpp:363](cppcryptfs/ui/CreatePropertyPage.cpp#L363) → 核心在 [CryptConfig::create](libcppcryptfs/config/cryptconfig.cpp#L855)：

1. 根据选项决定 FeatureFlags（EME 与明文名互斥等，[:865-884](libcppcryptfs/config/cryptconfig.cpp#L865-L884)）
2. 校验：反向模式必须 SIV（[:908](libcppcryptfs/config/cryptconfig.cpp#L908)）；正向模式目录必须为空
3. **生成 32 字节随机主密钥**，直接写进 `LockZeroBuffer`（锁定内存页，[:937-947](libcppcryptfs/config/cryptconfig.cpp#L937-L947)）
4. [encrypt_key](libcppcryptfs/config/cryptconfig.cpp#L721)：scrypt(密码, 盐) 派生密钥 → AES-GCM 加密主密钥 → base64 存入 conf
5. `fprintf` 写 JSON 配置 → 设只读 → 在根目录创建首个 [gocryptfs.diriv](libcppcryptfs/util/fileutil.cpp#L627)（`create_dir_iv`）

> **复刻要点**：主密钥永远不以明文落盘；conf 丢了 = 数据永久丢失。scrypt 参数 N=2^16（可调 2^10~2^20，见 [cryptdefs.h:69-71](libcppcryptfs/crypt/cryptdefs.h#L69-L71)）用于抵抗暴力破解。

### 3.2 挂载（mount）—— 文件系统的"开机"

UI 入口 [MountPropertyPage.cpp:343](cppcryptfs/ui/MountPropertyPage.cpp#L343) 调用 [mount_crypt_fs](cppcryptfs/dokan/cryptdokan.cpp#L2019)，这是**整个接入层最重要的一根线索**：

```
mount_crypt_fs (cryptdokan.cpp:2019)
  ├─ 1. 校验挂载点（盘符或空 NTFS 目录），查 MountPointManager 防重复
  ├─ 2. new CryptThreadData（每个挂载一份：DOKAN_OPERATIONS + DOKAN_OPTIONS + CryptContext）
  ├─ 3. 填 DOKAN_OPERATIONS 回调表 ──────────► cryptdokan.cpp:2083 起约 40 行 ★看这里
  ├─ 4. config->read()       读 conf
  ├─ 5. config->decrypt_key() 用密码解出主密钥 (cryptconfig.cpp:622)
  ├─ 6. con->InitEme() + m_siv.SetKey()  初始化两套加密上下文
  ├─ 7. DOKAN_OPTIONS: 挂载点/只读标志/超时/GlobalContext=con (:2140-2158)
  └─ 8. CreateThread(CryptThreadProc) → DokanMain() 阻塞运行整个挂载期 (:2014)
        GlobalContext = (ULONG64)con  (:2295) ★回调函数靠它找回上下文
```

- 回调里取上下文的宏：[cryptdokanpriv.h:45](cppcryptfs/dokan/cryptdokanpriv.h#L45) `#define GetContext() ((CryptContext*)DokanFileInfo->DokanOptions->GlobalContext)`
- `DokanMain` 会阻塞直到卸载，所以每个挂载点独立线程（[CryptThreadProc, cryptdokan.cpp:2009](cppcryptfs/dokan/cryptdokan.cpp#L2009)）
- 卸载：[unmount_crypt_fs](cppcryptfs/dokan/cryptdokan.cpp#L2374) → `DokanRemoveMountPoint`

**CryptContext**（[cryptcontext.h:83](libcppcryptfs/context/cryptcontext.h#L83)）= 每挂载点单例，持有：config 指针、EME/SIV 加密上下文、三个缓存、打开文件表 `m_openfiles`、防泄漏句柄表 `m_open_handles`、各种开关（只读/多线程/缓存 TTL…）。**你在任何回调里需要的一切状态都从这里拿。**

### 3.3 打开文件：明文路径 → 密文路径（★最核心的机制）

这是用户态文件系统最本质的问题：**系统给你 `\docs\报告.txt`，磁盘上根本没有这个名字**。

三个层次配合：

**① 懒转换包装器 [FileNameEnc](cppcryptfs/dokan/FileNameEnc.h)**

每个 Dokan 回调第一行都这么写（如 [CryptCreateFile, cryptdokan.cpp:310](cppcryptfs/dokan/cryptdokan.cpp#L310)）：

```cpp
FileNameEnc filePath(DokanFileInfo, FileName, &actual_encrypted);  // :315
// ...构造几乎零开销，直到第一次被当 const WCHAR* 用时才真正转换：
GetFileAttributes(filePath);   // ← 此刻触发 FileNameEnc::Convert() (FileNameEnc.cpp:94)
```

[FileNameEnc::Convert](cppcryptfs/dokan/FileNameEnc.cpp#L94) 内部：大小写不敏感挂载先查 CaseCache 修正大小写 → 调 `encrypt_path`。反向模式则相反走 `decrypt_path`。

**② 逐组件加密 [encrypt_path](libcppcryptfs/filename/cryptfilename.cpp#L748)**

```
encrypt_path("\docs\报告.txt")
  取根目录 IV ← get_dir_iv(C:\vault)        ← 读 gocryptfs.diriv，带缓存
  组件 "docs":
    encrypt_filename(dir_iv, "docs")  → "Xk9f.H2q_A"
    拼成 C:\vault\Xk9f.H2q_A，再读它的 gocryptfs.diriv 作为下一层 IV
  组件 "报告.txt":
    encrypt_filename(新dir_iv, ...)    → "Jz3kQ9x2mN..."
  结果: C:\vault\Xk9f.H2q_A\Jz3kQ9x2mN...
```

**每一级名字都用其父目录的 IV 加密** —— 解密时同理逐级走（[decrypt_path](libcppcryptfs/filename/cryptfilename.cpp#L489)）。

**③ 单个名字的加密 [encrypt_filename](libcppcryptfs/filename/cryptfilename.cpp#L132)**

```
UTF-16 → UTF-8 → pad16 补齐到16倍数 (PKCS#7)
  → EmeTransform(EME上下文, tweak=目录IV, 明文名, 加密方向)   ← 宽块加密，整个名字一起
  → base64url 编码 (util.cpp:296)
  → 若结果长度 > LongNameMax:
      磁盘名 = "gocryptfs.longname." + base64(sha256(完整密文名))
      并把完整密文名写入旁边文件 "<该名>.name"   (write_encrypted_long_name)
```

- EME（ECB-Mix-ECB）主入口：[EmeTransform, eme.cpp:219](libcppcryptfs/crypt/eme.cpp#L219)，数学上保证**改一个字符 → 整个输出完全变化**（伪随机置换），且解密时整名还原、无填充预言攻击
- 解密侧 [decrypt_filename](libcppcryptfs/filename/cryptfilename.cpp#L279)：识别长名前缀 → 读 `.name` 旁边文件 → base64 解码 → EME 解密 → unPad16 → 校验 Windows 非法字符（乱码解出来会因含非法字符而被拒，[:227](libcppcryptfs/filename/cryptfilename.cpp#L227)）
- 目录 IV 的读取+缓存：[get_dir_iv, fileutil.cpp:196](libcppcryptfs/util/fileutil.cpp#L196)

> **复刻要点**：如果你的自研文件系统不做文件名加密，3.3 整节可以只保留"路径拼接"骨架；但**懒转换包装器（FileNameEnc）这个模式强烈建议保留**——它把"翻译"从业务代码里剥离，且天然只做一次。

### 3.4 读文件：按块解密

Dokan 回调 [CryptReadFile, cryptdokan.cpp:791](cppcryptfs/dokan/cryptdokan.cpp#L791)：

```
CryptReadFile(FileName=\docs\a.txt, 偏移, 长度)
  ├─ handle ← DokanFileInfo->Context   (ZwCreateFile 时存入, :594)
  │   （若 handle 丢失——如内存映射的 paging IO——按密文路径重开文件 :808-820）
  ├─ CryptFile::NewInstance(con)       (cryptfile.h:62, 按正/反向模式选子类)
  ├─ file->Associate(con, handle, ...) ← 读18字节头, 拿到 fileid
  └─ file->Read(buf, len, &nread, offset)   ★核心 CryptFileForward::Read (cryptfile.cpp:162)
       块号 = offset / 4096
       跨多块且开了缓冲 → 一次大 ReadFile 到池化 IoBuffer (:206-243)
       循环每个块: read_block(...)         (cryptio.cpp:40)
         密文位置 = 18 + 块号*4128
         AAD = be64(块号) ‖ fileid
         GCM: 验标签→解密 / SIV: 解密→重算S2V比对 (aes256-siv.cpp:93)
         全零块验签失败 → 视为稀疏洞返回零 (cryptio.cpp:99-111) ★重要容错
         其他验签失败 → ERROR_DATA_CHECKSUM_ERROR
       非整块 → 解密整块到栈缓冲再 memcpy 切片 (:262-291)
```

### 3.5 写文件：读-改-写与批量 IV

Dokan 回调 [CryptWriteFile, cryptdokan.cpp:868](cppcryptfs/dokan/cryptdokan.cpp#L868) → 核心 [CryptFileForward::Write, cryptfile.cpp:367](libcppcryptfs/file/cryptfile.cpp#L367)：

```
Write(明文buf, offset)
  ├─ 空文件 → 先写 18 字节头 [版本|随机fileid]  (WriteVersionAndFileId :335)
  ├─ 一次性从 CSPRNG 抽出所有块的 IV（一次系统调用, :492）★性能细节
  ├─ 整块写: write_block (cryptio.cpp:118) → [IV|密文|标签] 写入 18+块号*4128
  │    多块先攒进池化 IoBuffer，FlushOutput 一次连续写出 (:308) ★避免碎IO
  └─ 非整块(头/尾): 加排它文件锁 → 读整块 → 解密 → 打补丁 → 重新加密整块 (:537-575)
       这就是为什么 CryptCreateFile 里写权限会自动加上读权限 (cryptdokan.cpp:411-428)
```

- 并发控制：每个明文路径对应一个 [CryptOpenFile / CryptFileMutex](libcppcryptfs/file/openfiles.h)（公平 mutex + shared_mutex 双层），读共享、读改写独占
- 截断/扩展 [SetEndOfFile, cryptfile.cpp:672](libcppcryptfs/file/cryptfile.cpp#L672)：因为最后一个密文块的长度隐含明文长度，**必须重新加密最后一个块**
- 明文字节区间 → 密文区间转换的 LockFile/UnlockFile：[cryptfile.cpp:613](libcppcryptfs/file/cryptfile.cpp#L613)

### 3.6 列目录：文件名解密的主战场

Dokan 回调 [CryptFindFiles, cryptdokan.cpp:1949](cppcryptfs/dokan/cryptdokan.cpp#L1949) → 库函数 [find_files, fileutil.cpp:312](libcppcryptfs/util/fileutil.cpp#L312)：

```
find_files(明文目录 C:\挂载点\docs)
  ├─ FileNameEnc → 密文目录，构造 "密文目录\*" → FindFirstFile 枚举（枚举的是密文名！）
  ├─ get_dir_iv 取该目录 IV（一次，所有条目共用）(fileutil.cpp:348)
  └─ 循环每个密文条目:
       is_interesting_name 过滤 gocryptfs 内部文件 (diriv/长名旁边文件) (:369)
       convert_fdata (fileutil.cpp:231):
          decrypt_filename(目录IV, 密文名) → 明文名
          adjust_file_size_down: 密文大小 - 18 显示为明文大小 (反向模式相反是 +18)
       通过回调推给 Dokan → 资源管理器显示
  （大小写不敏感挂载: 整个结果存入 CaseCache :417）
```

### 3.7 卸载

[unmount_crypt_fs, cryptdokan.cpp:2374](cppcryptfs/dokan/cryptdokan.cpp#L2374) → `DokanRemoveMountPoint` → Dokany 通知内核 → `Unmounted` 回调 → `DokanMain` 返回 → 挂载线程退出 → [MountPointManager](cppcryptfs/dokan/MountPointManager.h) 销毁 `CryptThreadData`（其中 `m_open_handles` 会兜底关掉所有泄漏句柄）。

---

## 4. 密钥体系（安全设计的学习样本）

```
用户密码
  │ scrypt(N=2^16, r=8, p=1, 盐)                ← CryptConfig::decrypt_key (cryptconfig.cpp:622)
  ▼
KEK（加密密钥的密钥）
  │ AES-256-GCM 解开 conf 里的 EncryptedKey
  ▼
主密钥 (32B, LockZeroBuffer 锁页内存)
  │ HKDF-SHA256 按用途派生 (crypt.cpp:416, 三种 info 字符串 :412-414)
  ├─► "EME filename encryption"        → 文件名加密子密钥
  ├─► "AES-GCM file content encryption"→ 内容子密钥 (InitGCMContentKey, cryptconfig.cpp:1075)
  └─► "AES-SIV file content encryption"→ SIV 64B 子密钥 (SivContext::SetKey, siv.cpp:47)
```

内存中的密钥保护（三层）：

| 机制 | 文件 | 作用 |
|---|---|---|
| [LockZeroBuffer](libcppcryptfs/util/LockZeroBuffer.h#L41) | LockZeroBuffer.h | `VirtualLock` 锁页 + 析构时 `SecureZeroMemory`，所有密钥唯一居所 |
| [KeybufManager](libcppcryptfs/util/KeybufManager.cpp#L69) | KeybufManager.cpp | "内存中加密密钥"选项：空闲时用 **DPAPI** 把密钥整体密封，进回调才解封（RAII 守卫 `KeyDecryptor`） |
| [KeyCache](libcppcryptfs/util/KeyCache.cpp#L61) | KeyCache.cpp | 跨挂载共享已解密密钥；后台线程每秒检查，闲置即清零——明文密钥只在活跃使用期间存在于内存 |

> **复刻要点**：即使不做加密文件系统，`LockZeroBuffer`（安全缓冲）和"句柄兜底回收表"（`m_open_handles`）这两个组件也值得原样抄走。

---

## 5. 缓存体系（性能的关键）

| 缓存 | 位置 | 容量 | 键→值 | 失效策略 |
|---|---|---|---|---|
| [DirIvCache](libcppcryptfs/filename/dirivcache.h#L59) | 每挂载 | 100 | 目录路径→16B IV | TTL + 过期时核对 diriv 文件写入时间（适配网盘同步场景） |
| [LongFilenameCache](libcppcryptfs/filename/longfilenamecache.h#L74) | 每挂载 | 5000 | 哈希名→真实路径 | 无 TTL（哈希→数据不会变脏），仅反向模式用 |
| [CaseCache](libcppcryptfs/filename/casecache.h#L63) | 每挂载 | 100 目录 | 大写路径→{大写名→正确大小写名} | 支撑大小写不敏感挂载 |
| [IoBufferPool](libcppcryptfs/file/iobufferpool.cpp) | **进程级单例**（≤70MB） | — | 多块读写的大缓冲+IV 数组复用 | 避免每次 IO 堆分配 |

三者都是同一 LRU 骨架：`unordered_map` + 侵入式 `list` + 空闲节点复用 + 命中率统计（`FsInfo` 对话框可见命中率，[cryptcontext.cpp:182](libcppcryptfs/context/cryptcontext.cpp#L182)）——**复刻时抄这套骨架即可得到全部三个缓存**。

---

## 6. 反向模式（Reverse Mode）—— 精妙的"只读密文视图"

正向：密文盘 → 挂出明文。反向：**明文盘 → 挂出密文**（用于把未加密数据以密文形式备份到云端）。

核心思想：**什么都不能写盘，一切密文按需现算**：

| 正向模式 | 反向模式 |
|---|---|
| 每目录有真实 `gocryptfs.diriv` 文件 | 虚拟 IV = SHA256(明文路径\|类型)[0..15]，[derive_path_iv, cryptfilename.cpp:72](libcppcryptfs/filename/cryptfilename.cpp#L72) |
| 每文件头存随机 fileid | fileid、block0iv 都由明文路径哈希派生（[cryptfile.cpp:807](libcppcryptfs/file/cryptfile.cpp#L807) `CryptFileReverse::Associate`） |
| 每块随机 IV | 块 IV = block0iv 低 64 位加块号（确定性，[cryptio.cpp:143-166](libcppcryptfs/file/cryptio.cpp#L143-L166)） |
| 数据落盘为密文 | 读到哪个块现加密哪个块（[CryptFileReverse::Read, cryptfile.cpp:852](libcppcryptfs/file/cryptfile.cpp#L852)） |

- 因为 IV 必须确定（同一明文两次"加密"必须产生相同密文供校验），**反向模式强制 AESSIV**（GCM 需要随机 IV，同一 IV 重用会彻底崩溃安全性）
- `gocryptfs.diriv`、`gocryptfs.longname.*.name` 这些"虚拟文件"不占磁盘，在 [find_files](libcppcryptfs/util/fileutil.cpp#L312) 枚举时合成、在 [read_virtual_file](libcppcryptfs/util/fileutil.cpp#L426) 读取时现生成
- conf 文件名映射：明文视图中的 `gocryptfs.conf` ↔ 磁盘上的 `.gocryptfs.reverse.conf`（[cryptdefs.h:58-60](libcppcryptfs/crypt/cryptdefs.h#L58-L60)）

> **复刻要点**：反向模式展示了"**文件系统视图与存储彻底解耦**"的极限——连磁盘文件都可以不存在，全部内容即时演算。理解它，你对"文件系统 = 一组回调 + 状态演算"的认知会完全成型。

---

## 7. UI 与 IPC（了解即可）

- GUI 是 MFC 属性表：Mount / Create / Settings / More Settings / About 五页（[cppcryptfs.cpp:359-397](cppcryptfs/cppcryptfs.cpp#L359-L397)），托盘图标常驻
- 命令解析中枢是 [CMountPropertyPage::ProcessCommandLine, MountPropertyPage.cpp:1321](cppcryptfs/ui/MountPropertyPage.cpp#L1321)——**启动参数和 IPC 命令走同一套 getopt 解析**（`--mount`、`--unmount`、`--transform`…）
- cppcryptfsctl 的 mount 命令不在本地执行，而是把命令行（含密码）经命名管道发给已运行的 GUI（[SendArgsToRunningInstance, libipc/client.cpp:51](libipc/client.cpp#L51)）；管道名 = `\\.\pipe\cppcryptfs_cmd_<会话ID>`，通信双方互相校验对方 exe 的 Authenticode 签名（[certutil.cpp:241](libipc/certutil.cpp#L241)）——防止恶意程序冒充管道客户端/服务端
- 密码输入框 [CSecureEdit](cppcryptfs/ui/SecureEdit.h)：真实密码只存于 `LockZeroBuffer`，控件里永远只有圆点

---

## 8. 复刻路线图：从 cppcryptfs 到你的自研文件系统

### 8.1 分层替换策略（推荐）

```
你的自研文件系统
├── 接入层（对应 cryptdokan.cpp）    ← 二选一：
│     A. 继续 Dokany（成熟稳定，商用需注意驱动签名）
│     B. WinFsp（另一流行方案，API 更现代）
│     C. WOF/CbFilter（微软官方过滤驱动，非完整 FS，适合透明加密场景）
├── 核心层（对应 libcppcryptfs）     ← 保留骨架，替换算法：
│     · 磁盘格式     自己定义（先写格式文档再写代码！）
│     · 加密原语     换 ChaCha20-Poly1305 / SM4-GCM 等，接口抄 crypt.h 即可
│     · 文件名策略   可简化（如只做 hash 名 / 不加密）
│     · 缓存骨架     直接抄（LRU 三件套 + IoBufferPool）
│     · 密钥管理     直接抄（LockZeroBuffer + DPAPI 密封）
└── 控制面（对应 ui/ + libipc + cppcryptfsctl）
      可换成服务 + CLI / REST API
```

### 8.2 必须先想清楚的 7 个设计问题（cppcryptfs 的答案）

| # | 问题 | cppcryptfs 的答案 | 出处 |
|---|---|---|---|
| 1 | 元数据/数据放哪？ | 无独立元数据，格式信息全在 conf + 文件头 + diriv 文件 | [cryptdefs.h](libcppcryptfs/crypt/cryptdefs.h) |
| 2 | 随机访问怎么做？ | 定长块密码学布局：明文 4KB ↔ 密文 4128B 严格一一对应 | §2.2 |
| 3 | 部分块写怎么办？ | 读-解密-改-重加密整块（代价：写必须先有读权限） | [cryptfile.cpp:537-575](libcppcryptfs/file/cryptfile.cpp#L537-L575) |
| 4 | 防块重排攻击？ | 块号+fileid 进 AEAD 的 AAD | [cryptio.cpp:53-59](libcppcryptfs/file/cryptio.cpp#L53-L59) |
| 5 | 名字超长/非法字符？ | 哈希短名 + 旁边文件存全名；解密后做字符白名单校验 | §3.3 |
| 6 | 同一文件并发读写？ | 每路径一把读写锁（shared_mutex），读改写期间独占 | [openfiles.h](libcppcryptfs/file/openfiles.h) |
| 7 | Windows 语义怎么填平？ | ZwCreateFile 里补 GENERIC_READ、paging IO 钳制、句柄兜底回收、大小写缓存 | [cryptdokan.cpp:310-721](cppcryptfs/dokan/cryptdokan.cpp#L310-L721) |

### 8.3 建议的最小可行版本（MVP）开发顺序

1. **纯算法层先行**（不碰 Dokan）：实现 `encrypt_path`/`read_block`/`write_block` 的等价物，写个命令行工具能把明文目录"原地加密/解密"——等价于先用好 [transform_path / --transform](cppcryptfs/dokan/cryptdokan.cpp#L2501) 这条路径做验证
2. **跑通 Dokan "mirror"**：从 [cryptdokan.cpp:2083](cppcryptfs/dokan/cryptdokan.cpp#L2083) 的回调表抄起，先实现不加密的透传 passthrough FS（Dokany 官方 mirror 示例即此），验证环境
3. **接通 ZwCreateFile + ReadFile + WriteFile 三个回调**（80% 的流量）+ 路径翻译层
4. **补齐元数据回调**：FindFiles / GetFileInformation / SetFileInformation / Cleanup / CloseFile
5. **补并发与边缘**：LockFile、安全描述符、流（NTFS ADS）、截断重加密
6. **最后加缓存与安全加固**

### 8.4 从本项目可直接复用的代码清单

| 组件 | 文件 | 复用价值 |
|---|---|---|
| 安全缓冲 | [LockZeroBuffer.h](libcppcryptfs/util/LockZeroBuffer.h) | ★★★ 原样复用 |
| LRU 缓存骨架 | [dirivcache.h](libcppcryptfs/filename/dirivcache.h) / [casecache.h](libcppcryptfs/filename/casecache.h) | ★★★ 换键值类型即用 |
| IO 缓冲池 | [iobufferpool.h](libcppcryptfs/file/iobufferpool.h) | ★★★ 原样复用 |
| 懒路径翻译 | [FileNameEnc.h](cppcryptfs/dokan/FileNameEnc.h) | ★★ 换翻译函数即用 |
| 每路径文件锁 | [openfiles.h](libcppcryptfs/file/openfiles.h) | ★★★ 原样复用 |
| AEAD 封装 | [crypt.cpp](libcppcryptfs/crypt/crypt.cpp) | ★★ 换算法改这里 |
| 回调表全样板 | [cryptdokan.cpp:2082-2121](cppcryptfs/dokan/cryptdokan.cpp#L2082-L2121) | ★★★ Windows 语义的完整答案 |
| DPAPI 密钥密封 | [KeybufManager.cpp](libcppcryptfs/util/KeybufManager.cpp) | ★★ |

---

## 9. 推荐阅读顺序（按天）

| 天 | 内容 | 要达到的状态 |
|---|---|---|
| D1 | §0-§2 + 通读 [cryptdefs.h](libcppcryptfs/crypt/cryptdefs.h)（71行）+ 用 cppcryptfs 真机创建/挂载一个卷，用十六进制查看器看密文 | 能画出磁盘格式图 |
| D2 | §3.1-3.2 + [cryptconfig.cpp](libcppcryptfs/config/cryptconfig.cpp) 的 create/read/decrypt_key | 讲清"密码如何变成三把子密钥" |
| D3 | §3.3 路径链：[FileNameEnc.cpp](cppcryptfs/dokan/FileNameEnc.cpp) → [cryptfilename.cpp](libcppcryptfs/filename/cryptfilename.cpp)（只看 4 个入口函数）+ [eme.cpp](libcppcryptfs/crypt/eme.cpp) 的 EmeTransform | 手算一个名字的加密流程 |
| D4 | §3.4-3.5：[cryptfile.cpp](libcppcryptfs/file/cryptfile.cpp) + [cryptio.cpp](libcppcryptfs/file/cryptio.cpp) | 解释"为什么写文件需要读权限" |
| D5 | §3.6-3.7 + [cryptdokan.cpp](cppcryptfs/dokan/cryptdokan.cpp) 通读回调表和 ZwCreateFile/FindFiles | 每个 Dokan 回调对应什么 Win32 调用 |
| D6 | §4-§5：[LockZeroBuffer.h](libcppcryptfs/util/LockZeroBuffer.h)、三个 cache、[KeybufManager.cpp](libcppcryptfs/util/KeybufManager.cpp) | 理解安全与性能工程 |
| D7 | §6 反向模式 + §8 复刻规划；动手写自己的 MVP 第 1 步 | 开工 |

**高效技巧**：
- 不要线性读 2900 行的 cryptdokan.cpp——按回调表（[:2083](cppcryptfs/dokan/cryptdokan.cpp#L2083)）当索引导航，用到哪个读哪个
- 三个"只读入口"适合设断点观察：`CryptCreateFile`、`CryptReadFile`、`find_files`，挂载后用记事本打开一个文件就能看到完整调用链
- 对照 gocryptfs 源码（Go）看同名概念，两边互为注释；本项目大量函数移植自 gocryptfs（如 pad16 注释所示）

---

## 10. 常见陷阱（前人踩过的坑）

1. **改格式常量 = 与 gocryptfs 决裂**：`PLAIN_BS`/`CIPHER_BS`/`FILE_HEADER_LEN` 等动一个字节，gocryptfs 就挂不上你的卷（反之亦然）。自研时把这些常量集中放进你的"cryptdefs.h"
2. **不能在回调里直接返回错误给用户态**：必须 `ToNtStatus`（[cryptdokan.cpp:232](cppcryptfs/dokan/cryptdokan.cpp#L232)）把 Win32 错误映射成 NTSTATUS
3. **Dokany 句柄生命周期 ≠ 文件打开周期**：`Cleanup`（最后一个句柄关）和 `CloseFile`（Context 可丢弃）是两个时机；本项目还处理了"句柄丢失后 paging IO 重开"（[cryptdokan.cpp:808-820](cppcryptfs/dokan/cryptdokan.cpp#L808-L820)）
4. **Windows 会以奇怪的偏移和标志调你**：`bPagingIo` 到 EOF 直接 no-op（[:881-887](cppcryptfs/dokan/cryptdokan.cpp#L881-L887)）；`SetEndOfFile` 之后最后一块的密文长度必须同步重加密
5. **大小写敏感性**：NTFS 不区分大小写，加密后的名字解回来大小写可能对不上——CaseCache 整套机制就是为此存在；自研若在 Linux（区分大小写）上则不需要
6. **性能第一刀在 IV 生成**：逐块调 CSPRNG 会把写拖垮，本项目一次抽齐所有块 IV（[cryptfile.cpp:492](libcppcryptfs/file/cryptfile.cpp#L492)）
7. **零块 = 洞**：文件系统扩展文件先填零，此时密文块全零、无标签。读时全零块验签失败按零处理（[cryptio.cpp:99-111](libcppcryptfs/file/cryptio.cpp#L99-L111)），否则新文件读出来全是校验错误

---

*文档基于 commit b3cc714（版本 1.4.4.10）的代码行号生成；若代码演进导致行号漂移，以函数名搜索为准。*
