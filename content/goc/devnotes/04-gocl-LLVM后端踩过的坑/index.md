---
title: "开发笔记：gocl（LLVM 后端）踩过的坑"
menuTitle: "gocl（LLVM 后端）踩过的坑"
date: 2026-10-07T01:20:00+08:00
draft: false
weight: 4
tags: ["goc", "gocl", "LLVM", "libLLVM", "COFF", "PE", "链接器", "AT&T", "TLS", "开发笔记"]
categories: ["编程开发", "goc", "开发笔记"]
description: "把 goc 的 C 代码交给 LLVM 编译、再由自研链接器合成 exe，这条路上踩过的坑：libLLVM 的 FFI 约定、LLVM IR 的畸形输出、COFF 重定位丢失 addend 导致控制台全哑、printf 特化的架构缺口、PE 段级文件对齐、TLS 访问被当成 extern 全局、以及 AT&T 前端的操作数方向不能一律翻转。"
---

> 这是一篇**开发笔记**，不是教程。记录的是把 goc 的 C 代码交给 LLVM 编译、再用自研链接器合成可执行文件这条路上踩到的坑——大部分是「读错字段不会报错、只是结果不对」那一类。教程正文里不写这些。

## 起因

goc 的默认后端是自研的 **goa**：手写 x86-64 代码生成 + 一条优化管线，产物是 PE32+ 或静态 ELF64，不依赖 gcc 也不依赖 libc。

后来想验证一件事：**LLVM 能不能替掉 goa 后端**。如果能，就等于免费拿到一整套中端优化、跳转表、向量化。做法是让 LLVM 把整个程序（用户函数 + C 库全部函数 + 全局变量）编译成单个 COFF 对象，再由 goa 贡献入口桩和镜像布局，把两者链接成 exe。

这条路子听起来只是"换个后端"，实际踩出来的坑比预想多得多——因为 **LLVM 吐出来的 COFF 对象要由我这个半吊子链接器吃下去**，而 COFF/PE 里"读错字段不报错"的地方特别多。

这篇按主题分节，每节是若干条坑。**每条都尽量写清"现象 → 根因 → 修法"**，因为这类坑的共性就是：现象离根因很远。

## 一、libLLVM 的 FFI：没有 cgo 怎么调

gocl 不链接 libLLVM，而是运行时用 `syscall.LazyProc` 动态加载。好处是 goc 本身不依赖 LLVM 装没装，坏处是所有 ABI 约定都得自己记对。

### 坑 1：`LLVMCreateTargetMachine` 返回 NULL 直接崩进程

少调一个初始化函数：`LLVMInitializeNativeTarget` / `NativeAsmPrinter` / `NativeAsmParser` 都调了，AsmParser 也注册了，但 **`LLVMTargetHasAsmBackend()` 返回 0**，随后 `LLVMCreateTargetMachine` 空指针崩溃——无异常、无诊断。

根因是 x86 后端要单独初始化 **TargetMC**。AsmParser/AsmPrinter 注册了但没有 MC 层，AsmBackend 标志就是 0。

初始化序列必须完整且有序：`TargetInfo → Target → TargetMC → AsmPrinter → AsmParser`。

### 坑 2：`LLVMGetTargetFromTriple` 的出参顺序和头文件相反

按头文件写的 `(triple, &err, &target)` 传，拿到垃圾指针，**而且返回码仍然是 0**——看起来"成功了"。真实签名是 `(triple, &target, &err)`。

附带一个容易误判的点：它返回的 target 是 `TargetRegistry` 里的静态存储，**地址看着像栈地址（`0x7ffc…`）是正常的**，别当成野指针去查。

### 坑 3：返回指针的函数不能判 `rc != 0`

`LLVMCreateTargetMachine`、`LLVMCreateMemoryBufferWithMemoryRangeCopy` 明明返回非空，却被自己的错误检查报成"失败"。

这些函数返回的就是指针本身，非 0 即成功，没有状态码语义。按状态码判必然误报。**判指针非空**（`llvm.go:331` `if tm == 0`、`llvm.go:346` `if buf == 0`）。

> 顺带纠一个写文时的笔误：这里原本写的是 `LLVMCreateMemoryBufferWithContentsOfFile`——那个函数在本项目里从未绑定过（全仓库 grep 零命中）。真正的调用点是 `LLVMCreateMemoryBufferWithMemoryRangeCopy`（绑定在 `llvm.go:61`，唯一调用点 `llvm.go:343-348`）。记下来是因为这类"名字看着合理"的错最难自己发现。

同类还有 `CPU` / `Features` 参数：**必须传空字符串，传 NULL 会让整个编译器段错误**。LLVM 23 不做 null 检查，直接解引用。同一个函数，ctypes 传空 bytes 能过、传 `0` 就崩——这个差异极难猜。

`LLVMCreateTargetMachine` 在 LLVM 23 还是 **9 个参数**（末尾多了 `ThreadCount`），按文档的 8 参数传会读到垃圾。

### 坑 4：`LLVMRunPasses` 调不动——无 cgo 方案的真正技术死点（**已解决**，见本节开头补记）

> **补记（2026-10-07）**：本条当时判断为「限制不是 bug」，**后来被推翻并修好了**。当时卡在 `LLVMStringRef` 这个16 字节聚合按值传（MSVC x64 下等于间接传指针，实际调用形态是 5 个指针参数），Go 变参 `LazyProc.Call` 表达不了「聚合按值传」这条 ABI 规则。修法很轻：**改用 `const char*` 传管线字符串**，4 个参数全是 `uintptr` 指针，ABI 墙直接消失。commit `1ba351a` 已接入，`src/gocl/llvm.go` 现有 `runPasses.Call(mod, passC.ptr(), tm, optv)`。**下面的原始记录保留作为当时判断的依据。**

`LLVMStringRef` 是 16 字节聚合，MSVC x64 ABI 下按值传递等于间接传指针，实际调用形态是 **5 个指针参数**。4 参 / 5 参 / 直接传 `StringRef` 结构体，全都崩。

Go 的 `LazyProc.Call` 是变参 uintptr 调用，**无法表达"聚合按值传"这一 ABI 规则**。

结论是这是**限制不是 bug**：长期只能拿到 TargetMachine 的 `CodeGenOptLevel`，拿不到中端优化管线（GVN / LICM / 向量化）。所有优化必须靠 `irPasses()` 返回的管线字符串。手工拼栈传参可行，但风险过高，没做。

> 附带：必须用 `LazyProc.Call` 而不是 `syscall.SyscallN`，因为后者要传裸 `uintptr`，读回还得转 `unsafe.Pointer`，而 **CI 有 `go vet` 的 unsafeptr 门禁**，必然红。
>
> **现状**：`irPasses()` 不再是「唯一」优化途径，而是**通过 `LLVMRunPasses` 真正跑起来了**。`LLVMCodeGenOptLevel` 只管机器码生成强度（传给 `LLVMCreateTargetMachine`），中端 IR 优化由 `irPasses(opt)` 的管线字符串 + `runIRPasses` 负责，两者是分开的。

### 坑 5：libLLVM.dll 依赖 libzstd.dll，且文件名大小写敏感

`LoadLibrary` 失败。这条坑要分成两半讲，因为**当初以为有效的那个修法后来被自己的排查记录推翻了**——这正是"修好了"和"绕过了症状"的区别。

**第一半：库名只认三个拼法。** `llvm.go:127` 的查找表就是 `{"libLLVM.dll", "LLVM.dll", "libLLVM-9.dll"}` 三个，加上 `GOC_LLVM_DLL` 环境变量优先（`llvm.go:121`）。所以 `libllvm.dll` 小写根本不会被尝试——Windows 的文件系统不区分大小写，但 `LoadLibrary` 的**参数**区分。

**第二半（真正卡住的地方）：把依赖全拷到 exe 同目录，无效。** 最初以为 `libLLVM.dll` 依赖 `libzstd.dll`，那就把它拷到 `bin/`。实测：把 6 个 MSYS2 依赖全部拷到 exe 同目录，逐个 `LoadLibrary` 全部成功，仍然返回 127。原因是 `libLLVM.dll` 有 21 个直接依赖，失败在**二层的传递依赖**上——直接依赖都加载成功了，加载器再去解析它们的依赖时找不到某个 dll，整条链失败。

**最终解法是绕开环境**：`GOC_LLVM_DLL` 指向 MSYS2 的完整环境 `D:/msys64/ucrt64/bin/libLLVM-22.dll`（注意是 `msys64`，不是曾经写错的 `msys`）。让 DLL 待在它自己的依赖旁边，比手工复制依赖树可靠得多。

> 这条坑的教训比结论有用：**"多拷几个 dll 到 exe 旁边"是个看起来很专业的动作，它确实能让 `LoadLibrary` 单点成功，但并不保证整条传递依赖链成立**。判断依据应该是失败码：127 是"某个依赖找不到"，而不是"你自己那个 dll 有问题"。

### 坑 6：静态链接 libLLVM 省 29MB，但运行时崩（**路线已放弃**）

下表记录的是 2026-10-04 一时的中间态，**现已不是现状**，保留是因为它解释了后面几个决策的动机：

| 方案 | 大小 | 说明 |
| --- | --- | --- |
| 动态（当时） | gocl.exe 3.2MB + libLLVM-23.dll 109.8MB | **128.4 MB**，两个文件 |
| 静态单个 exe | **99.2 MB** | 一个文件，省 29MB / −23% |

静态版在 `LLVMInitializeX86Target()` 之后段错误。

排除了"库不全"（X86 后端在 `libLLVMX86CodeGen.a`，而 `libLLVMTargetX86.a` **根本不存在**）和"符号缺失"。崩溃点是 `TargetRegistry` 的 **C++ 全局构造顺序**问题——`--start-group` 能解库间循环依赖，解不了 C++ 静态初始化顺序。

**判断**：这是工具链限制不是代码 bug。真要攻，方向是 MinGW 的 `.ctors` 排序（`-Wl,--sort-section=name`），或者改用 clang/lld 构建 LLVM（它们的 `.CRT$XCU` 优先级支持完整，很可能直接能跑）。

**后来怎么样了**：这条路线**已经放弃**，`build.sh` 与 `tools/` 下没有任何静态链接 libLLVM 的痕迹（grep `static` / `libLLVMTargetX86` / `start-group` 零命中）。现行方案是动态加载 + 组件裁剪：`LLVM_DYLIB_COMPONENTS` 把 libLLVM 从109.8MB 裁到约 22MB。上面那个"动态 = 109.8MB"的基线早已不存在——省体积的动机换了个实现，而"静态链接省 29MB"这个数字在今天没有参考价值。

顺带记一个容易漏的清单：静态链接需要的系统库里有 **`ole32`**（CoTaskMem/CoInitialize）、`oleaut32`、`ntdll`（`RtlGetLastNtStatus`）、`uuid`（`CLSID_FileOperation` / `FOLDERID_*` GUID），漏了都是链接期才报。

### 坑 7：LLVM 23 的 C API 导出面残缺（66/78 可用）

真正仍然成立的缺口：`bind()`（`llvm.go:148-174`）只绑定 24 个符号，`LLVMCreateTarget` 是 C++ 符号未导出 → 改用 `LLVMGetTargetFromTriple`（`llvm.go:294`）；`LLVMGetNumFunctions` / `LLVMGetFunction` / `LLVMGetTargetMachine` 不存在。

> 需要从原文里划掉的一半：`LLVMBuildLoad` → `LLVMBuildLoad2`、`LLVMGetConstInt` → `LLVMBinaryOperator` 这两条**与本项目无关**——gocl 的 IR 前端是**生成文本 IR** 再交给 `LLVMParseIRInContext`（`llvm.go:164`、`llvm.go:351`）解析，从不建 IRBuilder，所以这批 builder API 缺不缺根本影响不到我们。那是早期尝试用 builder API 时的记录，架构改成文本 IR 后就作废了。**教训**：记"某个 API 缺失"之前先确认自己有没有在用它。

**换 LLVM 22 救不了变参**：22 同样不接受 `vaarg` 指令关键字（两版报同一个 `expected instruction opcode`）。22 只多一个 `LLVMWriteBitcodeToFile`，价值有限。

## 二、LLVM IR：畸形输出的重灾区

这一层产出畸形 IR。**几乎每一条都是 LLVM verifier 精确指出来的**——给 82 个 `src/examples` 跑一遍 `goc -fllvm -S`（走 verifyModule + AsmPrinter），基线是 57 OK / 25 FAIL。比逐个猜语法快得多。

### 坑 8：运算符名不是 C 的拼写

直接透传 `+ - * / % & | ^` 被拒，报 `expected instruction opcode`。LLVM 叫 `add sub mul udiv/sdiv urem/srem and or xor`——**除法/取余有符号无符号各一套**，必须按类型选变体。

### 坑 9：`call` 的每个参数必须显式标注类型

同模块后面才定义的函数（**递归**）报 `invalid type for function argument`；补 `declare` 又报重定义。**两条路都不通**——LLVM 只从**前置 declare** 推断函数类型。唯一解是每个实参写成 `i32 %t3` 形式显式带类型。

### 坑 10：比较结果是 `i1` 不是 `i32`

控制表达式拿 i1 去 `icmp ne ..., 0` 类型不符；`ret i32 %i1` 非法。LLVM 的 `i1` 没有"提升成 int"的自动转换。

修法是引入独立布尔类型标记（`boolIr()` → `KBool → i1`），i1 参与算术前先 `zext`。

### 坑 11：`binaryType` 对纯算术一律 `return nil`（本轮最关键）

`(a+b)`、`x/64` 拿到 nil → `e.ty(nil)` 回落 `"i32"` → 发出 `sdiv i32` 配 i64 操作数。

根因很微妙：运行时 `binary()` 自己用 `arithCommon` 算对了宽度，**错的是"嵌套表达式作为操作数时"的静态类型查询**。修法是让 `* / % & | ^` 返回 `arithCommon`、`<< >>` 返回左操作数提升类型、比较返回公共类型。

同类还有一串：下标未扩展成 i64（`toI64`：signed→`sext`、unsigned→`zext`、ptr→`ptrtoint`）、`toInt` 对整型常量误发 `ptrtoint`（只对 `KPtr` 才发）、移位计数宽度必须与被移值同宽、`exprType` 漏了 `Unary -` / `~` / `AssignExpr` / `CondExpr` / `NumLit` / `StrLit` 这些节点。

### 坑 12：`NumLit.IsFloat` 语义误用

`3.14159`（**无后缀**）被走整数分支 → 输出 `double 0`；NAN/INFINITY 宏同理，20 多处 `sdiv i32 0, 0`。

`IsFloat` 只表示"有 f 后缀 → 宽度是 float"，**不表示"是否浮点"**。判浮点一律用 `Kind == TDouble`。这个坑在代码审查阶段被独立踩到两次。

### 坑 13：struct 尾部 padding 成员是错的

`struct S{char*p; int n;}` 被发成 `{ptr,i32,[8 x i8]}`（24 字节，C 里是 16）。根因是自行用 `t.Size − 成员 size 之和` 补 pad——**LLVM struct 本来就按 datalayout 自动补齐**。删掉即可。

union 类似的错：把"最宽成员宽度"当追加 padding，`union U{char*p; int x;}` 变 16 字节（C 是 8）。pad = size − **首成员**；且 union 初始化按 C 语义**只填第一个成员**。

### 坑 14：聚合常量语法的两个硬约束

嵌套 struct 必须带类型名（`%point { i32 1, i32 2 }`），跳过元素必须写 `zeroinitializer`。裸 `{...}` 报 `expected '}'`。

这两条不是猜的——是搭了个**临时 IR oracle**（直接拿 `.ll` 文本喂 `CompileToObject`）实证出来的，比查文档快。

### 坑 15：`switch` 里三个独立 bug

① 无条件 `trunc`：`trunc i32→i32` 和 `trunc i8→i32` 都非法；
② default 语句被塞进上一个 case；
③ 用 `*switchCase` **指针**跟踪当前分支，`append` 扩容使指针失效 → default 的语句跑到别的 arm 的 label 下。

第三个最阴：指针在 append 扩容后指向旧内存。改用 `cur int` 索引跟踪。

### 坑 16：函数名用作值时 decay 成 null

`printf_lite_with(vfmt_i, ...)` 这种"传格式化函数"的写法生成 `store ptr null` → **空指针跳飞**。

`irEmitter.ident` 末段的注释明明写着「a function used as a value」，但代码走 `zeroLiteral`。**不是 printf 专属**：任何 `int (*fp)(int) = twice` 在 LLVM 后端下都段错误。

修法：`typeResolver.isFuncName` + `irEmitter.fnPtrTy`，且——**LLVM 里函数符号本身就是其地址，转换结果就是裸符号，不做 load**。

### 坑 17：decay 规则（约 10 个 examples 的最大类）

两种方向都错：

- 数组/全局该取基址却发 load：`load i32, ptr @G_g_arr` 然后当指针用；
- 全局标量该 load 却漏 load：`icmp ne i32 %t11, @G_g`。

根因是 `llivrexpr` 的 ident/rvalue 取值路径 + decay 规则。修法是在**早期为所有全局注册类型**（名字类型决定下标是否 decay、标量是否 load，错了是畸形 IR 而非缺符号）。这条和 lib 全局裁剪一起修的——**类型注册不裁剪，定义才裁剪**。

### 坑 18：`alignOfLlir` off-by-one

判据写成 `len(ty) > 5 && ty[:5] == "float"`，`5 > 5` 恒假 → **float/double 永远得 align 1**；顺带 i1 会得 align 0（非法）。修正判据并把 align 钳到 `>= 1`。

## 三、变参（va_list）：模型冲突

### 坑 19：`printf("%d", x)` 崩，`printf("hi")` 不崩

只读取变参的程序崩，不读变参的正常。

根因是 **va_list 模型不匹配**，但**模型冲突发生在 Linux，不是 Win64**——这一点原文写反了，也是理解整章的前提。

两个目标的 `va_start` 行为完全不同（`expression.go:90-94` 的注释就是权威说明）：

- **Windows x64**：`llvm.va_start` 写入的就是**一个 8 字节指针**，指向调用方的寄存器保存区，每个变参占一个 8 字节槽（先通用寄存器，过后是栈上）。读一个参数就是"读游标 → 取值 → 游标 += 8"。**和 goc 的 `char*` 平坦游标完全一致，本来就不冲突**。
- **x86-64 SysV（Linux）**：`va_list` 是 `struct __va_list_tag[1]`，含 `gp_offset` / `fp_offset` / `overflow_arg_area` / `reg_save_area` 四个字段（`expression.go:96-101`）。如果按 Win64 的平坦游标去读 `gp_offset`，等于把"下一个通用寄存器槽的偏移"当成指针本身——Linux 上每个消费变参的调用都会崩。

最初把 `va_arg` 按平坦游标写，在 Win64 上其实是对的、在 Linux 上全错；两边共用一套前端展开代码，就必须在 `e.c.linux` 上分叉（`expression.go:127-129`：`if e.c.linux { return e.vaArgSysV(ap, lty, ty) }`）。

而 **LLVM 23 已不接受 `vaarg` 指令关键字**（`ExpandVariadics` pass 只做展开），`@llvm.va_arg(ptr, [i32, i8*])` intrinsic 也被拒。clang 同样是前端自行展开 → 只能按目标 ABI 在前端展开。

Win64 崩溃的实际修复是 `5bbde19`（"LLVM Win64 chkstk stub contract + strLit NUL + **vaArg flat cursor**"）——把 Win64 明确走平坦游标，而不是反过来去迁就一个错误的模型。

### 坑 20：`va_list` 局部量必须给 24 字节存储

`va_list` 的存储被统一加宽到 24 字节（`function.go:314` `const vaListTy = "[3 x i64]"`，`function.go:398` `alloca [3 x i64]`）。**但两条目标的理由不同**：

- **SysV**：`llvm.va_start` 真的写四字段结构，24 字节是必需的。
- **Win64**：intrinsic 只写 8 字节，加宽是**防御性的**——`function.go:308-311` 的原话是"这个槽仍然加宽到 24 字节，这样 intrinsic 永远不会覆盖掉紧随其后的三个局部量，**无论未来的目标往里写什么**"。Win64 上 `vaListSlot` 对参数甚至直接 `slotFor(uid, "ptr")` 返回 8 字节槽（`function.go:351-356`，`if !e.c.linux { return slot }`）。

第一版就栽在一致性上：`va_start` 写 `%t11`、实参求值读 `%t10`，而 `%t10` **从未写过**。修法是新增 `vaListSlot` / `vaSlots` / `paramNames` 三个字段，保证 `va_start`、每个 `va_arg`、`va_end`、`va_copy`、以及**把 ap 传给别的函数**时都落在同一个槽（`va_start`/`va_arg`/`va_end`/`va_copy` 的调用点：`call.go:137,145,149-170,166-167,205,360`）。

### 坑 21：无优化管线时 Win64 变参丢浮点实参

`printf("%f", 1.25)` → `0.000000`。

Win64 要求浮点实参**同时**进 XMM 与整数寄存器，无管线版本缺这段搬运，goc 的扁平 va_list 读不到。

**这直接决定了 `-O` 映射必须是"无 `-O` 也走 `default<O1>`"**——是正确性要求，不是性能选择。既修正确性又不增体积（`-O1` 的 bench2 16896B < 无管线的 19968B）。

顺带修了个映射错误：`-O1` 原本映射到 `LLVMOptLess`，**比不带旗标的 `LLVMOptDefault` 还低**，等于"要求优化反而更慢"。

### 坑 22：跨平台 `va_list` 传递：Win64 按值传正确，SysV 必须按引用（**已按架构拆开**）

原文这条写的是"Win64 ABI 要求 `va_list*`，属已知限制"。**结论是反的，而且限制已经消除。**

- **Windows x64**：`va_list` 就是 `char *`，参数里装的就是游标本身，按值传**正是 ABI 要求**。`function.go:330-332` 的原话："`va_listSlot` 绑出来的槽已经是 8 字节宽、已经装了正确的值，**所以它的地址就是答案**"。`valist.go:11-13` 补充："在 Windows x64 上这个歧义无害——`va_list` 的值**就是**游标指针，正好等于读一个 `char *` 会得到的东西。"
- **x86-64 SysV（Linux）**：`va_list` 是 `struct __va_list_tag[1]`，**数组类型**，作为参数会衰变成指向 tag 的指针，且这 24 字节住在**调用者**的栈帧里。`function.go:334-341` 明确了这一点，修法是先 `load ptr` 取出指针（`function.go:357-362`），写回也落进调用者的 tag。

所以 goclib 的 `printf_lite_with(..., va_list ap)` 按值传这个签名（`stdio.c:526`）**在两个目标上都是对的**，不需要改 C 代码。要改的是后端：Win64 分支直接返回已含正确游标的槽地址，Linux 分支才做 `load ptr`。跨函数传递的两条路径都覆盖了——直接调用 `call.go:205`，间接调用 `call.go:358-362`（同样只在 Linux 下走 `vaListSlot`）。来源 commit 是 `7f22b64`「feat(gocl): Linux ELF 后端 —— SysV va_list ABI + ELF 目标输出」。

> 附带一个当时没做的：`va_copy`（C99 7.16.1.1）现在也实现了（`call.go:149-170`）——它让`va_list` 能独立复制、先量长度再输出而不消耗原件。`stdarg.h:37-41` 在 Win64 下把它定义成指针赋值 `((dest) = (src))`，在 Linux 下**故意不定义**，让 codegen 落到 target-aware 的 `llvm.va_copy` intrinsic。

## 四、COFF / PE：读错不报错的重灾区

这一节是全部坑里最值得写的——COFF/PE 里"读错字段不会报错、只是结果不对"的地方特别多。

### 坑 23：COFF 符号名判断反了 —— 一个符号都解析不出来

COFF 的 8 字节 name 字段，**首 4 字节非 0 就是内联文本**，**首 4 字节为 0 才是字符串表偏移**（长名字）。段名判断同理。

原写法是"非 0 = 偏移"，判断整个反了。这个错误的特性是：不崩溃，只是所有符号都指向无关字节。

### 坑 24：符号表必须索引对齐 —— aux 记录也占一个符号槽位

重定位引用的索引整体前移，指向错误符号。量化过：`nsym=23` 但只有 16 个非 aux 条目，**差 7 个** = 6 个段符号的 aux + 1 个 FILE 的 aux。

读的时候要 `i += 1 + nAux`，且 aux 要占位。

### 坑 25：`IMAGE_RELOCATION` 字段顺序是 `{VirtualAddress(4), SymbolTableIndex(4), Type(2)}`

按 `(off, type, sym)` 读，偏移看着正常但符号索引是 `0x240004` 这种垃圾值。**符号索引在中间**；且 **Type 只有 2 字节**，用 rd32 读会吞掉下一条记录的前 2 字节。

### 坑 26：重定位语义换算（goa fixup ↔ COFF）

原文这两条 `ripAdj` 数值都是错的（写成了 `ripAdj = -4` / `-(off+4)`），而且"不需要改 `applyFixup`"这句也不对——**实际改了**，新增了 `Absolute` 字段。以 `coffmerge.go` 为准：

- **COFF `REL32`** → `Fixup{Sect, Off, Sym, Addend}`，`RipAdjust` **保持 0**（`coffmerge.go:606-648`）。为什么不需要那个 -4：微软 PE 规范把 `P` 定义为**字段开头**，而硬件执行完指令后的 RIP 是**字段末尾**，差 4 字节。goa 的公式本就以字段末尾为基准，所以`RipAdjust = 0` 正好对上 CPU 实际算的 `S - P`（`coffmerge.go:607-613` 的注释把这段推导写全了）。
- **COFF `ADDR32NB`** → `Fixup{..., Absolute: true}`（`coffmerge.go:679-682`），`RipAdjust` 同样是 0。因为它要的是目标的**绝对 RVA**，`P` 直接消掉，根本不是相对算法。这个 `Absolute` 是新字段（`image.go:83-85`），并且**`applyFixup` 为它加了独立分支**（`fixup.go:33-47`：`addr := uint64(target + f.Addend)` 直接写绝对 RVA）。
- **第三种类型**：还有 `ADDR64`（`relAMD64Addr64 = 0x0001`，`coffmerge.go:47`、`683-691`），走 `Absolute + Wide + Virtual`，`fixup.go:38-42` 为它加上 `ImageBase`——因为首选基址 `0x140000000` 在 4GB 以上，32 位字段会截断。所以链接器实际支持 3 种重定位，不是坑 32 说的 2 种（那是实测那个对象的结论）。

另外重定位 offset 是**"COFF 段内偏移"**，必须加上该段在 goa 段内的 `baseOf`，否则所有补丁提前几十字节、**污染的是无关代码而不是报错**。

> 原文这三条与坑 27 自相矛盾：坑 27 写的是"故只需 addend = 字段值、**ripAdj 保持 0**"，那个是对的。已按源码统一，`RipAdjust` 这个字段本来就不是为 COFF REL32 建的——`git log -S"RipAdjust" -- src/gocld/` 只有 `fbc0b42`、`7f22b64`、`407154c` 三次，都不是 COFF 路径。

### 坑 27：COFF REL32 分支没读 addend —— 控制台输出全哑、文件写正常

这是最值得写的一条"IR 全对但运行时结果错"。

**现象**：LLVM 后端下**所有控制台输出全丢**（puts / printf / putchar / fprintf(stderr)），显式 `fflush`、写 2000 字节、甚至 `> file` 重定向都没输出；但 **fopen + fwrite + fclose 完全正常**（out.txt 内容正确）。细节：`putchar` 返回 EOF 且置 `_err`；`fwrite` 返回 0 且不置错。

用户代码直接 `GetStdHandle(-11)` + `WriteFile` 能正常打印。

**定位三件套**：

1. **运行时探针**：镜像 FILE 布局写 `struct F{...}`，强转 `stdout`，把字段用 exit code 传出。结果 **native 位图 = 51**（fd+writable+base+off==-1），**LLVM 侧 = 1**（只有 fd 非 0）→ `_writable/_base/_size/_off` 全是 0。完美解释症状：`fwrite` 因 `!f->_writable` 直接 return 0（不置错），`fputc` 同样短路并置 `_err`。而文件 I/O 走 heap FILE 逐字段赋值，故不受影响。
2. **`objdump -d -r t1.obj`** 看 addend，**`objdump -d t1.exe`** 看落点。
3. **落点对照表**：偏移 0 的引用全对（`stdin_file` / `stdout_file` / `out_buf`），**带偏移的全错**（`stdin_file+32` → 落到 `stdin_file+0`）。

**根因**：`coffmerge.go` 的 `case relAMD64Rel32:` **没有读 addend**（旁边 ADDR32NB 分支读了）。

**COFF 重定位条目本身没有 addend 字段** —— `A` 就存在待修补字段的当前字节里，必须 `rd32(cs.data, off)` 读出来。语义 `S + A - P`，**P = 字段末尾(off+4)**，且 **A 已含 trailing 补偿**（实测：`mov %rax, sym+32(%rip)` → A=32, trailing=0；`movq $imm, sym+8(%rip)` → A=4, trailing=4 → 4+4=8；`call` → A=0）。

goa 的 fixup 公式是 `CPU目标 = symRVA + addend + trailing − ripAdj`，故只需 **addend = 字段值、ripAdj 保持 0**。`call` 的 A=0 所以不受影响——**这也是为什么入口桩的 `call` 一直是好的**，这个"为什么偏偏是它没事"是定位的关键线索。

**教训**：现象"只有控制台写失败、文件写成功"很容易误导向 FILE 层/缓冲逻辑（第一版就误判成"goclib stdio 的 `_pos` 维护"）。但**字段读出 0 说明是"写入没落到该落的地方"，应立刻怀疑重定位/地址而非业务逻辑**。

`-fllvm -S` 落 `.s` + `objdump -d -r` 是最快的定位路径。

### 坑 28：`.bss` 段没有文件字节，必须用 `vsize` 推进游标

只用 `len(data)` → `.bss` 段 `cur == 0` → `BuildPE` **跳过该段** → **符号解析到下一个段的地址**（`.bss` 的 `counter` 撞上 `.pdata`）→ 运行时访问冲突。

典型的"不报错的静默错位"。

### 坑 29：`.pdata` / `.xdata` 曾合并进 merged blob，现改为**合并但不映射**（**方案已反转**）

坑因是对的：unwind 表项存的是"**相对自己段起点的 RVA**"，搬进共享 blob 会让每一条都失效。

**第一版修法**：`planUnwindSections`（`pe.go:70`）/ `unwindSectionOut`（`pe.go:101`）/ `imageEndOf`（`pe.go:50`）规划独立段，异常目录（data directory index 3）写 `pdataRVA / pdataSize`（`pe.go:512-518`），`Fixup` 加 `absolute` 字段（`ADDR32NB` 要写绝对 RVA，不能走"target − 字段位置"的相对算法）。这些函数和字段今天都还在。

**但这个方案已经被推翻。** 现在 `coffmerge.go:279-281` 给这两个段打了 `Unmapped = true`：

```go
if name == ".xdata" || name == ".pdata" {
    gs.Unmapped = true
}
```

而 `pe.go:78-83` 把 Unmapped 段置 nil → `planUnwindSections` 不给它们分配地址 → `img.PdataSize` 停在 0 → `pe.go:512` 的 `if img.PdataSize > 0` 不成立，**异常目录压根不写**。注意 `coffmerge.go:275-278` 仍会合并它们的符号并应用重定位，只是字节不进镜像。

**为什么反悔**（`coff.go:26-34` 的原话）：表很小（每个函数 12 字节 `.pdata` + 约 11 字节 `.xdata`），但 PE 的一个段在文件里要占 `FileAlignment` 的整数倍，而 **512 是 Windows 接受的最小值**——三个函数就是 1024 字节，只为存72 字节的表。`image.go:46-52` 补上了代价："崩溃的程序无法事后回溯"。

> 所以这条坑现在是一条**"优化掉的正确性"**：功能（异常回溯）确实丢了，换来的是每段至少 512 字节的文件对齐开销。和坑 37 是同一个commit（`af2f695`）的两面——那边省的是没用的段，这边省的是 unwind 表。

### 坑 30：Win64 没有数据重定位，必须走 `.refptr` 槽

`int *gp=&g; return *gp;` 段错误。解码 `.text` 发现 `lea rax,[rip+X]` 目标 = **0x1000（.text 首）**而不是 `.data` —— 位移是合法的，指令能执行，所以**表现为崩溃而非链接错误**。

Win64 COFF 引用未定义数据必须走 `.rdata$.refptr.<name>` **八字节槽**（LLVM 就这么发）。goa 三处都不认识它：

1. 段名不在 `coffSectionMap` → 单独建段 → **镜像构建器只输出已知段，槽被丢弃**；
2. undefined 符号解析不到 → RIP 相对位移算成"从段首起"；
3. `attdirective.go:458` **早已理解 `.refptr`**（AT&T 路径），但 COFF 合并路径没接。

修法：`$.refptr.` 段名归一到 `.rdata`；段接收时记 `refptrFor[name] = loc`；undefined 符号遇 refptr 则把符号指向槽位置。

**这是 gocl 能出 exe 的最后一道阻碍。**

### 坑 31：其他静默坑

- **导入名必须带 `.dll` 后缀**：同一 DLL 出现两个描述符（`kernel32` 和 `kernel32.dll`），loader 精确匹配找不到 `kernel32` → `0xC0000139`（`STATUS_ENTRYPOINT_NOT_FOUND`），**进程第一条指令都跑不到且零诊断**。
- **`__main` 是 LLVM 的 CRT 初始化桩**：对象里**带全局构造函数的模块中每个函数**都引用它（不是无条件——`coffmerge.go:126-129` 的注释限定了这个条件），不映射就链接失败。合成一个内容为 `ret` 的 anchor 符号（`coffmerge.go:139-146` 追加 `0xC3` 并登记 `coffNoOpAnchor` = `__goc_coff_anchor`）。
- **`ADDR32NB` 的 addend 要从被修补的那个段读**，不是从目标段读（`coffmerge.go:670-678`："从目标段的同一偏移读出来的是那里的任意字节"）。
- **整个程序只能有一个 `Image`**：对象里的 `main` 在第一个 Image，桩的 `call main` fixup 在第二个，**永不相遇** → `undefined symbol referenced: main`（ELF 侧报错串在 `elf.go:133`，COFF 侧在 `coffmerge.go:708`）。
- **`.refptr` 修复时的变量遮蔽**：`if i := strings.LastIndex(...)` 里的 `i` 是**字节偏移**，同函数里 `baseOf[i+1]` 越界 → `index out of range [7] with length 5`（读起来像越界，实际是用了错的那个 `i`）。已修，`coffmerge.go:296` 用独立的 `k` 变量。
- **`___chkstk_ms` 栈探针**（三下划线）是 gocl 自己合成的（`coffmerge.go:161-191`）。前两版helper 都以 `STATUS_STACK_OVERFLOW (0xC00000FD)` 收场：读 `rcx`（调用者从不设置，按垃圾尺寸探测栈）或自己减页（双重分配）。现版用 **r11/r10** 作游标，探测块是 **18 字节**，且必须精确推进游标——`coffmerge.go:175-179` 的注释说得很直白："游标若不是恰好前进追加的字节数，从 COFF 对象合并进来的每个符号都会提前落位"，入口桩调`main`时会跳到函数前的对齐字节上立刻 fault。这和坑 28 是同一个"静默错位"的家族。

### 坑 32：COFF 对象的真实结构（实测数字，可作基线）

- **段共 6 个**：`.text`(0xd8) `.data`(0x20) `.bss`(4) `.xdata`(0x18) `.rdata`(0xf) `.pdata`(0x18)
- **重定位只有 2 种类型**（比预想可控得多）：`REL32` × 10（代码内 call / lea rip-relative）、`ADDR32NB` × 6（`.pdata` 的 RVA）
- **`@feat.00`**：`sec = -1` = absolute，安全 cookie 表
- **COMDAT**：`.pdata` 带 aux `comdat 0`，链接时要去重

> "只有 2 种"是**那个被测对象**的结论，不是链接器的上限——链接器实际接受 **3 种**（多一个 `ADDR64`，见坑 26）。这一节的权威出处是 `src/gocld/coff.go:13-24` 的文件头注释，它开头就写着 "measured on LLVM 23.1.2, **not guessed**"，逐条列出了 6 个段、2 类重定位、undefined 符号、每份 unwind 贡献一个 COMDAT 组、`@feat.00`。读这份注释比读实测数字可靠——它是把测量结论固化在代码里的。

> **bigobj：这条基线没覆盖到的最大变体**。`coff.go:200-225` + `:271-291`：`SizeOfOptionalHeader == 0x20` 表示 **bigobj**，符号记录宽度是 **20 而非 18**，布局为 `[4]flags [8]name [4]value [2]section [2]type [4]class+naux`，而且 `StorageClass` 与 `NumberOfAuxSymbols` **挤在同一个 4 字节字段里**（`cls = src[rec+18]; nAux = int(src[rec+19])`）。为什么必须从头部判断而不能猜，`coff.go:208-212` 说得很准："按 18 字节读一个 bigobj 表会产出看起来合理的垃圾，**而不是一个错误"——静默失败，正是本节标题说的那种坑。而 **LLVM 对 Windows 目标默认就发 bigobj**（`coff.go:201-203`），所以这不是罕见路径。坑 24 只讲了 aux 要占位，没提记录宽度本身会变，两条应该合看。

## 五、体积：必须落到段表 + 对齐上量

### 坑 33：无条件发射全部 380 个 goclib 函数 → exe 100k+

原生后端靠 `c.need` 不动点只发可达的十几个，IR 前端没有这层。修法是加 `llvmRoots(prog, lib, linux)` 做不动点可达性游走（定义在 `translate.go:297`；注意签名今天已经是**四个**参数 `llvmRoots(prog, lib, linux, tr)`，`translate.go:214`，多出来的 `tr` 是为了把 05 里那张 `UserDefines` 表传进同一个特化判定，见坑 36）。

实测（`-dump-ir` 数 `^define`）：

| 程序 | 发射前 | 发射后 |
| --- | --- | --- |
| `int main(){return 42;}` | 380 函数 | **2**（3072B vs 原生 1536B） |
| `printf` 用例 | 380 函数 | **43**（19456B vs 原生 7680B） |

### 坑 34：printf 调用点特化不在 IR 前端 —— hello world 2.5× 膨胀

`printf("hello, world\n")` 一个空 main：goa 后端 **6656 B**，LLVM 侧 **16896 B**。

量化（按 `.asm` 标签行数统计）：

| | .asm 行 | 标签数 | exe 字节 |
| --- | --- | --- | --- |
| goa 后端 | 1577 | 15 | 6656 |
| LLVM 侧 | 4362 | 67 | 16896 |

差异构成：共享 14 个标签，**LLVM 独有 53 个（占其 .asm 的 80.9%）**。多发的大头全是 printf 浮点格式化链，本程序一个都没用到：`vfmt` 791 行、`double_to_hex` 342、`double_to_exp` 298、`fmt_int_part` 265、`exp` 247……另有 `heap_alloc`/`heap_free` **在 LLVM 版里没有任何 callq 站点，纯虚引用**。

**根因链**：`printf` → `vfmt`（printf 的核心分派器，函数体含覆盖所有格式符的 switch，含 `%f`）→ 静态引用收集把整条浮点链拉进来。

**goa 后端没有 vfmt**，因为 `(*CG).genCall` 里有**调用点特化**（`constantFormatFwrite` / `constantFormatLite`）：printf 字面量格式串直接特化成直接写 stdout，完全绕过 vfmt。**LLVM 侧完全没有这一步**——特化挂在 goa 的 codegen 路径上，不在 IR 前端。

**反直觉的另一半**：共享函数里 LLVM 优化得**更好**（`__goclib_file_flush` 200→57 行、`memcpy` 61→19 行），函数体净膨胀才 +736 行——**优化红利完全被多发的 3530 行淹没**。所以"先补特化再谈 LLVM 优化水平"，否则测出来的都是噪声。

**这是架构缺口不是 bug**：特化逻辑放错层了。

### 坑 35：靠 LLVM 自己折叠格式串——两条都走不通

先试了这条路，实测**走不通**，两条原因都要记住：

1. IR 里 `@printf` 是 **`define` 而非 `declare`**（goclib 的 printf 被当普通函数发射），`SimplifyLibCalls` 只处理**已知 libc 符号**，不认；
2. 格式串在 IR 里是 `alloca` + 逐字节 store + `getelementptr`，**即使认得 printf 也看不到字面量**。

对照实验很能说明问题：绕开 printf 用 `puts` 时 LLVM 侧是 6144 < goa 后端 7680 ——**膨胀完全来自 printf 链，与 LLVM 后端无关**。

修法：特化判定抽到 **`src/common/printfspec.go`**（原文写 `src/printfspec.go` 有误——它在新建的 `common` module 内，`src/common/go.mod`，module 名 `goc/common`），用 `PrintfQueries` 参数化两个查询（`UserDefines` 程序是否自定义该名 / `ShadowedByVar` 有同名函数指针变量），两个后端共用同一函数：`src/gocl/call.go:108` 与 `src/goc/codegen.go:10039`。

**hello world 16896 → 5632 字节**（比 goa 后端 6656 还小 15%）。> 这个 5632是当时 `printf("%d")` 那一档的数，也是坑 37/38 那两项对齐修复**之前**的中间态。今天实测 `printf("hello, world\n")` gocl 是 **4096 B**（goa 6656 B）——又小了 27%，靠的正是后面那两条段对齐修复。

这里还有一个比代码更值得记的契约（`printfspec.go:22-33` 的注释）：两个谓词**只能问用户的声明，绝不能问运行时自己的副本**，否则每个库函数看起来都被遮蔽。也正因如此 shadow 判定最初查错了对象（查 `funcDefs` 而不是用户的 `userDefs`）。而"必须在编译期决定"也是硬约束（`printfspec.go:44-49`）："运行期试一下 lite、失败再退回 vfmt 的探测会让 vfmt 始终可达，什么都裁不掉"——这就是坑 36 那条铁律的机制，不是风格偏好。

### 坑 36：【铁律】可达性裁剪必须走同一个特化判定，否则互相抵消

`llvmRoots` 原本独立扫原始 AST，看到 `printf` 而非改写后的目标 → **裁剪把 printf 拉回来、特化被抵消**，改了等于没改。

两条纪律：

1. `llvmRoots` 也走 `specializePrintfCall`；
2. **Call 与 Ident 两类引用要合并进一次遍历**——两遍并存时第二遍会把刚摘掉的 printf 又加回来（第一版就犯了这个）。

附带：改写还引入原调用没有的引用（`fwrite` 拿到的 `__goclib_stdout()` 是个 **Call**），要一并标记可达，否则链接缺符号。

顺带修了个静默 bug：shadow 判定查的是 `tr.funcDefs`，但那张表**同时装用户函数和 goclib 的 wanted 函数** → `fwrite` 永远被判"被遮蔽"，特化从不触发。新增 `tr.userDefs` 只装用户函数。

### 坑 37：PE 段级文件对齐 —— 36 字节的段吃掉 512

`print("hello world")`（走 `str_print`，**printf 特化链完全没参与**）LLVM 侧 3072 字节，原版 2048。

**我先查错了方向**：认定是"IR 前端无条件发射全部 25 个 lib.globals"，改完发现**体积纹丝不动**（IR 里 25→0 个 `G_` 定义、`.data` 231→183，但总大小不变）。

> **教训**：改完必须重新看段级数据；不能因为"找到一个看起来合理的缺陷"就认定它是根因。

**真根因 = PE 段级文件对齐**。LLVM 多的 `.pdata`/`.xdata`（Win64 SEH 展开表）表本身很小（每函数 `.pdata` 12B + `.xdata` ~11B），但 **PE 每个段在文件里占 `FileAlignment` 的整数倍，512 是 Windows 接受的最小值** → 两个段 = 实打实 **1024B**，与内容多少无关。**三个函数花 1024 存 72 字节。规模越大亏越多。**

段表对比（print 场景）：`.text` goa 719 / LLVM 452、`.data` 231 / 183、`.pdata` — / 36(raw 512)、`.xdata` — / 36(raw 512)。**代码数据两边 LLVM 都更小。**

`coff.go` 原注释写「keeping them costs nothing」——按 36+36 字节算的，**忽略了文件对齐**。

修法：新增 `Section.Unmapped`。COFF 合并**照常并入**这两个段的字节（符号照常解析、`ADDR32NB` 重定位照常应用 → 不留悬空引用），只是不给镜像地址。Unmapped 是纯 PE 概念，**ELF 侧不碰**。

丢的是崩溃后 post-mortem 回溯栈。goc 只编 C、无 C++ 异常、无自己的 unwinder，且**原生后端早就接受了同样损失** → **体积决策非正确性决策**。

结果：`print("x")` 2048→**1536**；`printf("plain")` 6656→**4096**；`printf("%d",3)` 7680→**5120**；`printf("%f",1.5)` 14848→**7680**；`puts` 7680→**4608**。**不再有任何一档更大。**

> 测试要**直接读 PE 段表 + data directory[3]**，不比总大小——表将来变大也不会在重新引入浪费时仍通过。

### 坑 38：`FileAlignment` 设 0x1000 导致 93 字节的 .text 占 4096

hello.exe 16384 字节。`SectionAlignment` 必须保持 `0x1000`（页大小，改不了），但 `FileAlignment` 可以。改成 `0x200`（PE 规范最小值，也是 MSVC 默认）→ 1536 字节。

附带坑：合并段后 **符号基址表 `symBase` 和节的 VirtualAddress 是两回事** —— `.data` 节的 VA 是 `dataBase`，而 `.data` 里符号的 RVA 是 `dataBase + 偏移`，两者不能混用。

### 坑 39：`printf("%%\n")` 是个真漏洞（两边共有）

`%%` 是字面百分号，`ScanLiteFormat` 对它 `continue` 但**没有置 `has`** → `liteTargetFor` 返回 false → 整个程序退回全量 vfmt（**22016 字节 vs 应有的 ~4096**）。

**goc 也一样**（22016/37376），所以**不是回归而是两边共有的功能缺口**，也是唯一一处 LLVM 后端明显吃亏的常见写法。

修法方向（未做）：`%%` 应视作"无转换但需解转义"——要么单独走 fwrite（格式串需先解转义成单个 `%`），要么让它算作 `has=true` 并给 lite 加解转义。

`fprintf`/`sprintf`/`snprintf` **一律未特化**（没有对应 lite 入口），这是设计取舍不是 bug。

### 坑 40：体积实测对照（-O2，21 个用例）

**总和**：LLVM 侧 **229888** / goa 侧 366592（**小 37%**）。

| 档 | LLVM 侧 | goa 侧 | delta |
| --- | --- | --- | --- |
| 空程序 | 1536 | 1536 | 0 |
| 无 %（putchar / puts / printf 字面量） | 3584~4608 | 5120~6144 | +1536 |
| 整数 lite（`%s` / `%d` / `%x%o%c` / `%u%i%X`） | 5632 | 6144 | +512 |
| 浮点 lite（`%f` / `%f`×2 / 混合） | 7680~8192 | 13312 | +5120~5636 |
| math.h（`%f`+sqrt） | 8192 | 13824 | +5632 |
| **未特化**（`%e` / `%g` / `%%` / `%ld` / fprintf…） | 18944~22016 | 31744~37376 | +12800~15872 |

IR 侧函数数印证特化生效：`printf` 字面量 **14 个函数**且没有 vfmt；`%d` **22 个**，走 `printf_lite` + `vfmt_i`；`%%` **43 个**，整套 vfmt + double_to_buf 全被拉进来。

## 六、AT&T 前端：六个翻译难点

goa 原本只吃 Intel/NASM 语法，而 LLVM 的 AsmPrinter 输出 AT&T/GAS，两者不兼容——早期只能让 libLLVM 直接吐 COFF。加了 AT&T 前端后（逐行译成 goa 内部语法再喂既有 `encode()`，**x86 编码逻辑一行未改**），链条才真正打通。

六个难点**全部由 72 个真实样本暴露，不是推演**：

### 坑 41：操作数方向不能一律翻转

- 尾随内存操作数即目标 → 需翻；
- 但 `add`/`cmp` 配**寄存器**源时顺序**已经是对的** → 翻了会报错；
- **纯存储 `mov`** 与**立即数**必须翻（立即数物理上在末尾字节）；
- **三操作数 `imul` 是轮转，不是交换。**

这是 8086 src/dst 歧义留下的历史包袱，**不是可以统一处理的规则**。

### 坑 42：内存操作数自带逗号

`8(%rax,%rbx,4), %eax` —— naive `split(',')` 会把寻址撕碎。必须**按括号深度切分**。

### 坑 43：内存宽度只能从助记符后缀取

`decl -4(%rbp)` 是 32 位，但 AT&T 没有 `dword` 前缀。得把后缀宽度转写进 `memWidth`。

### 坑 44：`movq`/`movd` 歧义

`movq %rsi,%rcx` = GPR；`movq %xmm0,%rax` = SSE。靠**操作数是否含 xmm** 区分。

### 坑 45：GAS 无操作数惯用语按**源**宽度命名

`cqto`（= 64 位，goa 的 `cqo`）若译成 `cdq` → **rdx 高半段未定义**。还有 `cwtq`/`cltq` → `cwde`/`cdqe`。

### 坑 46：dot 标签有**两种作用域**

`.LBB*` / `.Ltmp*` 是函数内的（要加函数名前缀）；`.str.0` / `.LCPI0_3` 是**文件级且被十几个函数共用** —— 按 NASM 惯例一律加前缀会让**第二次引用就悬空**。

> **同源历史 bug（goa 侧）**：`.` 开头的标签曾是**全局**的（`defineSym` 后者覆盖），`num.asm` 里 `fact` 和 `fib` 各有一个 `.Lrec` → `fact` 的 `jg .Lrec` 跳进 `fib` 的 `.Lrec`。这曾是"calc.asm 那个无法解释的 bug"的真正根因（当时绕过去了没找到）。修法：`qualify()` 拼上最近的全局标签名，恢复 NASM/GAS 语义。

### 坑 47：跳转表重定位需要"一对符号"

72 个真实 LLVM 样本只有 **4 个**能链成 PE，其余 68 个全卡在 `.long .LBB29_14-.LJTI29_0`（switch 跳转表项，含义是"case 目标相对表基的偏移"）。

两个标签通常**分居不同段**（case 在 `.text`、表在 `.rdata`），汇编期无法化简；而 goa 的 `Fixup` **一处只记一个符号**。

修法：`Fixup` 加 `sym2` 携带被减数；`applyFixup` 在 `sym2` 非空时写 `(sym - sym2)`；PE/ELF 两侧对称解析。assemble 侧用 `attSplitSymDiff` 识别 `A-B`——**减号是指令名里唯一可能出现的字符，符号名不含它，切分无歧义**。

结果：**72 样本解析 72/72、链接 72/72**（此前 4/72）。

> 72 这个数字要看清楚：**样本是生成物，不在仓库里**。`src/goa/testdata/att/` 今天只有 4 个 `.s`（`intprint.s`、`longmin.s`、`print_thin.s`、`variadic.s`），`att_e2e_test.go:31` 会全量 glob 并在空集时 `t.Skip("no AT&T samples -- run: bash tools/gen-att-samples.sh")`。72 样本由 `tools/gen-att-samples.sh` 从 `examples/*.c` 生成，一次性测量后没有提交。复现：`bash tools/gen-att-samples.sh`。
>
> 另：`attSplitSymDiff`（`attdirective.go:588`）实际用 `LastIndex` 从右扫，而不是简单的首次出现——为了不让前导 `-`（负常数）被误当分隔符，再用 `isSymName(lhs) && isSymName(rhs)` 双侧校验。比"减号不出现"这个理由更稳。

### 坑 48：跳转表的验证方法（关键）

合成 6 路 switch，链成 PE 后用 `tools/peun.py` 在 Unicorn 里**真跑**，每个 case 累加不同权重、退出码即总和。

**这一条必需**——表项算错时字节仍合法、镜像仍能加载，**只有真正执行才暴露**（跳进无关代码或 trap）。测试在 `src/goa/att_jumptable_test.go:27` 仍在跑（`runPEUnderUnicorn`，`att_jumptable_test.go:100`），退出码比对在 `:102`。

> 更正一处原文的归因：这里曾写"`peun.py` 对 LLVM 产物**不可用**（栈模拟不建映射）"。源码不支持这个说法——`tools/peun.py:137-138` 明确**建了栈映射**（`mem_map(STACK_TOP - STACK_SIZE, ...)`），全文也没有任何区分"LLVM 产物/ goa 产物"的分支。前半段（6路 switch + 真跑 + 退出码即总和 + "只有真执行才暴露"）完全成立且有测试在跑；后半句那个归因请忽略。
>
> 另一条读源码才发现的细节：`ExitProcess` 在 Unicorn 里取的是 **RCX** 不是 RAX（`att_jumptable_test.go:79`）。比对退出码时用错寄存器会得出"表算错了"的假结论。

### 坑 49：AT&T 路径的四个静默 bug

1. **`.section` 名解析把反斜杠混进字符集**：`IndexAny(nm, "\",")` 截引号，字符集里有**反斜杠** → Windows CRLF 的 `\r` 先命中 → 段名变成 `"dr"` → 退化成 `.data` 里的匿名段；
2. **MSVC 栈探测缺失**：LLVM 把 >1 页栈帧降级成 `call ___chkstk_ms`（**三个下划线**，x64 MSVC ABI 名），必然链接失败；
3. **分号注释未剥离** → 行尾注释被当成后续操作符（GAS 用 `#`，但手写汇编常用 `;`）；
4. **goa 风格的无点 `section`/`global` 掉进指令流**：`section .rdata,"dr"` 因带引号被 `HasPrefix(".")` 拦掉。

### 坑 50：两份实现只会互相遮蔽

为 `___chkstk_ms` 新建了 `src/goa/winstack.go`，后来发现 **`coffmerge.go` 早已正确实现**，且**带 16 字节对齐填充**（缺了对齐会让 COFF 合并进来的符号落位偏移，入口桩调 main 会跳到函数前的对齐字节上立刻 fault）。删掉重复实现后实测 8192 字节栈帧的函数 rc=0。

**教训：加"发射某符号"的代码前，先 grep 同名符号的其它发射点。**

（原文写的行号 `coffmerge.go:122` 是写文时的旧值，后续 commit 让文件增长了；当前实现是 **`coffmerge.go:161-191`**，删除 `winstack.go` 的 commit 是 `9621d75`。`winstack.go` 今天确已不存在。顺带当前实现用 **r11/r10** 作游标而非 rcx、探测块 **18 字节**——细节见坑 31 最后一条。）

> 顺带一个测试方法上的要点：`att_test.go` 用**字节级等价对照**——同程序分别用两种语法写，要求 `.text` 完全相同，且 **Intel 一侧手写而非从前端导出**，否则测试是同义反复。

## 七、线程局部存储：表象是"递归坏了"，根因完全不同

这是最近才修的一个，值得单开一节——因为**它是我判断错了一次的方向**。

> **时态说明**：坑 51 和坑 52 描述的是**修复前**的现象，两者都已由提交 `28a8d72`（2026-10-07 02:08，「gocl: 支持线程局部存储（`_Thread_local`/`__thread`）」）修好，`bench/nim/BENCHMARK.md:5` 现在记的是 gocl **5/5** 内核与 gcc 逐位一致，并写明"此前 fib 失败，根因是 gocl 的 TLS 支持缺失，已修；**与递归无关**"。下面保留原始排查过程，因为它本身是有价值的记录。

### 坑 51：Nim 生成的 fib 递归在 LLVM 后端静默无输出（**修复前的现象**）

gocl 跑 Nim 生成的 C 递归（fib）**静默无输出**，goc 正确输出 `fib 317811`。

第一反应是"递归代码生成有问题"。于是逐步隔离：

| 用例 | goc | gocl（修前） |
| --- | --- | --- |
| 32 位 `fib(30)` | 832040 | 832040 ✓ |
| 64 位 `long long fib(30)` | 832040 | 832040 ✓ |
| 尾递归 `sum(100)` | 5050 | 5050 ✓ |
| goto 早返 + 调用间错误检查的递归 | 317811 | 317811 ✓ |
| **递归中读一个 TLS 变量** | 832040 | **0** ✗ |
| **非递归读一个 TLS 变量** | **5** | **1073754112** ✗ |

纯 C 递归**全部正确** → 不是递归的问题。改写成 64 位、goto 早返、去掉调用约定修饰（`N_NIMCALL` 在 x64 上是无修饰）都仍然正确 → 也不是这些。

**最小复现**：`_Thread_local int g_x = 5; int readx(void){ return g_x; }` → gocl 返回 **1073754112**（垃圾）。

**Nim 侧机制**：Nim 每个递归调用后执行 `if (NIM_UNLIKELY(*nimErr_))` 读一个 **TLS 错误标志 `nimInErrorMode`**；gocl 把这次读生成成垃圾 → 误判为真 → **提前返回 0 / 无输出**。

**所以根因是 gocl 缺 TLS 支持，与递归无关。**

### 坑 52：为什么 gocl 的 TLS 是坏的（**修复前的两层原因**）

两层原因叠加：

1. **IR 侧**：把 TLS 全局当普通 `extern` 全局（`noteExternGlobal`），LLVM 于是生成 `load @G_x`——**直接读静态模板地址，完全绕过 Windows `gs:0x58` / Linux `fs` 的 per-thread 机制**。现在 `translate.go:174-177` 在 `noteExternGlobal` **之前**就`continue` 掉 TLS 全局（注释："绝不作为直接全局，所以这里不声明 IR 符号"），运行时全局同样处理（`translate.go:504-507`）。
2. **链接侧**：`linkData` 里 TLS 全局被 `continue` **跳过了**，所以 `Data.TLSVars` 是空的 → 链接器根本没铺 `.tls` 段、没建 TLS 目录、没定义 `G_goc_tls_index`。现在 `gocl/cmd/gocl/main.go:239` 已填上：`TLSVars: gocl.ComputeTLSLayout(prog, common.Store(cfg.Linux), cfg.Linux)`。

### 坑 53：修法——复用原生后端已有的设施，不新造一套

关键认知是：**原生后端早就把 TLS 做完整了**（codegen 手写访问序列 + emit.go 铺 `.tls` 段 + pe.go 建 PE TLS 目录）。gocl 只需要**按同一套布局产出访问代码**，把段和目录交回链接器。

具体四处：

1. `translate.go:531` 新增 `ComputeTLSLayout`，按原生 `tlsPlace` 规则（**8 字节对齐**（`translate.go:544` `off = (off+7) &^ 7`）+ `link.TLSAlignedSize` 槽宽）给每个 TLS 全局分配偏移，并用 `seen` map 保证同名（用户程序与 C 运行时可能都有）只分配一次（`:534-537`、`:543-545`）；
2. `linkData` 用**同一个函数**填 `Data.TLSVars`（`gocl/cmd/gocl/main.go:239`）；
3. 访问 TLS 全局改经 helper `__goc_tls_slot(off)` 取 per-thread 地址，不再 `load @G_x`（`expression.go:475` 发调用，`module.go:188` 声明 `declare ptr @__goc_tls_slot(i64)`，lvalue 侧 `call.go:22`）；
4. `common/link/emit.go:182-207` 在有 TLS 变量时把 `__goc_tls_slot` 汇编进入口桩。

**第4 步的两条真实序列**（原文写错了寄存器，以 `emit.go:189-205` 为准）：

```
; Windows x64 —— 索引用 edx，TEB 指针取到 rcx，末步是 add 不是 lea
mov rax, rcx                    ; 取传入的偏移（Win64 第二参在 rcx）
mov edx, [rip+G_goc_tls_index]  ; 模块 TLS 索引
xor ecx, ecx
mov rcx, gs:[rcx+0x58]          ; TEB.ThreadLocalStoragePointer
mov rcx, [rcx+rdx*8]            ; 按索引取槽
add rax, rcx
ret

; Linux x86-64 —— 不是裸 lea
mov rax, rdi                    ; 取传入的偏移
lea rdx, [rip+__tls_start]
add rax, rdx
ret
```

三个必须注意的点：

- **`noteExternGlobal` 是陷阱**：把 TLS 声明成 extern 全局就直接退化成"读模板地址"，必须跳过；
- **IR 访问偏移与链接器 `.tls` 布局必须用同一个函数算**，否则逐条错位。实际有**三个**消费点共用它，不止原文说的两个：IR 访问（`translate.go` 记 `tlsOffsets`）、链接布局（`main.go:239`）、emit 符号解析（`module.go:73-77` 定义 `tlsOffsets`，`module.go:737` 注释"这个偏移正是 `ComputeTLSLayout` 分配的"）；
- **`__goc_tls_slot` 是双后端共用定义，不是 gocl 专属**。它住在共享的 `common/link/emit.go:179-181`，注释说明："原生后端不调用它，但多一个未使用的定义不花什么代价，而且让两个桩保持完全一致。"

结果：8 文件 +181/−27（与提交 `28a8d72` 的 `git show --stat` 精确一致，8 个文件也完全对得上）。Nim 五个内核 gocl 从 4/5 变 **5/5**，且提交信息自证"asm.go 的改动仅为 gofmt 对齐（`git diff -w` 为空）"、"gocregress 476 ok / 30 fail，失败集合与修复前逐条相同"（那 30 个 fail 是本机Windows 跑不了的 Linux 腿既有问题，不是 TLS 引入的）。

> 一个遗留：`readtls` / `fibtls` / 多类型混合 / 取地址写回这四类回归用例，`grep -rl` 全仓库只命中 `BENCHMARK.md` 一处，源码树里既没有对应的 `.c` 用例也没有 Go 测试（`gocl/link_e2e_test.go` 只有 3 个测试，无 TLS 相关）。仓库里唯一的 TLS C 用例是 `src/examples/tls_basic.c`。这批用例要么当时是临时文件，要么已删除——**这是一个真实的覆盖缺口**，值得补进 `tests/`。

## 八、Nim 生成的 C

### 坑 54：`NIM_STRLIT_FLAG` 被静态初始化器折成 0

这个坑的修复比原文列的更宽：`foldCastInt`（`common/link/global.go:329-355`）除了 `_Bool` / `_BitInt` 截断，还处理**指针等非整数目标**——`default: return v, true`，注释是"为指针和其他非整数目标保留位模式，这样 `(void*)0` 空指针还是 0"。`t == nil` 时返回 `(0, false)`（`:330-332`），这是整个 folder 唯一会"放弃"的情形。`truncInt`（`:360-369`）对 `width <= 0 || width >= 8` 直接透传。这一层也印证了"与后端无关（两后端共享 `common/link`）"——`global.go:240-243` 的注释直接点名 `NIM_STRLIT_FLAG`："`((NU)(1) << 62)` … 折不出来，它的静态初始化器被发射成 0，这破坏了 Nim 的每一次 float→string 转换"。

Nim 的字符串字面量标志是 `NIM_STRLIT_FLAG = ((NU)(1) << 62)`，写成
```c
static const struct{ NI cap; ... } TM = { 0 | NIM_STRLIT_FLAG, "" };
```
`setLengthStrV2` 据此判断是否需要新分配。

`echo 3.5` 在**两个后端下都段错误**。最小复现证明 `((NU)(1) << 62)` 在 goc 下 cap 被算成 0、gcc 下正确。

根因：`foldConstInit` 不处理 `CastExpr` → 这个常量表达式折不出来 → 静态初始化器被写成 0 → `setLengthStrV2` 误判需要重新分配。

修法：加 `CastExpr`（`global.go:234`）/ `CondExpr`（`:230-233`）/ 一元 `!` / `_Bool`·`_BitInt` 截断，新增 `foldCastInt`（`:329`）/ `truncInt`（`:360`）。

> 这条与后端无关（两后端共享 `common/link`），但它揭示了一类问题：**前端常量的折叠能力决定了 C 库能不能用**，而不是"编译器支持不支持这个语言特性"。

### 坑 55：goclib 缺 Nim 运行时函数

完整 `bench.nim`（含 `std/strutils`）拉进 `resize__system_u3063`、`eqdestroy___system_u3728` 等 Nim 运行时函数，goclib 尚未实现 → 暂不可编。

**这是运行时覆盖率的事，与后端无关**，所以基准改用 5 个内核（sieve / fib / intloop / strbuild / float）。

> 更正"自包含"这个说法：那 5 个 `.nim` 文件**今天仍然写着 `import std/strutils`**（`bench/nim/k_sieve.nim:15`、`k_fib.nim:15`、`k_intloop.nim:15`、`k_strbuild.nim:15`、`k_float.nim:15` 全都还在）。它们能编过纯粹是因为源码里没实际调用 strutils 的过程，编译器因而没拉进 `resize`/`eqdestroy` 那批运行时符号——是**依赖分析兜住了**，不是 import 被删了。严格更干净的做法是把这几行多余的 import 删掉，防止将来误用。`bench/nim/BENCHMARK.md:26` 那句"完整 `bench.nim` 目前不能直接编"仍然准确。

## 九、架构与工程坑

### 坑 56：混合双生成器是本轮所有 bug 的根源

最初的设计是"用户函数走 LLVM、goclib 走原生"。两个生成器共享符号表，在三处分歧，每一处都表现为**链接错误或静默的错误结果**：

- 全局变量名（`G_` 前缀不一致）；
- undefined 符号是"导入"还是"对方已定义"；
- 变参调用的格式串重写。

**修法**：IR 侧**拥有全部 C 代码**（用户函数 + goclib 全部函数 + 全局变量含初始值）；goa 只负责**启动桩**（非 C，按平台 ABI 设置进程栈）+ 链接 COFF 对象。分工变成"两种不同种类的代码"而非"两个编译器抢同一批符号"。

配套的一条原则：**IR 翻译器必须与 codegen 零耦合**。IR 生成器是 CG 的**另一个输出后端**，直接用它的 `exprType`/`lookupVar`/`scopes`；**自己写第二套类型推导 = 对同一程序给两个答案，两者终将分歧然后静默编译错**。

### 坑 57：`link.Data` 的布尔开关不够用

全量 LLVM 下所有函数与全局都在对象里，但仍有"哪些要桩补、哪些对象已定义"的区分——同一后端里两种情况并存。布尔开关表达不了。

新增两个**按符号判定的谓词**：`SymbolInObject`（谁定义）与 `NeedsSlotBinding`（谁要桩补）。

踩过：给 `d.StrLabs` 传 nil map → `panic: assignment to entry in nil map`。

### 坑 58：`-fllvm` 在 goc 里已是死标志

`src/goc/main.go` 里 `cfg.llvm = true` 设置后**全文不再读取** → `goc -fllvm` 与纯 `goc` 行为一致（都走 goa 后端，产物同为 7680B）。**LLVM 后端只能经 `gocl` 进入**。

后果：验证裁剪后的 libLLVM.dll 时，第一次误用 `goc -fllvm` 测的其实是 goa，**结论无效**，得用 gocl 重测。

### 坑 59：`go build -o x.exe .` 对 `package compiler` 产出的是 archive

`src/goc` 改成 `package compiler` 后，`go build -o bin/goc .` 产出 **archive（`!<arch>`）**。Linux 上执行它 `Permission denied`，**81 个例子全部报 `(compile): ./bin/goc: Permission denied`**——看起来像编译器全崩了。

判据：凡是把 `-o` 指向 exe 的 `go build`，都要确认**包参数指向 main package**（`./cmd/xxx` 或带 `func main` 的目录），而不是 `.`。

### 坑 60：库查找必须「上下交替」，且 `filepath.Join(dir, "..")` 不会爬升

库在 `src/goclib/`、exe 在 `bin/`，两者是**兄弟目录不是父子**。只上溯从 `bin/` 到 `D:/` 永远不到 `src/`；只下潜从 `bin/` 只碰到 `goc-out*` 构建目录。

正确写法：**每层先 descend 再 climb，两个方向交替**——"先试完一个方向再试另一个"的搜索表达不了"上溯到根再下潜进 src/"。

Go 坑：**`filepath.Join(dir, "..")` 会 Clean 掉 `..` 直接返回父目录** → 上溯必须用 `filepath.Dir()` 逐级循环，**用 Join 写的循环根本不会爬升**。

### 坑 61：CI 连红三次 —— 本地全绿 ≠ CI 绿

当时是 `src/goa/llvm.go`（Windows-only 的 `syscall.LazyDLL` 绑定）整个文件用 `syscall.LazyDLL` 却**没有 `//go:build windows`**，Linux 上 `go vet` 报 10 处 undefined。连续三次 push 全红，同一个原因。不是新引入的，是**目录重组让依赖图变化后暴露**。

修法不是给整个绑定加标签（那会让所有提到 `goa.OpenLLVM` 的调用方都编不过），而是拆三文件：

- `llvmapi.go`（无约束）：`ErrNoLLVM` + 档位常量——**错误与档位是调用方要能说出名字的东西，必须跨平台可用**；
- `llvm.go`（`//go:build windows`）：整个绑定；
- `llvm_stub.go`（`//go:build !windows`）：同名导出的桩，返回 `errUnsupportedPlatform`。

**不能用 `ErrNoLLVM`** —— 那说的是"库没装"，而这平台上装了也没用。`llvm_stub.go:21-23` 的注释把这层区别写明了："它不是 `ErrNoLLVM`：后者说的是库缺失，而在这个平台上装什么都没用。"

对应提交：加约束是 `94a76f2`「给 LLVM 绑定加构建约束：CI 的 Linux 腿连着红三次」；随后 `407154c`「把 LLVM 绑定搬进 gocl，并抽出 gocld 链接器」把三个文件整体从 `src/goa/` 搬到 `src/gocl/`（因为 LLVM 后端已成为独立编译器）。所以**原文引用的文件路径今天已不存在**——`src/goa/` 下现在没有任何 `llvm*.go`。

另一个教训：**加约束要连测试一起加**，否则 Linux vet 会在 `undefined: llvmAPI` 上再红一次。

铁律：改完必须双平台验（逐 module `go vet ./... && GOOS=linux go vet ./...`）；**推送后要 `gh run list` 看结果**，不能推完就当完事。

> **同一类教训的新成员（本次核实发现）**：当年"双平台验"的教训如今又有新成员没被 CI 覆盖。仓库现有 **8 个 `go.mod`**（`src/common`、`src/frontend`、`src/goa`、`src/goc`、`src/gocl`、`src/gocld`、`src`、`tools`），而 `.github/workflows/ci.yml:29-34` 只 vet 了 6 个——**缺 `src/common` 和 `src/gocl`**。也就是说本篇 05 里修的那批 `src/common/printfspec.go` 改动，目前不在 CI 的 vet 范围内。
>
> 同类还有两个 CI 结构性细节值得记：`ci.yml:22-25` 的 gofmt 检查刻意不用 `.`（因为 `.gitignore` 排除了本地 `scratch/`，`gofmt -l .` 会把不入库的目录也判红，而这种红"无法从 commit 里修"）；`ci.yml:42-46` 的 `msgboxcheck` 改为**交叉编译而非跳过**，因为裸 `go build` 会因 build constraints 排除全部 Go 文件而报 `build constraints exclude all Go files`，曾"静默把整个 Linux job 拉下来"——与本坑"Linux vet 再红一次"同型。

### 坑 62：rebase 的四条铁律

1. **只解冲突文件，其余原样保留**。曾试图用 `git show <mycommit>:<file>`"恢复"自动合并的 4 个文件，那会覆盖掉远端的自动合并（远端给 `CompileToObject` 加了第 5 参 `linux bool`，用旧版会 `have/want` 签名不匹配、build failed，**且伪装成前端 `__builtin_va_list` 解析错，浪费一轮排查**）；
2. **判断"失败是否我引入"要双向对照**：先把工作区置为纯 origin 跑全套确认全绿，再恢复我的改动跑，才定位到归属；
3. 合并冲突区若两侧结构差异大，**整函数重建比逐块拼更可靠**；重建后必删被自动合并保留的重复尾段；
4. **`gofmt -l` 对冲突文件必跑**——rebase 后必查，**冲突解决不会自动格式化**。

### 坑 63：`-S` 被静默 no-op，且第一轮修复也是错的

`gocl -S file.c` 不输出汇编（实际仍出 exe）。

第一轮错误修复：把 `-S` 接到 `cfg.DumpAsm`（写 goa 入口桩）→ **桩里没有用户的代码**。`-S` 应该走 LLVM 的 AsmPrinter 生成 C 函数体的 AT&T `.s`（这是 `gcc -S` 的对等物）。

正确修复：复用早已存在但**一直没接线**的 `emitIRAssembly`。输出命名：给了 `-o` 落到该路径，没给落到 `<源>.s`。内容边界：只含 C 函数体（不含入口桩），与 gcc -S 不含 CRT 启动一致。

### 坑 64：其他环境坑（都会反复踩）

- **Git Bash 直接跑 PE 会误报崩溃**：`./x.exe` 报 `Segmentation fault`，但 `cmd //c "x.exe"` 退出码正确。**验证 PE 必须走 cmd。**
- **同秒内批量"编译 + 运行"多个程序会假失败**：libLLVM.dll **并发加载竞争**。每之间 `sleep 0.2~0.3s`。
- **只看顶层 `--- FAIL` 行会误判成"全红"**：e2e 实际 9/10 通过。
- **`bin/goc` 遮蔽 `bin/goc.exe`**：`bin/` 里一个陈旧的无扩展名文件，PATH 优先选它 → 所有"改了源码没生效"的假象都源于此。**goc 项目一律用绝对路径 `bin/goc.exe`。**> **这条今天依然成立，尚未清理**：`bin/goc`（4020224 B，10-06 07:48）与 `bin/goc.exe`（4210688 B，10-07 02:18）并存，`file` 确认两者都是 PE32+ 可执行文件，无扩展名的那个更旧。
- **`go build` 缓存不刷新 `//go:embed`**：新增头文件后产物 embed 仍是旧头。症状是 `skipping unavailable system header`。> **这条今天只属于 standalone 构建**：`src/goc/headers.go` 现在**不含任何 `go:embed`**（全文是注释，描述"运行时从磁盘读"），`src/common/source.go:15` 还留着迁移痕迹："goc 过去把整个 C 库塞在可执行文件里"。普通 `goc`/`gocl` 现在运行时读磁盘（`FindRoot()`，`source.go:166`），**新增头文件不需要 `touch` 了**。今天唯一的 `//go:embed goclib/*` 在 `src/main.go:46`，属于 `goc-standalone`（`src/main.go:43-46` 注释说明 `src/goc/cmd/` 下需要 embed 才能拿到 `goclib/`）。所以遇到 `skipping unavailable system header` 时，先查 `GOCLIB_PATH` / 库查找路径，而不是 `touch src/goc/headers.go`。
- **批量 sed 改路径后必须 grep 目标串确认为 0**：某次 8 处漏改，全表现为"某腿 FAIL"而非编译错误；其中 `./cmd/goa` 被误改成 `./goa`，而 goa module 的主包目录就叫 `cmd/goa`，**与 cmd/goc 不是一回事**。
- **`go mod tidy` 联网超时；本地 replace 的 module 也要 go.sum 条目**（**间歇性**，清缓存时才报）。**加 module 后逐个检查所有 go.mod，别只加被直接 import 的那个。**
- **`commit -m` 的反引号会被 shell 吞掉**（bash 双引号串里是命令替换，报 `command not found`，**提交成功但消息留空洞**）。**长提交信息一律用 `git commit -F <file>`。**
- **这个仓库的 shell 与 Python heredoc 会把 `\n` 写成字面 `/n/`**：症状是 LLVM 报 parse error，**我因此误判过一次"段错误"**。写多行 Go/asm 字符串**必须用文件写入工具**。

## 还没做的

按"能不能靠现有手段做"分三类：

**能力缺口**
- ~~`gocl -target linux` 报"LLVM 后端尚未实现 ELF 对象"~~（**已解决**：commit `7f22b64`「feat(gocl): Linux ELF 后端 —— SysV va_list ABI + ELF 目标输出」落地，已在 alpine 真机跑通并与 Windows 输出一致）；
- 位域 / `_BitInt` / 内联汇编仍走原生路径（`llvmEligible` 排除）；
- goclib 缺 Nim 运行时函数，完整 `bench.nim` 暂不可编。

**工具链限制**
- ~~`LLVMRunPasses` 调不动 → 无中端优化，只能用管线字符串~~（**已解决**：改用 `const char*` 传管线，commit `1ba351a` 已接入 `runIRPasses`，GVN/LICM/向量化随 `default<O?>` 管线可用——与上面的 ELF 是两件独立的事）；
- MinGW 静态链接 libLLVM 的 C++ 全局构造顺序不可控；
- Win64 上 `va_list` 按值传给 `printf_lite_with`（应为 `va_list*`），多层转发可能暴露。

**已知功能缺口（两边共有，不是回归）**
- `printf("%%")` 不触发 lite；
- `fprintf`/`sprintf`/`snprintf` 一律未特化。

## 三条元教训

写完回头看，这 100 多条坑里最值得记住的是三条方法论：

**一、"IR 是对的但运行时结果错"这一类最值钱，定位手段已经成型。**

现象离根因极远（控制台全哑 → 重定位丢了 4 字节 addend）。有效的三板斧：
- **运行时探针把内部结构体字段用 exit code 位图传出**（native=51 vs LLVM=1，一眼定位）；
- **`objdump -d -r` 看 addend + `objdump -d` 看落点 + 落点对照表**（偏移 0 全对、带偏移全错 → 直接指向重定位）；
- **凡"汇编对但运行时错"，第一怀疑对象是指令编码的 size 修饰符**——窄类型存储用 `C7`（4 字节）而不是 `C6`（1 字节）会淹没邻居槽，这类 bug 汇编层面完全看不出来。

**二、COFF/PE 的坑集中在"非对称字段"和"隐含约定"。**

8 字节符号名的首 4 字节语义与直觉相反、`IMAGE_RELOCATION` 的符号索引在中间、Type 只有 2 字节、aux 记录要占符号槽、**COFF 重定位条目根本没有 addend 字段**（`A` 藏在待修补字节里）、`ADDR32NB` 的 P 是段首、Win64 没有数据重定位要走 `.refptr` 槽、`__main` 是伪符号、`@feat.00` 的 `sec = -1`。

读错这些**不会崩溃**，只是结果不对——所以必须靠"落点对照表"这类外部验证，不能指望实现自己报警。

**三、体积问题要落到"段表 + 对齐"上量，不要只看内容大小。**

PE 每段占 `FileAlignment` 整数倍（512 最小）→ `.pdata`+`.xdata` 内容才 72 字节却要占 1024；导入表膨胀 100% 在 `.data`；可达性裁剪与特化必须走**同一个判定**，否则互相抵消。

还有一条元教训是关于**排查方法**的：我曾把"IR 前端无条件发射全部 25 个 lib.globals"当成 print 体积反常的根因，改完发现体积纹丝不动。**不能因为"找到一个看起来合理的缺陷"就认定它是根因**——改完必须重新看段级数据。

## 结果

- **Nim 跨编译器验证**：5 个内核（sieve / fib / intloop / strbuild / float）在 goc 与 gocl 下**全部与 gcc 逐位一致**；
- **计时**：三家 wall-clock 都在 0.16–0.20s，由 Nim 运行时启动主导，差异在 5–10% 噪声内——基准的真正价值是**正确性校验**而非计时；
- **体积**：-O2 下 LLVM 侧总和比原生小 37%，且**不再有任何一档更大**；
- **TLS**：从"读出垃圾值"到四类用例全对；
- 回归：476 ok / 30 fail，**失败集合与修复前逐条相同**（全是本机 Windows 跑不了的 Linux 腿），零回归。