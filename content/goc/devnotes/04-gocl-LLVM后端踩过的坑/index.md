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

这一节全部结论都在 `src/gocl/llvm.go`（496 行，绑定 + 全流程）和 `src/gocl/compile.go`（161 行，驱动）两个文件里，下面每条都给出 `文件:行号`。验证环境是 `bin/libLLVM.dll`，实测 `LLVMGetVersion()` 报 **23.1.2**（1038 个 `LLVM*` 导出）；`src/gocl/llvm.go:38-43` 的注释解释了为什么必须用 `LazyProc.Call` 而不是 `syscall.SyscallN`——后者要传裸 `uintptr`，读回来得转 `unsafe.Pointer`，过不了 CI 的 `go vet` unsafeptr 门禁。

### 坑 1：`LLVMCreateTargetMachine` 返回 NULL 直接崩进程

**现象**。少调一个初始化函数：`LLVMInitializeX86Target` / `NativeAsmPrinter` / `NativeAsmParser` 都调了，AsmParser 也注册了，但 `LLVMTargetHasAsmBackend()` 返回 0，随后 `LLVMCreateTargetMachine` 空指针崩溃——无异常、无诊断、进程直接死。

**为什么难查**。这是最像"LLVM 装坏了"的一类 bug。库明明加载成功了（`dll.Load()` 返回 nil，25个符号全部 `Find()` 成功），`LLVMGetVersion()` 也能正常返回 23.1.2——**所有能证明"库是好的"的检查都通过了**，唯一崩的是最后一步。更糟的是崩溃点在 `LLVMCreateTargetMachine` 里面，而那个函数跟"少调了一个初始化函数"在语义上毫无关系：它接收一个 `LLVMTargetRef`，创建一个 codegen 配置，跟"后端有没有初始化"看起来是八竿子打不着。任何人第一反应都会去查 LLVM 版本、DLL 完整性、导出面——也就是已经全部证明为正常的那批东西。

**根因（附源码位置）**。x86 后端要单独初始化 **TargetMC**（machine-code 层）。`llvm.go:190-199` 的 `initTargets()` 按 `TargetInfo → Target → TargetMC → AsmPrinter → AsmParser` 五步循环调用（列表在 `llvm.go:191-195`），`llvm.go:186-189` 的注释点名了 `LLVMInitializeX86TargetMC` 是最容易漏的一个，理由就写在注释里：没有它，target 没有 machine description。实测印证了这条注释——我拿 `bin/libLLVM.dll` 写了个探针，逐步初始化并调`LLVMTargetHasAsmBackend`：

| 调了几步 | `LLVMTargetHasAsmBackend` |
| --- | --- |
| 2 步（跳过 TargetMC） | **0** |
| 3 步（跳过 AsmPrinter） | 1 |
| 4 步（跳过 AsmParser） | 1 |
| 5 步（完整） | 1 |

**只有 TargetMC 缺失会翻转这个标志**，AsmPrinter / AsmParser 缺不缺都不影响它。所以这个标志是个相当精确的探针——可惜没人会去调它（下面坑 7 会说，它其实**是导出的**，只是当初没绑）。标志为 0 之后 `LLVMCreateTargetMachine` 内部解引用空指针，我复现出的正是这个：

```
Exception 0xc0000005at PC=0x7ff8f9090f5e
```

`0xc0000005` 是 Windows 访问违例。注意**它不是 Go 的 nil dereference**——Go 的空指针会 panic 并打印 goroutine 栈，这里是 DLL 里越界读，Go 侧的栈全是 `syscall.SyscallN` 胶水代码，看着像"系统调用出错"，实际上错在几百公里外的库内部。

**修复**。补上 `LLVMInitializeX86TargetMC`，五步一个不少（`llvm.go:191-195`）。顺序也有讲究：前两步填TargetRegistry，后三步填这个 target 的能力位。

**验证与教训**。最好的验证方式是**不靠崩不崩来判断，而是直接问那个标志**——`LLVMTargetHasAsmBackend` 一次调用就把"是不是少初始化了"变成yes/no，不用猜。可迁移的教训：**当你怀疑某个 C 库的初始化不完整时，去找一个能反映初始化状态的查询函数，而不是反复重试失败的调用**。这个库里它已经导出了，只是当时没想到要用。

### 坑 2：`LLVMGetTargetFromTriple` 的出参顺序和头文件相反

**现象**。按头文件写的 `(triple, &err, &target)` 传，拿到垃圾指针，**而且返回码仍然是 0**——看起来"成功了"。真实签名是 `(triple, &target, &err)`。

**为什么难查**。"返回码是 0 但指针是垃圾"是最难查的一类 bug，原因很具体：出参顺序错了，**编译器不会报错**。两个形参类型都是 `uintptr`（一个是 `LLVMTargetRef*`，一个是 `char**`），Go 这边看到的是两个一模一样的 `unsafe.Pointer(&x)`，类型完全匹配、参数个数正确、栈布局正确——**唯一的差别是这两个 slot 的语义顺序**，而语义是编译器看不见的东西。传错的代价不是崩溃而是指针互换，于是 `target` 拿到的是 error message 的指针（或者反过来）。

更阴的是错误方向的检查会"半通过"：`llvm.go:296` 现在的判断是 `if rc != 0 || target == 0`。传错顺序时 `rc` 仍然是 0（函数确实成功了），`target` 拿到的那 slot 里恰好是个**非空**的已分配地址——因为那一格现在装的是另一个有效指针。所以 `target == 0` 这一半也拦不住。你只有一个证据：这个指针指向的东西看起来不对，但"不对"没法用 `if` 表达。

**根因（附源码位置）**。`llvm.go:290-292` 的注释记着这件事：两个出参的顺序和 C 头文件给人的印象相反，写错会得到一个"看着像指针、rc 又是 0"的结果。对照真实头文件 `llvm-c/TargetMachine.h:90-92`：

```c
LLVM_C_ABI LLVMBool LLVMGetTargetFromTriple(const char *Triple,
                                            LLVMTargetRef *T,
                                            char **ErrorMessage);
```

`T` 在前、`ErrorMessage` 在后——所以 `llvm.go:294-295` 的调用顺序 `(tripleC.ptr(), &target, &errMsg)` **是对的**。"相反"是相对于人读头文件时的直觉：看到函数返回 `LLVMBool`，容易以为第一个出参是错误信息。这不是 LLVM 的设计错误，是C 接口把出参和返回值分成两半之后的阅读陷阱。

附带一个容易误判的点：它返回的 target 是 `TargetRegistry` 里的静态存储，**地址看着像栈地址（`0x7ffc…`）是正常的**，别当成野指针去查。实测拿到的是 `0x7ff8fa4e8ef0`——落在 DLL 映像的地址区间里，不是栈，但长得和栈地址是同一个形状。

**修复**。照头文件写 `(&target, &errMsg)`，并且**两个出参都用变量分开声明**（`llvm.go:293`），不要写成 `&struct{a, b uintptr}{}` 再取字段——那种写法下顺序错误更难看出来。

**验证与教训**。这类 bug 唯一可靠的防线是**把出参解引用到具体用途上验证**，而不是只判 `rc`：`llvm.go:296` 同时判 `rc != 0 || target == 0`，就是让"指针必须非空"成为一条独立的不变量。可迁移的教训：**FFI 调用的"成功"判据不能只有返回码**——对返回指针的函数，指针非空是独立的、必须显式写的判据，因为出参错位恰好能同时骗过两者。

### 坑 3：返回指针的函数不能判 `rc != 0`

**现象**。`LLVMCreateTargetMachine`、`LLVMCreateMemoryBufferWithMemoryRangeCopy` 明明返回非空，却被自己的错误检查报成"失败"。

**为什么难查**。和坑 2 正好相反，这次是**检查太严**导致的假失败：函数成功返回一个好指针，你的代码把它当成状态码去judge，非 0 于是判成失败。迷惑性在于这条逻辑在同一个文件里对别的函数是对的——`llvm.go:296` 判 `LLVMGetTargetFromTriple` 的 rc、`:353` 判 `LLVMParseIRInContext` 的 rc、`:369` 判 `LLVMVerifyModule` 的 rc，这三个都返回 `LLVMBool`（真的是状态码），判 rc 是正确的。**同一个文件里两种返回语义并存**，而"返回指针还是返回状态"这件事C 头文件里没有任何标记告诉你。

**根因（附源码位置）**。这些函数返回的就是指针本身，非 0 即成功，没有状态码语义。**判指针非空**（`llvm.go:331` `if tm == 0`、`llvm.go:346` `if buf == 0`）。`llvm.go:313-314` 的注释专门记了这件事：`LLVMCreateTargetMachine` 的返回值**就是机器本身，不该当成状态码比零**。

> 顺带纠一个写文时的笔误：这里原本写的是 `LLVMCreateMemoryBufferWithContentsOfFile`——那个函数在本项目里从未绑定过（全仓库 grep 零命中）。真正的调用点是 `LLVMCreateMemoryBufferWithMemoryRangeCopy`（绑定在 `llvm.go:61`，唯一调用点 `llvm.go:343-348`）。记下来是因为这类"名字看着合理"的错最难自己发现。

**同一坑的另外三个变体**，都在参数侧：

1. `CPU` / `Features` 参数**必须传空字符串，传 NULL 会让整个编译器段错误**。LLVM 23 不做 null 检查，直接解引用。同一个函数，ctypes 传空 bytes 能过、传 `0` 就崩——这个差异极难猜。`llvm.go:308-311` 记着这条，并解释了为什么传空串是对的：空串选中 generic target，而这里本来就想要 generic。
2. `LLVMCreateTargetMachine` 在 LLVM 23 还是 **9 个参数**（末尾多了 `ThreadCount`），按文档的 8 参数传会读到垃圾。`llvm.go:305-307` 列出了完整签名，`llvm.go:327-329` 传满9 个。我实测过：9 参返回非空、7 参和 8 参**同样返回非空**——所以这个错**不会以崩溃的形式暴露**，而是把 `CodeModel` / `FileName` / `ThreadCount` 三格里的随机内容当成有效值传进库。
3. `LLVMCreateTargetMachine` 的 `Reloc` 参数：Linux 路径必须传 `LLVMRelocStatic`(1)，`llvm.go:317-326` 讲了原因——静态 ELF 镜像没有 GOT/PLT，默认(0) 会让 LLVM 对每个静态符号发出 `GOTPCRELX` 重定位，一个无 libc 的静态镜像既不需要也解析不了。COFF 路径保持 0。

**修复**。返回指针的一律判非空；`CPU`/`Features` 传 `newCstr("")`（`llvm.go:315` 一次创建、复用两次）；`Reloc` 按目标三元组分叉。

**验证与教训**。端到端跑一遍才知道参数个数对不对——我拿一份最小 IR（`define i32 @main() { ret i32 0 }`）走完 `CreateTargetMachine → ContextCreate → MemoryBuffer → ParseIRInContext → VerifyModule → EmitToFile`，产出 581 字节 COFF，`ParseIR`/`Verify`/`EmitToFile` 三个 rc 全 0。可迁移的教训：**ABI 参数个数错位往往不会崩，它只会静默地产出错误结果**——所以这条坑唯一的检测手段是完整跑通一次并检查产物，不是"没崩就算过"。

### 坑 4：`LLVMRunPasses` 调不动——无 cgo 方案的真正技术死点（**已解决**，见本节开头补记）

> **补记（2026-10-07）**：本条当时判断为「限制不是 bug」，**后来被推翻并修好了**。当时卡在 `LLVMStringRef` 这个16 字节聚合按值传（MSVC x64 下等于间接传指针，实际调用形态是 5 个指针参数），Go 变参 `LazyProc.Call` 表达不了「聚合按值传」这条 ABI 规则。修法很轻：**改用 `const char*` 传管线字符串**，4 个参数全是 `uintptr` 指针，ABI 墙直接消失。commit `1ba351a` 已接入，`src/gocl/llvm.go` 现有 `runPasses.Call(mod, passC.ptr(), tm, optv)`。**下面的原始记录保留作为当时判断的依据。**

`LLVMStringRef` 是 16 字节聚合，MSVC x64 ABI 下按值传递等于间接传指针，实际调用形态是 **5 个指针参数**。4 参 / 5 参 / 直接传 `StringRef` 结构体，全都崩。

Go 的 `LazyProc.Call` 是变参 uintptr 调用，**无法表达"聚合按值传"这一 ABI 规则**。

结论是这是**限制不是 bug**：长期只能拿到 TargetMachine 的 `CodeGenOptLevel`，拿不到中端优化管线（GVN / LICM / 向量化）。所有优化必须靠 `irPasses()` 返回的管线字符串。手工拼栈传参可行，但风险过高，没做。

**为什么当时判断错了**。当时把"Go 表达不了聚合按值传"当成了终点，没回头看 LLVM 自己提供了什么。真实签名（`llvm-c/Transforms/PassBuilder.h:50-52`）是：

```c
LLVMErrorRef LLVMRunPasses(LLVMModuleRef M, const char *Passes,
                           LLVMTargetMachineRef TM, LLVMPassBuilderOptionsRef Options);
```

**`const char *Passes`** ——C 侧根本没有 `StringRef`。当时的全部困难都建立在"要自己造一个 `StringRef` 传过去"这个前提上，而这个前提是自己加的。库早就把那个便利封装好了，绕不绕得开是调用方的事，不是接口的事。

**修复**。整条链路现在是这样（`-fllvm` 编一个 `.c` 时）：

1. `compile.go:143` `irPasses(opt)` 把 `-O` 级别翻成管线字符串（`default<O1>` / `<O2>` / `<O3>`，见 `compile.go:144-160`）
2. `compile.go:73` 把它和 `level` 一起交给 `api.CompileToObject(..., level, irPasses(opt), linux)`（`-S` 走 `compile.go:96`的 `CompileToAssembly`）
3. `llvm.go:485-487` → `llvm.go:216-218` 转到 `compileToFile(ir, outPath, opt, passes, fileType, linux)`
4. `llvm.go:378` 在**Verify 之后、Emit 之前**调 `a.runIRPasses(mod, tm, passes)`
5. `llvm.go:236-251` `runIRPasses`：`llvm.go:240` 建 PassBuilderOptions、`llvm.go:245` `newCstr(pipeline)`、`llvm.go:246` `a.runPasses.Call(mod, passC.ptr(), tm, uintptr(optv))` —— 4 个 `uintptr`，ABI 墙不存在了

顺序有讲究且写在注释里：`llvm.go:376-377` 说明优化**必须**在 module 拿到 target 的 data layout 之后，否则管线按错误的 ABI 做推理；`llvm.go:360-362` 正是先 `CreateTargetDataLayout` + `SetModuleDataLayout`。而 Verify 在优化之前（`llvm.go:367-371`），`llvm.go:364-365` 给的理由是：一个畸形的module 否则会变成一个晚得多才失败的对象文件，真正的抱怨无处可寻。

**`LLVMCodeGenOptLevel` 和 `LLVMRunPasses` 是两件事**。这是本坑最容易继续搞混的地方：

- **`LLVMCodeGenOptLevel`**（`llvmapi.go:19-26`，`None/Less/Default/Aggressive` = 0/1/2/3）传给 `LLVMCreateTargetMachine`（`llvm.go:329`），只管**机器码生成强度**：指令选择、寄存器分配、机器码展开。它**不跑任何 pass**。
- **`LLVMRunPasses`** 管**中端 IR 优化**：GVN / LICM / 向量化 / 内联，全部作用在 LLVM IR 上。

两者由`compile.go:69-72` 独立算出（`level`）和 `irPasses(opt)`（管线字符串）独立产生，然后各自传入。**证据**：commit `1ba351a` 的 message 记着，接入 RunPasses 之前"benchmark 的 `-O2` 与 `-O0` 同速(160ms)"，因为 `LLVMTargetMachineEmitToFile` 只做 codegen 不做优化（这个事实在 `llvm.go:229-235` 的注释里也写着）。所以 `CodeGenOptLevel` 调到最高也拿不到中端优化——**两个旋钮，一个管后端一个管中端，缺一不可**。

附带的两处细节：

- `llvm.go:68` / `llvm.go:170` 绑了 `LLVMPassBuilderOptionsSetVerifyEach`，但全仓库**没有调用点**（grep 只命中声明和绑定两处）。一个绑了不用的符号：留着是因为它是完整的 API 面的一部分。
- `runIRPasses` 开头 `llvm.go:237-239` 就有 `if pipeline == "" { return nil }` ——空管线直接跳过，这也让"完全不优化"仍是一个可表达的选项。

> 附带：必须用 `LazyProc.Call` 而不是 `syscall.SyscallN`，因为后者要传裸 `uintptr`，读回还得转 `unsafe.Pointer`，而 **CI 有 `go vet` 的 unsafeptr 门禁**，必然红。（理由写在 `llvm.go:38-43`。）
>
> **现状**：`irPasses()` 不再是「唯一」优化途径，而是**通过 `LLVMRunPasses` 真正跑起来了**。`LLVMCodeGenOptLevel` 只管机器码生成强度（传给 `LLVMCreateTargetMachine`），中端 IR 优化由 `irPasses(opt)` 的管线字符串 + `runIRPasses` 负责，两者是分开的。

**教训**（这一条比修法本身更值钱）：**当你判定"这个接口在Go 里表达不了"时，先去读那个接口自己的声明，确认它到底要求什么类型**。坑 2（出参顺序）和坑 3（返回语义）都是同一个毛病的两个方向——**你没读 C 声明，只读了它的名字和文档印象**，而这三个 bug 全部零成本地消解在真实的头文件里。ABI 墙只在"接口真的用了 Go 表达不了的类型"时才存在，而这三条一条都没到。

### 坑 5：libLLVM.dll 依赖 libzstd.dll，且文件名大小写敏感

`LoadLibrary` 失败。这条坑要分成两半讲，因为**当初以为有效的那个修法后来被自己的排查记录推翻了**——这正是"修好了"和"绕过了症状"的区别。

**第一半：库名只认三个拼法。** `llvm.go:127` 的查找表就是 `{"libLLVM.dll", "LLVM.dll", "libLLVM-9.dll"}` 三个，加上 `GOC_LLVM_DLL` 环境变量优先（`llvm.go:121`）。所以 `libllvm.dll` 小写根本不会被尝试——Windows 的文件系统不区分大小写，但 `LoadLibrary` 的**参数**区分。

注意查找的**范围**也只有一处：`llvm.go:128-136` 只看 `os.Executable()` 所在目录（`filepath.Dir`），不搜 `PATH`。所以"把 DLL 放到 PATH 里"也不work，必须放在 exe 旁边或设环境变量。

**第二半（真正卡住的地方）：把依赖全拷到 exe 同目录，无效。** 最初以为 `libLLVM.dll` 依赖 `libzstd.dll`，那就把它拷到 `bin/`。实测：把 6 个 MSYS2 依赖全部拷到 exe 同目录，逐个 `LoadLibrary` 全部成功，仍然返回 127。原因是 `libLLVM.dll` 有 21 个直接依赖，失败在**二层的传递依赖**上——直接依赖都加载成功了，加载器再去解析它们的依赖时找不到某个 dll，整条链失败。

**为什么难查**。127 这个码**不指向任何具体原因**。它是Windows loader 启动前失败的标准码（`STATUS_DLL_NOT_FOUND`），而"哪个 dll 找不到"这件事没有出现在任何错误信息里。更折磨人的是：单独 `LoadLibrary` 每个直接依赖**全部成功**——这一步是主动做的，做完得到一个**false negative**，让人以为"依赖没问题"。判断依据只能是失败码本身：127 是"某个依赖找不到"，而不是"你自己那个 dll 有问题"。

**根因（附源码位置）**。看 `llvm.go:99-101` 的错误包装就知道这个失败发生得非常早：`dll.Load()` 在 `bind()` 和 `initTargets()` **之前**（`llvm.go:103`、`llvm.go:106`），连一个符号都还没解析。整个 `findLLVM`（`llvm.go:120-138`）只做"选一个路径"，对路径上那个文件能不能真正加载一无所知。

**最终解法是绕开环境**：`GOC_LLVM_DLL` 指向 MSYS2 的完整环境 `D:/msys64/ucrt64/bin/libLLVM-22.dll`（注意是 `msys64`，不是曾经写错的 `msys`）。让 DLL 待在它自己的依赖旁边，比手工复制依赖树可靠得多。

顺带一个安全性上的运气：`llvm.go:122-124` 里 `GOC_LLVM_DLL` 指到一个不存在的文件时，返回的是明确的 `GOC_LLVM_DLL=<path>: ...` 错误（`ErrNoLLVM` 家族），**不是**127。这一点被测试钉住了——`cmd/gocl/main_test.go:225`专门造了一个不存在的库路径，断言输出里含 `LLVM` 且进程失败。这就是 `llvm.go:137` 那句 `set GOC_LLVM_DLL to the library` 的价值：环境问题的报错必须和真·加载失败区分开，否则你会一直在"依赖"里白找。

> 这条坑的教训比结论有用：**"多拷几个 dll 到 exe 旁边"是个看起来很专业的动作，它确实能让 `LoadLibrary` 单点成功，但并不保证整条传递依赖链成立**。判断依据应该是失败码：127 是"某个依赖找不到"，而不是"你自己那个 dll 有问题"。

**验证与教训**。可迁移的一条：**验证依赖要递归整棵导入树，不能只验第一层**。第一层全绿而整体失败，恰恰是传递依赖的典型特征。更一般地说，"逐个 `LoadLibrary` 全部成功"这个看似很强的证据，在动态链接里**一点信息量都没有**——加载器解析依赖时用的是另一套路径查找逻辑，它找的是**依赖的依赖**，你手工验的那批根本没被覆盖。

### 坑 6：静态链接 libLLVM 省29MB，但运行时崩（**路线已放弃**）

下表记录的是 2026-10-04 一时的中间态，**现已不是现状**，保留是因为它解释了后面几个决策的动机：

| 方案 | 大小 | 说明 |
| --- | --- | --- |
| 动态（当时） | gocl.exe 3.2MB + libLLVM-23.dll 109.8MB | **128.4 MB**，两个文件 |
| 静态单个 exe | **99.2 MB** | 一个文件，省 29MB / −23% |

静态版在 `LLVMInitializeX86Target()` 之后段错误。

**为什么难查**。崩溃点是 `LLVMInitializeX86Target()` **之后**——这句话本身就有误导性：崩在A 之后，通常会被读成"A 造成的"，于是反复重试A、换参数试 A、怀疑 A 的符号。而真因是**更早的静态初始化阶段**就已经错了，A 只是第一个用到它的函数。这种"崩溃点 ≠ 出错点"的偏移是链接顺序类 bug 的通用形态。

排除了"库不全"（X86 后端在 `libLLVMX86CodeGen.a`，而 `libLLVMTargetX86.a` **根本不存在**）和"符号缺失"。崩溃点是 `TargetRegistry` 的 **C++ 全局构造顺序**问题——`--start-group` 能解库间循环依赖，解不了 C++ 静态初始化顺序。

**判断**：这是工具链限制不是代码 bug。真要攻，方向是 MinGW 的 `.ctors` 排序（`-Wl,--sort-section=name`），或者改用 clang/lld 构建 LLVM（它们的 `.CRT$XCU` 优先级支持完整，很可能直接能跑）。

**后来怎么样了**：这条路线**已经放弃**。我这次重新grep 了一遍：`libLLVMTargetX86` 在整个仓库**零命中**（坑 6 里提到的那个库名，因此确认不再被引用）；`build.sh` 里没有任何 libLLVM 静态链接的痕迹——它只调`go build`（`build.sh:53`），LLVM 那句注释（`build.sh:49-52`）明确写着 gocl "needs libLLVM at run time"。`--start-group` 在仓库里只有一处命中，在 `src/gocld/cmd/gocld/main.go:110`，而且是 gocld **主动接受并忽略**这个 flag（用于吃下 gcc 风格的链接命令行参数），与 LLVM 无关。`LLVM_DYLIB_COMPONENTS` 也只存在于 `.workbuddy/memory/2026-10-04.md:304` 的排查记录里，不是现行构建配置。

现行方案是动态加载 + 组件裁剪：`LLVM_DYLIB_COMPONENTS` 把 libLLVM 从109.8MB 裁到约 22MB。上面那个"动态 = 109.8MB"的基线早已不存在——**实测现在的 `bin/libLLVM.dll` 是 14,551,357 字节（13.9 MiB）**，比那个"裁到 22MB"的说法又小了一档，所以省体积的动机换了个实现，而"静态链接省 29MB"这个数字在今天没有参考价值。

顺带记一个容易漏的清单：静态链接需要的系统库里有 **`ole32`**（CoTaskMem/CoInitialize）、`oleaut32`、`ntdll`（`RtlGetLastNtStatus`）、`uuid`（`CLSID_FileOperation` / `FOLDERID_*` GUID），漏了都是链接期才报。

**教训**。静态初始化顺序问题的排查成本极高，而且**你在崩溃点附近做的每一个实验都是无效实验**——因为真因在崩溃点之前。可迁移的一条：**遇到"在 X 之后崩"时，先怀疑 X 之前的初始化阶段**；对应的手段是让动态库变成静态库这件事本身就是一个巨大的变量，先解决它、再谈调试。

### 坑 7：LLVM 23 的 C API 导出面残缺（66/78 可用）

真正仍然成立的缺口：`bind()`（`llvm.go:148-174`）只绑定 **25** 个符号（结构体字段在 `llvm.go:47-71`，一行一个，grep 数得出来），`LLVMCreateTarget` 是 C++ 符号未导出 → 改用 `LLVMGetTargetFromTriple`（`llvm.go:294`）；`LLVMGetNumFunctions` / `LLVMGetFunction` / `LLVMGetTargetMachine` 不存在。

**"不存在"是实测的，不是推测的**。我用 `objdump -p bin/libLLVM.dll` 导出了它的导出表，这几个符号的命中数都是 0，而同一批里`LLVMGetTargetFromTriple`、`LLVMTargetHasAsmBackend`、`LLVMRunPasses`、`LLVMInitializeX86TargetMC` 都是 1。顺带一个反直觉的事实：

- **`LLVMCreateTarget` 确实没导出**，但它的邻居 `LLVMCreateTargetDataLayout`、`LLVMCreateTargetMachine`、`LLVMCreateTargetMachineWithOptions`、`LLVMCreateTargetMachineOptions` **全都导出了**。所以"Target 家族缺了一部分"是真的，但只缺这一个。
- **`LLVMTargetHasAsmBackend` 反而是导出的**（实测 `Find()` 成功）——坑 1 里那个能直接告诉你初始化漏了什么的探针，**当初完全可以绑上**，只是没人想到。所以"导出面残缺"这条对**我们需要的符号**成立，对**整个库**不成立：22.1.8（`D:/msys64/ucrt64/bin/libLLVM-22.dll`）的 `LLVM*` 导出有 1301 个，23.1.2（`bin/libLLVM.dll`）有 1038 个，而且两者的差集很有意思——22 独有 274 个（几乎全是 JIT：`LLVMOrcCreateLLJIT`、`LLVMCreateMCJITCompilerForModule`、`LLVMCreateDisasm`……），23 独有 11 个（`LLVMConstByte` 系列、`LLVMIsACondBrInst` 这类新指令判定）。**裁剪的方向不同，不是单纯的"新版变少"。**

> 需要从原文里划掉的一半：`LLVMBuildLoad` → `LLVMBuildLoad2`、`LLVMGetConstInt` → `LLVMBinaryOperator` 这两条**与本项目无关**——gocl 的 IR 前端是**生成文本 IR** 再交给 `LLVMParseIRInContext`（`llvm.go:164`、`llvm.go:351`）解析，从不建 IRBuilder，所以这批builder API 缺不缺根本影响不到我们。那是早期尝试用builder API 时的记录，架构改成文本 IR 后就作废了。（顺带一提，`LLVMBuildLoad2` 在 22 和 23 里**都**导出，`LLVMBinaryOperator` 两版**都不**导出——当初把它们记成"改名了"并不准确，但结论（用不上）不受影响。）**教训**：记"某个 API 缺失"之前先确认自己有没有在用它。

**换 LLVM 22 救不了变参**：22 同样不接受 `vaarg` 指令关键字（两版报同一个 `expected instruction opcode`）。22 只多一个 `LLVMWriteBitcodeToFile`，价值有限。这条我也验了：`LLVMWriteBitcodeToFile` 在 22 里导出（命中 1）、在 23 里没有（命中 0）——确实是 22 独有的那一个，但换不回`-fllvm` 需要的任何东西。

**教训**（和坑 1 呼应）：**"库没导出"和"我要的东西没了"是两件事**。导出表是一份精确的、可一条命令查完的事实（`objdump -p <dll> | grep -cw <symbol>`），比任何"我记得好像没有"的印象都可靠；而它到底重不重要，取决于**你绑的那二十几个符号够不够用**。

> 顺带一个仍未清理的小瑕疵：`gocl` 包的错误前缀全是 `goa:`（13 处，`llvm.go:100` 起，以及 `llvmapi.go:15`、`llvm_stub.go:23`）——那是 LLVM 绑定还在 `src/goa` 时代留下的（commit `407154c` 把绑定搬进 `gocl`），搬家时没改。对功能零影响，但会让人在诊断时怀疑自己是不是找错了程序。
## 二、LLVM IR：畸形输出的重灾区

这一层产出畸形 IR。**几乎每一条都是 LLVM verifier 精确指出来的**——给 `src/examples` 下80 多个 `.c` 跑一遍 `goc -fllvm -S`（`-fllvm` 在 `main.go:758` 被识别，走 verifyModule + AsmPrinter，见 `compile.go:96` 的 `CompileToAssembly`），基线是 57 OK / 25 FAIL。比逐个猜语法快得多。

贯穿全章的是一件事：**LLVM 的报错指向的是它看到的那行文本，不是你写错的那行代码**。下面11 个坑里有超过一半的报错位置是假的现场。

### 坑 8：运算符名不是 C 的拼写

**现象。** 直接把 C 的 `+ - * / % & | ^<< >>` 透传进 IR 文本，verifier 报 `expected instruction opcode`。

**为什么会错。** 报错指向的是 `%t3 = + i32 1, 2` 这一行——看起来像"这条语句语法写错了"，于是人的第一反应是去检查是不是少写了逗号、缩进不对、临时变量名撞了。实际上这行文本完全合法，**错的是 `%t3 =` 后面那个运算符**：它是一个未知词法单元，LLVM 解析器把它当成"这里应该是一条指令 opcode"，发现不是，于是抛出这句话。错误信息说的是"我期待一个 opcode，你给了我一个 `+`"，而不是"你给的 opcode 拼错了"。

**根因（附源码位置）。** 修法就是一张映射表，`operator.go:489` 的 `llirBin`，它开头的注释把这件事写成了规则：

> They are not the same words -- C says "+", LLVM says "add" -- and passing the C spelling through produces a module LLVM rejects with "expected instruction opcode".

表在 `operator.go:490-512`：`+`→`add`、`-`→`sub`、`*`→`mul`、`/`→`div`、`%`→`rem`、`&`→`and`、`|`→`or`、`^`→`xor`、`<<`→`shl`、`>>`→`shr`。注意表里只出 `div`/`rem` 这两个**中性名**——它们不是最终指令名。

**修复。** 映射之后还有第二层：除法和取余在LLVM 里各有有符号/无符号两套变体，得按类型选。`operator.go:530-540` 在 `arith` 里做这件事：

```go
if op == "div" || op == "rem" {
    kind := "sdiv"
    if t != nil && !t.Signed {
        kind = "udiv"
    }
    if op == "rem" {
        kind = "srem"
        if t != nil && !t.Signed {
            kind = "urem"
        }
    }
```

即 `arith` 收到的已经是中性名 `div`/`rem`，它在这里按结果类型换成 `sdiv`/`udiv`/`srem`/`urem` 四个具体 opcode 之一。同样的模式在移位上有第三层：位宽一致时`operator.go:507-510` 的映射给出 `shl`/`shr`，但 `operator.go:123-127` 会按左操作数是否带符号把 `shr`换成 `ashr`（算术右移）——`>>` 在 C 里对负数是实现定义但实践上都是算术右移，`lshr` 会把负数变成巨大的正数。

**验证与教训。** 一次 `expected instruction opcode` 其实是两个不同的病：拼写错（表缺了一项）和变体选错（表有了但忘了分s/u）。前者报在运算符上，后者报在类型上。

**教训：一个只报"这里不是合法指令"的解析错误，通常意味着你的词法表缺了一项，而不是你的语法树错了。**

### 坑 9：`call` 的每个参数必须显式标注类型

**现象。** 调用同模块后面才定义的函数（也就是**递归**）报 `invalid type for function argument`。想在前面补一条 `declare` 缓解，改为报重定义。

**为什么会错。** 这是本章最漂亮的一个"两条路都不通"。`invalid type for function argument` 的真实含义是"**我无法推断这个实参的类型**"——LLVM 读到 `call i32 @f(i32 %a)` 时，需要知道 `%a` 是什么类型，而它唯一的来源是前面某处对 `@f` 的声明。如果 `@f` 在这个模块里**还没有被定义**，就还没有声明，LLVM 无处可查。而递归恰好是"调用自己"，定义在后面。补 `declare` 的思路方向是对的，但撞上了第二个问题：模块后面会有真正的 `define`，一个符号既有 `declare` 又有 `define`，LLVM 认定这是重定义。

注意这两条报错**互相矛盾且都是真的**——第一条说"你缺个声明"，第二条说"你有了声明"。这正是它迷惑人的地方：两个诊断指向完全不同的位置（参数 vs 符号），而根因只有一个。

**根因（附源码位置）。** 两边都在`translate.go:244-253` 这段被堵住了：

> A call to a function defined later in the list would otherwise emit a `declare` for it during the earlier body's generation, and the later `define` would collide with it: LLVM reads a declare followed by a matching define as a redefinition.

解法是**提前标记**：在任何函数体生成之前，就把这一轮所有要定义的函数名全部登记进 `m.defined`，这样递归调用时 `noteExtern`（`module.go:482` 的 `if m.defined[name] { return }`）直接跳过声明。

**修复。** 真正的解法是让实参**不再依赖推断**。`call.go:186-190` 的注释说明了为什么这是唯一稳妥的路：

> Every argument carries its type explicitly. LLVM will infer them from a declaration when there is one, but a function defined later in the same module -- or one whose only definition this module does not have -- leaves nothing to infer from, and the call is rejected with "invalid type for function argument". Spelling the types out is always accepted.

落到 `call.go:240` 这一行，**每个实参前面都拼上自己的类型**：

```go
args = append(args, e.ty(v.ty)+" "+v.op)
```

这条路径对直接调用和间接调用都适用，后者见 `call.go:379` 的 `call i32 %s(%s)`。

**验证与教训。** "Spelling the types out is always accepted" 是这里唯一无条件的真话——显式标注类型在任何情况下都合法，包括本来能推断的情况。这也是为什么它顺手解决了递归和外部函数两个问题，而没有第三种情况需要再处理。

**教训：当一个诊断说"信息不足"时，补齐那条推断链（前置声明）往往比在别处下功夫更难；能让数据自带类型，就不要指望消费者去查。**

### 坑 10：比较结果是 `i1` 不是 `i32`

**现象。** 一个 `if (a > b)` 里的 `a > b` 被拿去 `icmp ne %c, 0`——类型不符。而把控制表达式返回给上层时，`ret i32 %i1` 也非法。

**为什么会错。** 陷阱在于 **C 的类型和 IR 的类型在这个点上是分叉的**：`a > b` 在 C 里是 `int`（这是C 标准明文规定的，`int` 宽度的 0 或 1），在 IR 里是 `i1`。前端如果照着 C 的类型走，就会产出一个"i32 的值"，然后去和另一个 `i32` 比较——比较是合法的，但语义完全错了（把 0/1 的比较结果当成任意整数再用）。反过来，如果照着 IR 的`i1` 走而下游还按 C 的 `int` 理解，就会拿 `i1` 去参与算术，LLVM 直接拒绝。**同一个节点，两套类型系统，没有一处会告诉你你搞混了。**

**根因（附源码位置）。** 需要一个独立的类型标记把两者区分开。`module.go:225` 就是那个标记：

```go
func boolIr() *frontend.Type { return &frontend.Type{Kind: frontend.KBool} }
```

`module.go:234-239` 说明 `KBool` 在 IR 里渲染成什么，以及为什么可以这么做：

> C's _Bool is a byte, but the IR front end uses this type for the result of a comparison and for a reduced controlling expression, which LLVM models as i1. A _Bool stored to memory is written through an i8 slot by the store path, so nothing else depends on this.

即：`KBool` 只用于"已经归约成条件"的场合，存回内存时走`store` 路径的 `i8`槽位，两边不打架。`operator.go:177-180` 是发射比较的地方，它给结果打上这个标记：

```go
// A comparison is i1 in IR. Tagging it frontend.KBool -- which goc's front end
// already treats as a one-byte boolean -- is what stops a later
// controlling expression from trying to compare it against zero.
return val{op: t, ty: boolIr()}
```

**修复。** 两处配套改动。第一处，`expression.go:278-285` 让 `cond` 先看 IR 类型再决定是否要造条件：

```go
// The value's IR type decides, not the C type: a comparison already yields
// an i1 even though C types it as int, and re-comparing that against zero
// would both be redundant and type-wrong.
v := e.eval(x)
ty := e.ty(v.ty)
if ty == "i1" {
    return v.op
}
```

第二处，`i1` 参与算术前必须先提升。LLVM 没有"把 i1 变成整数"的自动转换，`module.go:625-641` 的 `convertTo` 按位宽比较后选 `sext`/`zext`，`i1 → i32` 就走这条，得到 `zext i1 %c to i32`。之后 `i32` 就能正常参与算术和比较了。

**验证与教训。** `cond` 里剩下三条分支各对应一种 C 控制表达式的形态：浮点用 `fcmp une` 跟 `0.0` 比（`expression.go:286-290`）、指针用 `icmp ne ptr %p, null` 而不是整数 0（`expression.go:292-295`，注释说明了混用类型会被拒）、其余整数才用 `icmp ne i32 %v, 0`。写一个"统一把条件归约成 `!= 0`"的辅助函数会同时错掉这三种里的至少两种。

**教训：前端类型系统里每个类型标记都承载"在某个子体系里是什么"的语义；共用一个标记等于把两套体系的差异全塞进注释里。**

### 坑 11：`binaryType` 对纯算术一律 `return nil`（本轮最关键）

**现象。** `(a + b)` 作为更大表达式的操作数时、`x / 64` 里 `64` 是 `long`，都拿到 nil。nil 一路传到类型渲染，`module.go:228-230` 的 `if t == nil { return "i32" }` 把它回落成 `i32`，于是发出 `sdiv i32 %x, %y`——而 `%y` 实际是个 i64 值。LLVM 报类型不匹配，但**报错指向那条 `sdiv`，而真正错的是几百行外那个刚刚返回 nil 的函数**。

**为什么会错。** 这是"一个 nil 毁掉整个类型检查"的典型，而且它的破坏力不在于那一个 nil 本身，在于**nil 会被一个看起来合理的默认值静默吸收**。`module.go:228-230` 的回落不是 bug，它是对的：类型未知时按 `i32` 处理是前端的一贯策略。问题在于这个策略把"我不知道"和"它确实是 i32"变成了同一个值，于是错误信息里所有的症状都被归到这个 `i32` 上，而**罪魁祸首的 nil 早在十层之外被吃掉了**。

更微妙的是，`e.ty(nil)` 在这里并不总是错——如果那个表达式真的恰好是 `int`，回落的结果就是对的。**同一个 bug，在 32 位和 64 位不同的例子里表现完全不同**，这正是它当时没被第一轮核实抓住的原因。

**根因（附源码位置）。** `types.go:335-343` 的注释把这条规则写得很清楚，注意第二句：

> The runtime lowering already widens operands through arithCommon; what this must do is report, for a node that is itself an operand of a larger expression, the type that node will have. A pure arithmetic node (a+b, x/64, a&b) therefore resolves to the usual arithmetic common type, not nil -- a nil here makes an enclosing operator pick the wrong width and emit, say, an i32 sdiv against an i64 operand.

关键在第一句：**运行时的降级路径本来就用 `arithCommon` 算对了宽度**。`operator.go:260-266` 的 `usualArith` 调`arithCommon(lty, rty)`，拿到公共类型再把两个操作数转过去。所以 `x / 64` 这个表达式**被生成出来的那一行指令，类型是对的**。

错的是**静态类型查询**。`binaryType` 是 `exprType` 的一个分支（`types.go:252-255`），当这个节点不是最外层、而是某个更大表达式的操作数时，`exprType` 要回答"这个子表达式是什么类型"。原来的实现对纯算术节点 `return nil`——理由大概是"我不需要知道具体类型，降级的时候 `arithCommon` 会算"。但这个假设只在节点处于最外层时成立。**运行时的动态求值路径和编译期的静态类型查询是两条独立的路径，只有最外层节点才走前者。** 节点一旦嵌套，就只剩后者，而后者当时是空的。

**修复。** `types.go:364-404` 现在按运算符分类返回：

- `* / % & | ^`（`types.go:396-397`）→ `arithCommon(lt, rt)`。这就是坑的正解：这六个跟 `+ -` 一样遵守通常算术转换。
- `<< >>`（`types.go:398-403`）→ 返回**左操作数**的类型，不是公共类型：
  ```go
  // A shift keeps the left operand's type; the right operand only sizes it.
  if lt != nil {
      return lt
  }
  ```
  这是 C 的规则，和运行时 `operator.go:106-115` 一致（那边也只用左操作数当结果类型）。
- 比较（`types.go:369-373`）→ `arithCommon(lt, rt)`，注意**不是** `i1`：结果确实是 `i1`，但一个比较节点作为更大表达式的操作数时，外层需要的是它**操作数的**公共类型。
- `&& ||`（`types.go:365-368`）→ C 规定结果是 `int`，返回 `IntType()`。
- `+ -` 单独处理指针算术（`types.go:374-395`），其中 `ptr - ptr` 返回 64 位有符号整数（`ptrdiff_t`，`types.go:377-379`）。

**验证与教训。** `arithCommon` 本身（`operator.go:282-317`）是这条链上另一个值得看的点：它开头先对两个操作数各跑一次 `promoteInt`（`operator.go:283-284`），实现 C 的整数提升。`operator.go:268-281` 的注释记了它为什么必须存在——不带提升的话 `unsigned char` 减 `unsigned char` 会在一字节寄存器里算再零扩展，`'a' - 'b'` 得到 255 而不是 -1，**而这正是让 qsort 变成空操作的那个 bug**：它的比较函数返回 `strcmp` 的值，那是个 `unsigned char` 差，于是每一次"小于"判断都是真的。

同一批修复里还有几个同源问题，都属于"静态类型查询覆盖不全"这一个类：

- 下标没有扩展到 i64。`operator.go:232-254` 的 `toI64` 处理三种情况：signed → `sext`、unsigned → `zext`、指针 → `ptrtoint`。`operator.go:205-210` 说明了为什么必须扩展：C 的指针算术在 `ptrdiff_t`（64 位）里跑，而下标本身是普通 `int`（32 位），不扩展 LLVM 会拒绝 i32 下标配 i64 基址的混合。
- `toInt` 对整型常量误发 `ptrtoint`。`operator.go:185-198` 现在只在**真的是指针**时才发：
  ```go
  // Emitting ptrtoint against an integer, as the
  // old nil-treating branch did for constants, is rejected by LLVM ("ptrtoint ptr
  // 63" -- 63 is not a pointer).
  if v.ty != nil && v.ty.Kind == frontend.KPtr {
  ```
  这又是一次"nil 被当成别的含义"——这里 nil 被当成"指针"，因为前端对无类型常量的约定是留 nil。
- 移位计数宽度必须与被移值同宽，见 `operator.go:116-120` 的转换。
- `exprType` 漏了一串节点。`types.go:165-295` 现在逐个补齐了：`NumLit`（`types.go:167-185`）、`StrLit`（`types.go:186-189`）、`Unary -` 和 `~`（`types.go:226-228`，注释说"this matters for width-sensitive nesting such as `~x | y`"）、`AssignExpr`（`types.go:256-261`）、`CondExpr`（`types.go:264-269`，它返回 `arithCommon(Then, Else)`）、`IncDecExpr`、`CastExpr`、`Call` 等。

`types.go:168-172` 和 `types.go:258-260` 各举了一个具体的连带后果，值得对着读——后者是 `"(*d++ = *src++) != 0"`：赋值表达式的值是左操作数的值，类型也得取左操作数的，读成"一个 i32 对比一个一字节 load"。

**教训：`nil` 在一条有默认值的链路上不是"缺失"，是"沉默的错误答案"。凡是让 nil 回落到某个具体值的地方，都要问一句"回落成这个值之后，原本能暴露的错误会不会变成一个看起来合法的值"。**

### 坑 12：`NumLit.IsFloat` 语义误用

**现象。** `3.14159`（**无后缀**）被走进整数分支，输出 `double 0`。NAN / INFINITY 宏同理——它们是 `0.0/0.0` 和 `1.0/0.0` 之类的浮点表达式。结果是二十多处 `sdiv i32 0, 0`。

**为什么会错。** 一个名字叫 `IsFloat` 的布尔量，读起来就是"是不是浮点"。于是所有判断浮点的代码都去读它。而它其实回答的是另一个问题：**"有没有 `f` 后缀"**。`3.14159` 的答案是 `false`，但它显然是浮点数。名字和语义错位，而错位的方向正好是"看起来对"的那一侧——如果 `IsFloat` 表示"是 float 宽度"，那 `3.14159f` 返回 true 反而更让人误解。

这类的隐蔽之处在于：**错的不是那个字段，是所有把"宽度选择"当"种类判定"来用的地方**。而字段本身没错，它在 `floatOrDouble` 里就是对的。

**根因（附源码位置）。** 两处注释把这个语义写清楚了。第一处是 `util.go:18-27`：

```go
// floatOrDouble picks the C type a floating literal has: an unsuffixed literal
// is a double, an "f" suffixed one a float.
func floatOrDouble(n *frontend.NumLit) *frontend.Type {
	// IsFloat is the "f" suffix: it picks float, and everything else that
	// reached here is a double.
	if n.IsFloat {
		return frontend.FloatType()
	}
	return frontend.DoubleType()
}
```

第二处是 `translate.go:691-695`，`constScalar` 里的：

> Kind == frontend.TDouble is what marks a literal as floating point at all. IsFloat only says WHICH width -- "1.5f" is float, "1.5" is double -- so testing it alone sent a plain double literal down the integer path and emitted "double 0" for "3.14159".

判浮点一律用 `Kind == frontend.TDouble`，`IsFloat` 只用来在 float 和 double 之间选宽度。

**修复。** `expression.go:326-348` 的 `numLit` 是主路径，改成先按 `Kind` 分流再按 `IsFloat` 选宽度：

```go
// Kind == frontend.TDouble marks the literal as floating point; IsFloat then only
// says whether it is float or double ("1.5f" versus "1.5"). Testing IsFloat
// alone made every unsuffixed literal -- "1.0", "0.0", and so the NAN and
// INFINITY macros -- look like an integer, so a division of them was
// emitted as "sdiv i32 0, 0" and the surrounding double arithmetic lost its
// type.
if n.Kind == frontend.TDouble {
```

`exprType` 那边同样要改，见 `types.go:176-181`，注意它也保留了同样的分工：`Kind` 判种类，`IsFloat` 选宽度。

顺带记一个同段的坑，见 `expression.go:337-341`：浮点常量必须写成 16 位double 形式，**即便它是 float**：

> The 16-digit form is what LLVM reads for float as well as double, and it must be exactly representable in the type: for float that is the bit pattern of the double the float widens to, so 5.0f is 0x4014000000000000 and not the 32-bit pattern 0x40a00000 padded out, which lands in the subnormals and is rejected.

`translate.go:697-702` 是对应的全局常量版本，同样的道理。

**验证与教训。** 这个坑在代码审查阶段被独立踩到两次——两次都是有人新写了一段判断浮点的代码，又去读了 `IsFloat`。两次都不是"忘了看注释"，而是名字把人带过去了。

**教训：一个布尔字段如果被取用了两个语义，就该拆成两个；靠注释去纠正一个比名字更具体的名字，成本是每次新增调用点都要重读一遍注释。**

### 坑 13：struct 尾部padding 成员是错的

**现象。** `struct S { char *p; int n; }` 被发成 `{ ptr, i32, [8 x i8] }`——24 字节，C 里是 16。

**为什么会错。** 这是本章唯一一个**过度补偿**的例子，方向和其他坑相反：不是少做了什么，而是多做了一件事。而且它做得很"合理"——用 C 的 `sizeof` 减去各成员宽度之和，得到 8（16 - 8），补一个 `[8 x i8]`，凑成 24……等等，16 是补之前的数，补完变24，这个算术本身就不自洽。但它之所以难以发现，是因为**在只有一个成员的 union 上，同样的直觉会给出正确结果**（`union { char *p; int x; }` 最宽成员 8 字节、成员自己就占 8、pad 确实是 0），所以人很容易把 struct 的做法也套上去。

**根因（附源码位置）。** `module.go:358-364` 的注释记录了这件事，而且它点出了这个过度补偿的真正危害——**不止是尺寸变大**：

> No trailing pad member. LLVM lays a struct out from its members per the target datalayout -- inserting the same internal and trailing padding the C ABI does -- so "{ ptr, i32 }" is already 16 bytes with the right alignment. Adding a pad member of our own both inflates the size (a pad rounded up to the struct's alignment made it 24) and puts a member the C source never mentions in the type, which every brace initialiser then fails to fill ("initializer with struct type has wrong # elements").

第二句才是关键：多出来的那个 `[8 x i8]` 成了类型里的一个**成员**。于是任何 `struct S s = { p, n };` 这样的花括号初始化都少了一个元素——**同一个 bug 在类型定义处和常量构造处各炸一次，而且报错完全不同**。第一条是"尺寸不对"（可能要到运行期访问越界才看出来），第二条是 `initializer with struct type has wrong # elements`（编译期就炸，但指向的是初始化语法而不是类型定义）。这就是过度补偿的典型危害：**它把一个布局问题扩散成了一个语法问题。**

LLVM 本来就按 datalayout 自动补齐，包括内部 padding 和尾部 padding。

**修复。** `module.go:347-356` 现在只遍历成员列表拼类型，没有任何 pad 逻辑：

```go
parts := make([]string, 0, len(t.Members))
for _, mem := range t.Members {
    parts = append(parts, m.llirType(mem.Type))
}
```

union 那边是另一个方向的错，但同样关于pad 的计算。`module.go:332-346` 现在用 `unionLayout`：

```go
// A union is as wide as its widest member, and its first member
// already occupies that width -- only the remainder, if any, needs an
// explicit pad. Adding the whole width on top of the member doubled
// it: "union U { char *p; int x; }" came out as "{ ptr, [8 x i8] }",
// 16 bytes for what C calls 8.
```

关键区分：union 确实**需要**显式 pad（因为 IR 只放了一个成员，得手工补足 C 的 `sizeof`），但 pad = 最宽宽度 − **首成员**宽度，不是 − 0。`module.go:374-395` 的 `unionLayout` 算这个值，而且 `module.go:370-373` 说明它为什么是个共享函数：

> The type emitter and the constant initialiser both go through this so they cannot disagree about how many members a union has.

——类型发射和常量初始化都走它，两边就不会对"union 有几个成员"产生分歧。`translate.go:821-825` 是常量侧的对应代码。

另外 union 初始化按 C 语义**只填第一个成员**，`translate.go:806-808`：

```go
if t.Kind == frontend.KUnion && i > 0 {
    break // C initialises only the first member of a union
}
```

**验证与教训。** struct 的 pad 是纯多余，union 的 pad 是必需但要算对。两者共用一个"补 padding"的直觉，结果一个多补一个少补，症状还不一样。

**教训：LLVM IR 的类型定义是"声明"不是"布局"。你写下的每个成员都会被当成真实成员参与初始化和布局，把 C 层的布局细节（如 padding）翻译进去就是把一个问题复制成两个。**

### 坑 14：聚合常量语法的两个硬约束

**现象。** 嵌套 struct 必须带类型名（`%point { i32 1, i32 2 }`），跳过元素必须写 `zeroinitializer`。裸 `{ ... }` 报 `expected '}'`。

**为什么会错。** `expected '}'` 是个彻底误导人的报错——它精确地指向那个**完全合法**的裸 `{`。LLVM 解析到 `{` 之后期待一个**类型**（因为它需要知道这是在给谁做初始化），结果读到了一个值，于是说"我以为这里要结束这个结构体"。真实原因是前面缺了一个类型名，报错却落在花括号上。第二个约束更隐蔽：跳过元素**必须出现在列表里**，只不过写成 `zeroinitializer` 而不是省略——C 里`{1, 0, 3}` 的那个 0 你以为可以省，但 LLVM 的聚合是定长的，槽位数必须对上。

**根因（附源码位置）。** `translate.go:838-881` 的 `typedInAggregate` 把这两条都写成了代码，注释里带着 LLVM 的原话。第一个约束在 `translate.go:863-868`：

> A nested aggregate is spelled "<type> { ... }". A bare "{ ... }" is taken for the enclosing body and the reader reports "expected '}' at end of struct" -- and flattening it out ("{i32 1, i32 2, i32 3, i32 4}") is rejected as having the wrong number of elements.

注意它连"另一条可能的错误修法"也堵上了：把嵌套结构摊平（4 个元素而不是 2 个结构体）会撞上元素个数不符。

第二个约束在 `translate.go:850-855`：

> A skipped element still has to say what it is: inside an aggregate a bare "zeroinitializer" is read as a type. It is only at the top of a global ("@g = global [3 x %point] zeroinitializer") that the type is already written.

分界很清楚：**只有顶层全局**的类型是已经写好的（就在 `@g = global [3 x %point]` 里），往下一层就都得自己带类型。同一个函数 `translate.go:857-861` 处理字符串常量，是同一条规则的另一个实例：

> A bare c"..." does NOT: inside an aggregate LLVM wants "[6 x i8] c\"hello\\00\"".

**修复。** `translate.go:846` 的 `typedInAggregate` 是这条规则的集中实现，函数签名注释在 `translate.go:838-845` 概括了它：

> A struct body and a nested array are "{ ... }" and "[N x T] ...", which already say what they are. Everything else -- an integer, a pointer, a getelementptr, a null -- has to be written "<ty> <value>" when it sits inside an aggregate, or the reader takes the value for a type.

**这里的关键是"类型来自文本"而不是"类型来自上下文"**——`typedInAggregate` 唯一的信息来源就是传入的 `t *frontend.Type`，所以每一条分支都在补`<ty> ` 前缀。指针那支（`translate.go:871-875`）连`null` 都要加前缀，注释说明了原因：`"ptr null"` 被接受，裸 `null` 会被当成类型的开头（`expected type`）。

**验证与教训。** 这两条不是查文档查来的，是搭了个**临时 IR oracle** 实证出来的——直接拿 `.ll` 文本喂 `CompileToObject`，看 LLVM 到底收不收，比读文档快得多，而且能拿到原话报错原文。这个 oracle 后来固化成了两个测试：`gocir_test.go:97` 的 `TestLLVMAcceptsGeneratedIR`（正向，把生成的 IR 喂给 LLVM，要求产出非空 object）和 `gocir_test.go:128-149` 的 `TestLLVMRejectsBadIR`（反向对照，用一个无终结指令的块证明"失败"是真实拒绝而不是测试悄悄通过）：

```go
`t.Fatalf("LLVM accepted a module with an unterminated block; " +
    "the front end's own checks cannot be trusted")
```

`TestLLVMRejectsBadIR` 的存在很关键：只有一个"LLVM 接受我的 IR"的测试，你无法区分"生成正确"和"LLVM 没在看"。

**教训：写代码生成器时，一个"把生成的文本喂回后端"的双向测试（含一个必须失败的对照）比任何静态检查都更能告诉你语法约束的真实边界。**

### 坑 15：`switch` 里三个独立 bug

**现象。** 三个各自独立的 bug，同一个函数。

① 无条件 `trunc`：`trunc i32 %v to i32` 和 `trunc i8 %v to i32` 都非法。
② default 语句被塞进上一个 case。
③ 用 `*switchCase` **指针**跟踪当前分支。

**为什么会错。**

①**的方向和坑 18 的 off-by-one 是同一类：`trunc` 本身在某些方向上是对的**（i64→i32 合法），所以"无条件发 trunc"看起来是安全的默认值。但 LLVM 拒绝的是**同类型转换**和**反向的窄化方向**：`i32→i32` 是自己转自己，`i8→i32` 是变宽，窄化方向是 `i64→i32` 才对。

② 的误导性在于 C 的 fall-through 语义让人以为 default 会"落到"某个 arm——它不会，default 自己就是一个分支目标。

③ 是最有意思的一个。

③**根因（附源码位置）。** 用指针跟踪 slice 的当前位置：

```go
var cur *switchCase          // ← 指向 cases 里的某一项
for _, st := range body {
    case *CaseStmt:
        cases = append(cases, switchCase{...})
        cur = &cases[len(cases)-1]   // ← 每次 append 后都重新取，看起来是对的
```

`append` 会扩容，扩容时**把底层数组整体搬到新地址**。旧地址上仍然留着旧数据（指向旧数组的那次 append 写进去的值），内存也没被清掉——所以指针**不会崩，不会报越界，不会 panic**。它只是安静地指向了一个已经不再被 slice 使用的地方。

于是代码继续 `cur.stmt = st` 往那个陈旧位置写入，最后 `range cases` 遍历真实 slice 时读到的那个位置的 `stmt` 还是 nil，或者更糟，是**前面某次 append 时留下的上一个 case 的值**。

**发现它的方式**是看症状而不是看代码——`statement.go:269-273` 的注释记下了这个"how"：

> An index, not a pointer: append may move the backing array, and a pointer taken before the next append would then name the wrong case -- which showed up as a default arm's statements being emitted under another arm's label.

症状是**default 分支的语句跑到了别的 label 下面**——不是崩，不是类型错，是**生成的代码语法完全合法、语义完全正确地执行了错误的分支**。一个静默 produce-valid-but-wrong 的 bug。改成索引：

```go
// An index, not a pointer
cur = len(cases) - 1
```

**修复。**

① `statement.go:251-253`：

```go
if e.ty(sty) != "i32" {
    sv = e.convert(sv, sty, frontend.IntType())
}
```

`statement.go:244-250` 的注释把三种情况都列了，关键在最后一句——i32 的情况**必须直接用操作数**，不能复制：

> The i32 case uses the operand directly; a copy would need a real instruction, and a bare "%t = %t" is not one.

② `statement.go:274-280` 给 default 建自己的 `switchCase`（`val: -1` 标记）并单独记 `defIdx`，注释在 `statement.go:275-277`：

> A default arm is a branch target like any other, and it keeps its own statements: they belong to the default block, not to whichever case happens to precede it.

最后 `statement.go:300-303` 从 `cases` 里取出 default 的label。

③ 见上，改索引。

顺带记一个同一函数里的第四个坑，因为它有相同的"静默 produce-wrong"性质：`break` 栈。`statement.go:307-319` 记着 `break` 会跳到外层的 `while`/`for` 而不是 switch 自己，`strftime` 的格式循环（一个套着 switch 的 while）里 `%Y` 直接跳出循环、后面所有转换被丢掉，`"%Y-%m-%d"` 只格式化出 `"2025"`；`%F` 看起来对只是因为它在同一个 arm 里写完了整个日期。`continue` 则**故意不**压栈（`statement.go:317-318`），因为在 C 里它属于循环不属于 switch。

`statement.go:326-330` 还记了第四个坑的最后一块拼图——每个 arm 都需要终结指令：

> Every arm needs a terminator, including one whose statements all ended in a jump: LLVM requires each basic block to end in one, and a label followed straight by the next label is not a block at all ("expected instruction opcode").

顺带闭环了坑 8：`expected instruction opcode` 有两种完全不同的成因——运算符拼错，以及块没有终结指令。

**验证与教训。** `statement.go:262-291` 现在用一个单遍循环完成"收集 case + 挂语句"，`switchCase` 结构体在 `statement.go:339-343`（字段 `val`/`l`/`stmt`，注意是**值**类型，不是指针，所以 append 搬走数组时 `c.stmt = st` 写的是新的那一项）。

**教训：在Go 里，`slice[i]` 取地址后再 append 是拿`&slice[i]` 之后失效的指针。这类 bug 不会 panic 也不会越界——它安静地指向旧数据。追踪 slice 下标一律用整数索引，且在注释里写明原因，否则后人"优化"回指针时没人会重读这个坑。**

### 坑 16：函数名用作值时 decay 成 null

**现象。** `printf_lite_with(vfmt_i, ...)`——传一个格式化函数给C 运行时——生成的 IR 里那个参数是 `store ptr null`。程序**编译链接全过**，运行时**跳飞**。

**为什么会错。** 这是本章最恶劣的一个：IR 合法、链接通过、没有任何诊断，然后在运行时以 `printf` 内部某处的随机崩溃形式出现。报错信息（如果有的话）在离真正原因几个调用栈之外的地方。而它偏偏是 goclib **自己**的C 代码调gocl 的函数——所以它会表现为"gocl 生成的代码总是段错误"，很容易被误判成代码生成器整体有问题，而不是一个具体的 decay 规则。

**根因（附源码位置）。** `ident` 函数末段有一句注释，**写对了规则但没实现**——`expression.go:418-419`：

```go
// A name with no storage is a constant (an enum member) or a function used
// as a value. The constant case is what the checker leaves behind.
```

注释识别出了这个case，但代码走的分支是 `constValue`，没有命中就落到 `zeroLiteral`，函数名在那条路上变成零值。

为什么类型系统没能发现？因为 `exprType` 对一个可调用的名字**返回的是它的返回类型**。`types.go:64-71` 的注释专门解释了为什么不能用类型查询来代替 decay 判断：

> It exists for the function-designator conversion: a bare function name in a value context (assigned to a pointer, passed as an argument) *is* the function's address. It cannot be inferred from exprType, which reports a call's result type for a name that is also callable -- asking whether the name has a type answers "int" for `twice` and misses the conversion entirely.

也就是说 `twice` 明明有类型，只是那个类型是**返回值** `int`。要判断 decay，得问的是"这个名字是不是一个函数定义"，而不是"这个名字有没有类型"——这是两个不同的问题。

**修复。** 新增 `typeResolver.isFuncName`（`types.go:72-75`）和 `irEmitter.fnPtrTy`（`types.go:80-86`），在 `expression.go:433-435` 接上：

```go
if e.tr.isFuncName(n.Name) {
    return val{op: "@" + n.Name, ty: frontend.PtrType(e.fnPtrTy(n.Name))}
}
```

`fnPtrTy` 带着参数表（`types.go:85` 的 `frontend.FuncType(f.Ret, f.ParamTypes)`），注释说明了为什么参数表重要——`types.go:77-79`：

> The parameter list is carried over so the pointer type matches the declaration, which is what lets the indirect call type-check.

这里有一个关于 LLVM 的知识点，也是这条修复的核心，注释在 `expression.go:423-428`：

> A function used as a value decays to its own address. C spells this "function designator conversion"; in LLVM a function *is* its address, so the symbol reference is already the pointer and no load is involved -- **loading would read the instruction bytes at the entry point instead**.

**在 LLVM IR 里，一个函数符号本身就是它的地址。** `@twice` 已经是 `ptr` 了，**不能再 load**。加一个 load 会去读入口点的指令字节（`push rbp; mov rbp, rsp; ...` 的机器码），得到一个指向代码的数值当指针用。这就是为什么修复不只是"少一条 `null`"，而是"直接用裸符号、不发 load"——而局部变量（`expression.go:399-402`）和全局标量（`expression.go:412-417`）**都需要** load，`ident` 开头 `expression.go:380-384` 的注释就是在讲这个区分。

顺带说，`expression.go:436-442` 那个最终 `zeroLiteral` 的兜底也被重写了，注释记录了它之前的问题：

> Emitting "null" unconditionally put a pointer constant where a long was expected, and LLVM rejected the comparison "icmp sle i64 null, %v".

**验证与教训。** `expression.go:428-432` 记了具体的受害者：

> This is what makes `int (*fp)(int) = twice;` and `apply(twice, 21)` work. Without it they silently produced a null pointer, and the program crashed at the first indirect call -- which is how goclib's own `printf_lite_with(vfmt_i, ...)` took the program down: the formatter was handed a null function pointer and called through it.

**不是 printf 专属**：任何 `int (*fp)(int) = twice;` 在 LLVM 后端下都段错误。间接调用那条路径见 `call.go:172-177`，它也把"被调用的名字其实是函数指针变量"和"真的是函数"区分开了。

**教训：代码生成器里"降级成零值"的兜底分支比任何一条报错都危险——它让一个必须显式处理的语义（decay）缺失变成了一个可以正常编译的合法程序。规则要写在注释里的同时，必须有一条 assert 或测试证明那条路被走到过。**

### 坑 17：decay 规则（约 10 个 examples 的最大类）

**现象。** 两种方向都错：

- 数组 / 全局该取基址却发load：`load i32, ptr @G_g_arr`，然后把这个 load 出来的值当指针用；
- 全局标量该load 却漏 load：`icmp ne i32 %t11, @G_g`（拿符号本身去比）。

**为什么会错。** 这两个错误长得不一样，但**根因是同一个**：`decay`（数组退化为指针）和 load（取地址里的值）这两个决策，都依赖同一个东西——**全局变量的类型**。类型没查到，`ident` 就不知道这个符号是数组还是标量：当成数组就该取基址，当成标量就该 load。

于是**未注册的全局会同时在两个方向上出错**，取决于那次误判落在哪一边。而且症状都是**畸形 IR**（不是缺失符号），因为 `exprType` 返回 nil 时 `llirType` 回落`i32`，一切都有形状、只是形状是错的。

**根因（附源码位置）。** `translate.go:154-162` 把这个连锁反应写得很完整：

> Register the type first, whatever happens to the definition below. Expression typing looks a name up in tr.globalTyp, and a global that was never registered there resolved to no type at all -- which the emitters read as i32. A file-scope array then loaded its first element instead of decaying to its address, so every subscript of a global array came out as "ptrtoint ptr <i32>" and LLVM rejected the module; a global scalar in a comparison was compared against the symbol itself instead of being loaded. Registering the type is what makes both of those decay and load correctly.

**"注册类型"必须无条件先于"生成定义"**，注释第一句就是 `whatever happens to the definition below`。类型查询发生在表达式生成期间，而定义可能被后面的分支跳过（不可支持的初始形式、TLS 变量、不可达）——这些情况下全局依然需要一个正确的类型。

decay 与 load 的分派点在 `expression.go:378-387`，注释说明了为什么决策要集中在这里而不是散落在每个使用点：

> A local is a slot; a global is a symbol. Both are addresses, and the value is a load from it -- except for an array, which decays to its address. Deciding that here rather than at every use is what C means by "an array is converted to a pointer", and it is why `a[j]` on an array parameter does not try to load the whole array as a value.

全局数组那条在 `expression.go:412-415`：

```go
if sym := e.c.globalSym(n.Name); sym != "" {
    if ty != nil && ty.Kind == frontend.KArr {
        return val{op: "@" + sym, ty: frontend.PtrType(ty.Elem)}
    }
    return e.load("@"+sym, ty)
}
```

**修复。** 修复和 lib 全局裁剪一起做的，结论是"**类型注册不裁剪，定义才裁剪**"。`translate.go:490-493` 是这条设计决定的理由，写得很直白：

> Types were registered earlier, for every global, and are deliberately not pruned: a name's type is what makes a subscript decay and a scalar load, and getting that wrong produces malformed IR rather than a missing symbol. Only the storage is pruned.

**这句话解释了整个取舍**：裁剪**定义**的代价是"可能少一个符号"——一个显式、响亮的失败，链接器会告诉你缺什么。而裁剪**类型**的代价是"生成畸形 IR"——一个安静的失败，会一路带到后面某个不相干的报错上。**两种代价不对称，所以选前者。**

对比的两侧：`translate.go:126-145` 对 lib 全局只注册类型和标记 `defined`（`defined["G_"+lg.Name] = true`），定义则交给 `emitLibGlobals`（`translate.go:494`）在可达性分析后按需生成。为什么值得裁：`translate.go:127-131` 给了具体数字——运行时声明了约 25 个文件级变量，一个只调 `write()` 的程序一个都不需要，全发就是白白多 1 KB 的 `.data`，大到让 `print("hello world")` 在 `-fllvm` 下比原生生成器**还大**。

`translate.go:138-143` 还解释了 `defined` 标记为什么无论如何都要设——它同时是 `genWith` 的剪枝依据，所以符号恰好只有一个 owner：

> genWith skips these names on the strength of this same map, so the symbol keeps exactly one owner: if the reachability walk were to miss a reference, the symptom is a link error naming a global, not two definitions of one.

同样地，`translate.go:164-165` 处理了顺序：程序自己的全局在运行时之后注册，所以用户定义可以遮蔽同名的运行时符号。

**验证与教训。** 这是约 10 个 examples 受影响的"最大类"，因为文件级全局数组在 C 里太常见了——一个下标发出去就变成 `ptrtoint ptr <i32>`，整个模块被拒。修完之后 decay 的三条路径（局部数组 `expression.go:385-387`、全局数组 `expression.go:412-415`、字符串字面量 `expression.go:363-376`）走的是同一套判据。

**教训：可静默失败的信息（类型、字段、签名）必须无条件地全量注册；只有可显式失败的东西（存储、定义）才值得按需裁剪。区分"缺了会安静地错"和"缺了会响亮地错"，是这类优化唯一的判断依据。**

### 坑 18：`alignOfLlir` off-by-one

**现象。** 浮点对齐错、`i1` 对齐成 0（非法）。修好之后暴露出一个更严重的问题：Win32 `WriteFile` 在奇地址上故障。

**为什么会错。** 这是本章唯一一个**判据里写了但恒假**的条件。`module.go:271` 的 `alignOfLlir` 原本长这样：

```go
case len(ty) > 5 && ty[:5] == "float":
```

`len(ty) > 5`：字符串 `"float"` 的长度**就是5**，`5 > 5` 恒假。所以 `float` 的对齐永远取不到这一支，掉到函数末尾的 `return 1`。`"double"` 和 `float` 这条判据一起写的话同样也漏（要看原写法），总之浮点的对齐全是 1。

**这个 bug 的隐蔽性在于它不会报错**：`module.go:176-177` 是 `alignOfLlir` 唯一的调用点，它把结果直接写进 global 的 `align`：

```go
b.WriteString("@" + g.name + " = " + m.dso() + "global " + g.ty + " " + init + ", align " +
    itoa(alignOfLlir(g.ty)) + "\n")
```

`align 1` 对 float 完全合法（1总是满足的对齐），LLVM 不做任何抱怨。**产物是个合法但比正确值更弱的对齐**，性能损失是渐进的，永远不会有人注意到。

附带问题是 `i1`：`module.go:281-286` 现在写着

```go
// i1/_Bool is a byte in memory; n/8 would give 0, and LLVM rejects
// an alignment of 0.
if a := n / 8; a >= 1 {
    return a
}
return 1
```

整数分支把 `ty` 的数字部分解析出来算 `n/8`。对 `i8`/`i16`/`i32`/`i64` 都对（1/2/4/8），但 `i1` 算出来是 `1/8 == 0`，而 **LLVM 拒绝 `align 0`**。所以钳到 `>= 1` 不是锦上添花，是必需的。

**根因（附源码位置）。** `module.go:287-292` 是修好后的形态：

```go
case ty == "float":
    return 4
case ty == "double":
    return 8
case ty == "ptr":
    return 8
}
```

精确相等比前缀比较更不容易犯这类错，而且 `float`/`double`/`ptr` 是全部可能的浮点和指针拼写（`llirType` 的 `KFloat`→`"float"`、`KDouble`→`"double"`、`KPtr`/`KFunc`→`"ptr"`，见 `module.go:255-260`）。

**修复与附带修好的东西。** 修掉判据之后，`[1024 x i8]` 这类仍返回 1，于是暴露出第二个问题，而且这个比off-by-one 严重得多。`module.go:294-304`：

```go
// An array or a struct is as aligned as its elements. Reporting 1 for
// "[1024 x i8]" put a char buffer the C runtime hands to a Win32 API on an
// odd address, and WriteFile wrote through it faulted: the API assumes the
// alignment its own prototype implies, not the alignment of the element.
// "[3 x i64]" is the Windows x64 va_list, which is only ever accessed
// through pointers, so it keeps its own tighter answer above.
if strings.HasPrefix(ty, "[") {
    if i := strings.Index(ty, " x "); i > 0 {
        return alignOfLlir(ty[i+3 : len(ty)-1])
    }
}
```

解析 `" x "` 之后递归回自己的元素类型。注释里还留了个例外说明：`[3 x i64]`（Windows x64 的 `va_list`）只通过指针访问，所以它保留上面那条更紧的答案。

**症状值得单说**：`WriteFile` 通过一个奇地址的 char 缓冲区写入时崩溃。原因是 Win32 API 假定参数的对齐符合它自己原型声明的暗示（一个 `const char *` 被理解成8 字节槽），而不是缓冲区元素的对齐。**IR 完全合法、程序完全正常，只有在真的调用那个 API 时才fault。**

**验证与教训。** 三层：整数（含 `i1` 钳位）、浮点/指针精确匹配、数组按元素递归。每一层修的都是"合法但错"的值，没有一层会触发 LLVM 报错。

**教训：`> N && s[:N] == ...` 这个惯用法里，N 同时出现在长度比较和切片里，是off-by-one 的标准温床——写`== "float"` 时长度条件自动变成`>= 5`，永远不会错，但一旦有人把 `>` 写成 `<` 或者把长度写成 `6`，它就变成恒假且静默。精确匹配（`==`）在这个场景总是更好的选择。**

## 小结：三类的误导性

这一章的 11 个坑，按"错误信息把你带偏的方向"分成三类：

**1. 报错位置是假的，根因在别处。** 坑 8（报在运算符上，实际是代码生成没做映射表）、坑 11（报在 `sdiv` 上，实际是一个返回 nil 的静态类型查询）、坑 13（报在初始化的元素个数上，实际是类型定义里多了一个成员）。**这类最费时间，因为 LLVM 的报错精确指向它读到的坏文本，而这个坏文本往往是下游产物。**

**2. 报错是假的，根本没有错。** 坑 9 的两条报错（"缺个声明"和"声明重复了"）互相矛盾且都是真的；坑 14 的 `expected '}'` 精确指向一个完全合法的裸 `{`；坑 15① 的 `trunc i32 to i32` 是自己转自己。这类要靠读文档或问oracle 才能解开——**光看报错永远解不出来**。

**3. 完全没有报错。** 坑 12（IR 合法、结果是 `double 0`）、坑 16（IR 合法、链接通过、运行时跳飞）、坑 17（IR 合法、语义完全错误地执行了别的分支）、坑 18（IR 合法、只是对齐弱一点）。**这类里LLVM 是个沉默的共犯。**

统一的应对方式有两条：一是**让类型信息自带**（坑 9 显式标注实参类型、坑 17 全量注册类型），二是**搭一个会拒绝东西的 oracle**（坑 14 的 `TestLLVMRejectsBadIR`）——**没有反向对照的测试，无法区分"生成正确"和"没人真的在看"。**
## 三、变参（va_list）：模型冲突

### 坑 19：`printf("%d", x)` 崩，`printf("hi")` 不崩

只读取变参的程序崩，不读变参的正常。这个"读不读"的分界线本身就是线索：崩不崩取决于**有没有人真的去动那个游标**，而不是取决于实参有几个、类型对不对。

根因是 **va_list 模型不匹配**，但**模型冲突发生在 Linux，不是 Win64**——这一点原文写反过，也是理解整章的前提。搞反了整章的修法就会反过来：以为 Win64 需要特殊照顾，于是给 Win64 加了一套并不需要的间接层，而 Linux 上的崩溃照旧。

要理解这个分歧，得看 goc 是怎么声明 `va_list` 的：`frontend/parser.go:172` 一行 `typedefs["va_list"] = PtrType(CharType())`——整个前端里 `va_list` 就是 `char *`，**一个普通的 8 字节指针typedef**。于是前端推理时它和一个任意指针不可区分，这个前端级简化直接决定了后端必须干什么。

而两个目标的 `llvm.va_start` 往这个槽里写的东西完全不是一回事（`expression.go:90-94` 的注释就是权威说明）：

- **Windows x64**：`llvm.va_start` 写入的就是**一个 8 字节指针**，指向调用方的寄存器保存区，每个变参占一个 8 字节槽（先通用寄存器，用完了才是栈上）。读一个参数就是"读游标 → 取值 → 游标 += 8"。**和 goc 的 `char*` 平坦游标本来就一致，不冲突**。落到 IR 上就是三行（`expression.go:132-156`）：`load ptr` 取游标、按类型 `load`、`getelementptr i8, i64 8` 再写回。窄类型也不额外处理——`i1` 先读成 `i8` 再 `trunc`，`float`/`double` 先读成 `i32`/`i64` 再 `bitcast`（`expression.go:138-153`），因为槽宽恒为 8 字节。
- **x86-64 SysV（Linux）**：`va_list` 是 `struct __va_list_tag[1]`，含 `gp_offset` / `fp_offset` / `overflow_arg_area` / `reg_save_area` 四个字段（`expression.go:96-101`）。而且整型和浮点住在**寄存器保存区的两个互不相干的一半**，各有自己的游标和自己的上限（6 个 GP 槽 = 48 字节，8 个 SSE 槽 = 128 字节），越过任一半的上限才改从 overflow 区取（`expression.go:96-101`、`179-180`）。如果按 Win64 的平坦游标去读 `gp_offset`，等于把"下一个通用寄存器槽的偏移"（一个 0/8/16…的小整数）当成指针本身去解引用——Linux 上每个消费变参的调用都会崩。

注意这两个方向的错误是**不对称**的（`expression.go:103-107`）：Win64 的形状（一个指针）被 SysV 的分半算术读起来是垃圾；反过来，SysV 的四字段被平坦游标读，第一个字段是个偏移量当指针用。都不是"能跑但结果错"，是直接崩。

最初把 `va_arg` 按平坦游标写，在 Win64 上其实是对的、在 Linux 上全错；两边共用一套前端展开代码，就必须在 `e.c.linux` 上分叉（`expression.go:127-129`：`if e.c.linux { return e.vaArgSysV(ap, lty, ty) }`）。SysV 那条分支的判据也很干净（`expression.go:186-190`）：`float`/`double` 走 SSE 半区（`fp_offset` 上限 176，注意 `float` 传参时提升为 `double`，所以它同样占一个 16 字节 SSE 槽的步长），其余类型走 GP 半区（上限 48），选中的那个游标前进。

而 LLVM 自己那条路也堵死了：`vaarg` 是**目标相关的指令**（`"vaarg %ap, i32"`）而不是调用，LLVM 23 直接拒收；旧的 intrinsic 写法 `"@llvm.va_arg(ptr, [i32, i8*])"` 同样被拒，报错是 `expected number in address space`（`expression.go:109-112`）。clang 同样是前端自行展开——所以只能按目标 ABI 在自己的前端里展开。顺带一个理由：自己拼出读取方式也让两个后端对"参数到底在哪"保持同一份说法（`expression.go:112-114`）。

Win64 崩溃的实际修复是 `5bbde19`（"LLVM Win64 chkstk stub contract + strLit NUL + **vaArg flat cursor**"）——commit 正文写的是"rewrite with flat cursor (single counter instead of in-register/memory split) so va_arg alignment arithmetic matches the actual ABI layout"。**把 Win64 明确走平坦游标，而不是反过来去迁就一个错误的模型**：既然 Win64 的 `llvm.va_start` 本来就只写一个指针，平坦游标就是它的原生模型，不需要额外包装。

> 教训：跨 ABI 的"通用抽象"几乎总是在**较简单的那一侧**成立。先问"哪个目标的 intrinsic 语义最贴近我的既有表示"，再决定统一到谁——而不是先抽象、再给不合的那个打补丁。

### 坑 20：`va_list` 局部量必须给 24 字节存储

`va_list` 的存储被统一加宽到 24 字节（`function.go:314` `const vaListTy = "[3 x i64]"`，`function.go:398` `alloca [3 x i64]`）。**但两条目标的理由完全不同**，混为一谈就会写出错误的"解释"：

- **SysV**：`llvm.va_start` 真的写满四字段结构，24 字节是**硬需求**，少一个字节都装不下。
- **Win64**：intrinsic 只写 8 字节，加宽是**防御性的**——`function.go:308-311` 的原话是"这个槽仍然加宽到 24 字节，这样 intrinsic 永远不会覆盖掉紧随其后的三个局部量，**无论未来的目标往里写什么**"。也就是说这条规则是按"最坏情况"写的，不是因为 Win64 需要。Win64 上 `vaListSlot` 对参数甚至直接 `slotFor(uid, "ptr")` 返回 8 字节槽（`function.go:351-356`，`if !e.c.linux { return slot }`），根本没加宽。

`function.go:312-313` 顺带点明了另一层：`char *` 这个 typedef **保持不变**——前端其余部分推理靠它，函数之间传 `va_list` 传的就是这个指针——**只有存储被加宽**，这正是真实 `stdarg.h` 在 `__builtin_va_list` 是数组类型时做的事。

第一版就栽在**一致性**上：`va_start` 写 `%t11`、实参求值读 `%t10`，而 `%t10` **从未写过**。这类 bug 最坏的性质是它不一定崩，可能只是安静地读出栈上的垃圾。修法是新增 `vaListSlot` / `vaSlots` / `paramNames` 三个字段，保证 `va_start`、每个 `va_arg`、`va_end`、`va_copy`、以及**把 ap 传给别的函数**时都落在同一个槽（调用点：`call.go:137`（`va_start`）、`call.go:145`（`va_end`）、`call.go:149-170`（`va_copy`，其中 `166-167` 取 dst/src 两个槽）、`call.go:205`（直接调用传参）、`call.go:360`（间接调用传参））。

这里还有一层**命名扫描**的必要性（`valist.go:5-29`）：因为前端把 `va_list` 定义成 `char *`，一个转发 `va_list` 的调用**光看类型是认不出来的**。所以要在 lowered 任何调用**之前**先扫一遍函数体，记下所有被当作 `va_list` 用的名字（`va_start`/`va_end`/`va_copy`/`va_arg` 的第一个操作数，`valist.go:133-139`、`144-151`）。必须"提前"扫，是因为 `va_list` 可能在给它做 `va_start` 的那次调用**之前**就被转发出去（`valist.go:16-24` 的例子：`log_it(fmt, ap)` 就是 `ap` 在求值顺序上的首次出现）。这个扫描是纯结构化遍历，漏了也不会引入新故障——未登记的名字退回 `char *` 读法，而那正是 Windows 的行为，也就是早就诊断过的老失败模式（`valist.go:26-29`）。

扫描里还有一处**按目标分岔**值得单说（`function.go:372-396`）：一个 `va_list` 局部量如果普通局部变量 machinery 已经给它分过槽（就是 `va_list m; va_copy(m, ap);` 里那个 `m`），要不要复用它，两边答案是**相反**的。Win64 下 `stdarg.h` 把 `va_copy` 定义成普通指针赋值，`m` 就是个寻常的 `char *` 局部量、一个槽、一次赋值——再给它分第二个槽，就会让 `va_arg(m, T)` 读到一个**和 `va_copy` 填过的那个不同的对象**，症状是"一个看起来很正常的垃圾数字"，而不是可诊断的故障（`function.go:374-381`）。SysV 下同样的复用则是**反方向**的错：那里 `va_copy` 是编译器 builtin，`m` 是货真价实的 24 字节 tag，声明分出的那个 8 字节槽只是它的**前三分之一**，从那儿读会丢掉 `overflow_arg_area` 和 `reg_save_area`（症状：第一个 `%d` 正常，其余全是垃圾）。所以 SysV 保留自己那个加宽槽（`function.go:383-389`）。

> 教训：一个"够用"的存储宽度不总是对的；但反过来，**按最坏目标定宽度、按当前目标省成本**这两件事必须分开记账，否则你会在注释里写下错误的理由，然后照着错误的理由做下一次修改。

### 坑 21：无优化管线时 Win64 变参丢浮点实参

`printf("%f", 1.25)` → `d=0.000000`。Win64 要求浮点实参**同时**进 XMM 与整数寄存器（各一份），无管线版本缺这段搬运，goc 的扁平 va_list 读不到。

这里有个反直觉的点，也是本章我最想记下来的：**`-O` 映射必须是"无 `-O` 也走 `default<O1>`"——这是正确性要求，不是性能选择**。`compile.go:126-142` 有完整论述：不加管线时 LLVM **照字面**降低调用，omits 掉 Win64 那条"传给变参函数的浮点实参要传两次"的规则，而正是这份重复让被调方的 `va_start` 能在寄存器保存区里找到值。goc 的 `va_list` 是扁平 8 字节游标、恰好覆盖那块区域，所以没有这份重复，交给 `printf` 的 `double` 就**静默丢失**。

证据是实测的（`compile.go:135-137`）：未优化的调用点**没有** `movq %xmm1,%rdx`，而 O1/Os/O2 全都有。所以"A literal, unoptimised translation is therefore not merely slow here, it is wrong"（`compile.go:139-140`）——对 goc 来说逐字直译的输出是**错的**，`-O0` 因此只能买最小的那条管线，而不是不买。而且**体积也不吃亏**（`compile.go:141-142`）：`bench/bench2.c` 的 `-O1` 构建是 16896 字节，未优化的是 19968 字节。

顺带修了个映射反转（`compile.go:122-124`）：`-O1` 原本映射到 `LLVMOptLess`，**比不带旗标的 `LLVMOptDefault` 还低**，等于"要求优化反而更慢"。这张表现在是（`compile.go:113-117`）：

```
opt 0  无 -O        default<O1>   ← 见上，正确性要求
opt 1  -O/-Og/-O1   default<O1>
opt 2  -Os/-Oz      default<O2>   ← 名字变了，见下
opt 3  -O2          default<O2>
opt 4  -O3/-Ofast   default<O3>
```

**再补一层，这一层原稿没写**：`-Os` 这条管线本身在 **LLVM 21 被移除**了——不是过时，是 libLLVM 会**直接拒绝**这个字符串（`compile.go:149-154`）：

```
The optimization level "Os" is no longer supported. Use O2 in
conjunction with the optsize attribute instead.
```

LLVM 把它换成了 O2 管线 + 每个函数上的 `optsize` 属性。所以 `opt == 2` 现在返回 `default<O2>`（`compile.go:157-158`），而**属性那一半最容易漏**——漏了会得到一个"构建成功但根本没按体积优化"的产物。因此另一半要单独发：属性由 `irMod.optSize` 逐函数发出（`compile.go:156`、`function.go:221-223` 写 `attributes #0 = { optsize }`，来源是 `translate.go:43` 的 `m.optSize = opt == 2`）。单测把**两半都锁住**了（`ir_test.go:358-378`）：管线字符串必须是 `default<O2>`（`ir_test.go:362-363`），且普通 `-O1` 构建的 IR 里**不许**出现 `optsize`（否则就是"为了谁也没要求省下的字节去换速度"）。

> 教训：当"不优化"在一个后端上等价于"不生成正确代码"时，优化等级就不再是性能旋钮。**先用那个会静默算错的案例把正确性钉死，再去谈它顺带省了多少字节。**

### 坑 22：跨平台 `va_list` 传递：Win64 按值传正确，SysV 必须按引用（**已按架构拆开**）

原文这条写的是"Win64 ABI 要求 `va_list*`，属已知限制"。**结论是反的，而且限制已经消除。** 根因还是坑 19 那条：前端把 `va_list` 定义成 `char *`，而两个目标里"参数槽里放的东西"根本不是一回事。

先说清失败的**症状特征**，这是它当初被判成"已知限制"的原因：`function.go:325-328` 写得很直白——按错模型传是**静默而非致命**的，被调方会顺着一条尾部是垃圾的 tag 往下走，于是"第一个参数能打印，后面的全没了"。这种"部分正确"比崩更难查，也更容易被误判成"大概是别的地方坏了"。

- **Windows x64**：`va_list` 就是 `char *`，参数里装的就是游标本身，按值传**正是 ABI 要求**。`function.go:330-332` 的原话："`vaListSlot` 绑出来的槽已经是 8 字节宽、已经装了正确的值，**所以它的地址就是答案**"。`valist.go:11-13` 补充了这层为什么无害："在 Windows x64 上这个歧义无害——`va_list` 的值**就是**游标指针，正好等于读一个 `char *` 会得到的东西。"所以 Win64 分支直接返回 `slotFor(uid, "ptr")` 的 8 字节槽地址（`function.go:353-356`），**不做任何 load**。
- **x86-64 SysV（Linux）**：`va_list` 是 `struct __va_list_tag[1]`，**数组类型**，作为参数会衰变成指向 tag 的指针，且这 24 字节住在**调用者**的栈帧里（`function.go:334-341`）。把上面那个 8 字节槽的地址交给 `llvm.va_arg`，会让每一次越过前 8 字节的读取都变成垃圾，而且**推进后的游标被写回本地副本**，调用者的 tag 纹丝不动——于是被调方永远从头读。修法是**先 `load ptr` 把指针取出来**（`function.go:357-362`）：`load ptr, ptr %s, align 8` 之后再用，`IT` 才是那个 `va_list` 的地址，这样写回才落在 C 语义要求的调用者那份 list 里。

所以 goclib 的 `printf_lite_with(..., va_list ap)` 按值传这个签名（`stdio.c:526`）**在两个目标上都是对的**，不需要改 C 代码。要改的是后端：Win64 分支直接返回已含正确游标的槽地址，Linux 分支才做 `load ptr`。跨函数传递的两条路径都覆盖了——直接调用 `call.go:204-205`，间接调用 `call.go:358-362`（后者同样只在 `if e.c.linux` 下走 `vaListSlot`，注释也指回 `callExpr` 说明同因）。注意两处都是 `ptr` + **槽地址**而不是值：Win64 传地址是因为该地址里已经装着正确游标，SysV 传地址是因为它**本身就是**指向调用方 tag 的指针。来源 commit 是 `7f22b64`「feat(gocl): Linux ELF 后端 —— SysV va_list ABI + ELF 目标输出」。

这条限制消除之后，`va_list` 才真正成为**可以跨越库边界传递的一等公民**——goclib 内部那些 `vfmt` / `printf_lite_with` / `vfprintf` 全都是 `va_list` 形参（`stdio.c:25`、`526`、`926`、`974`），它们在两个目标上都不需要为对方准备不同的 C 代码。

> 附带一个当时没做的：`va_copy`（C99 7.16.1.1）现在也实现了（`call.go:149-170`）——它让 `va_list` 能独立复制、先量长度再输出而不消耗原件。`va_copy` 是唯一一个**降级方式本身按 ABI 分岔**的操作，所以它也是唯一一个在 `stdarg.h` 里**故意不定义成宏**的（`stdarg.h:22-38`）：Win64 下 `va_list` 就是一个 `char*` 游标，复制它是指针赋值，`stdarg.h:40-43` 就直接 `#define va_copy(dest, src) ((dest) = (src))`；Linux 下则**故意留空**，让名字原样进 codegen，落到 target-aware 的 `llvm.va_copy` intrinsic（`call.go:168-169`）——它在 SysV 上复制整个 24 字节对象、在 Win64 上只复制那一个指针，同一个 intrinsic 两侧都服务，**前端一个编译期分支都不需要**（`call.go:150-165`）。只复制前 8 字节的代价也说得很具体：副本会继续**共享**原对象的寄存器保存区，而 `overflow_arg_area` 是空的，于是第一个栈上实参就解引用空指针（`call.go:155-159`、`stdarg.h:27-33`）。这也让 `stdio.c:513-520` 里那句"显式声明 + `va_copy`，而不是 `va_list m = ap;`"站得住——后者在 SysV 上是数组类型初始化，直接被拒（`array initializer must be an initializer list`），就算能编译也只是**共享一个游标**而不是复制。

> 教训：把"目标 A 上碰巧成立"记成"目标 B 的限制"，会让你在错误的地方写 TODO、在错误的地方花力气。先把两个 ABI 的**实参槽里到底放了什么**逐字节对上，再决定后端要不要动。跨 ABI 移植里最贵的 bug 从来不是崩，是"一个平台上正确、另一个平台上只是碰巧对"。

## 四、COFF / PE：读错不报错的重灾区

这一节是全部坑里最值得写的——COFF/PE 里"读错字段不会报错、只是结果不对"的地方特别多。

为什么这一节最危险：ELF 那边的格式错位往往会在解析阶段就炸出来——段偏移越界、符号索引越界、`machine` 不对，`parseCOFF` 一进门就能把你拦下（`coff.go:129-139`：太短、MZ 开头、machine 不是 `0x8664` 三种情况直接返回错误）。而 COFF 的字段布局是**定长、无校验、无内部一致性检查**的：符号表里少读一条 aux，索引不会越界，只会让后面每个引用落到错误的符号上；重定位的 Type 读成 dword，不会越界，只会让符号索引变成 `0x240004` 这样的垃圾值，然后照样写进镜像。

所以本章的十条坑共享同一个失败模式：**没有任何一层会告诉你读错了**。校验必须来自源码注释和构造性推理，而不是来自报错。

本章引用的权威依据是 `src/gocld/coff.go:13-24` 的文件头注释。它开头就写着 "measured on LLVM 23.1.2, **not guessed**"，然后逐条列出 6 个段、恰好 2 类重定位、undefined 符号、每份 unwind 贡献一个 COMDAT 组、`@feat.00`。**写这一节时我反复回到这段注释**——它比任何一次实测输出都可靠，因为它把测量结论固化在代码里了，而且和坑 32 的实测数字能互相印证。

### 坑 23：COFF 符号名判断反了 —— 一个符号都解析不出来

**现象**：链接阶段一个符号都解析不出来，或者符号名变成 `?~0?~0?~0` 之类的垃圾；但**解析过程本身不报任何错**，段也照样建出来了。

**为什么会错**：COFF 的 8 字节 name 字段是一个**联合体**，两种形态共用同一段字节：

- **首 4 字节非 0** → 名字是**内联文本**，左对齐、NUL 补齐，8 字节以内装下。
- **首 4 字节为 0** → 后 4 字节是**字符串表偏移**，真正的名字在字符串表里。

原写法是"非 0 = 偏移"，判断整个反了。这个错误的特性是：读完照样得到一个 4 字节的整数，加到字符串表基址上照样落在表内某个位置（字符串表里全是名字，任意偏移读出来仍是**某个合法名字**），于是不会越界、不会报错——**只是每一个符号都指向了无关的名字**。

**根因 + 文件:行号**：正确的判定在 `coff.go:250-258` 的 `readName` 闭包里：

```go
readName := func(rec int) string {
    if binary.LittleEndian.Uint32(src[rec:rec+4]) == 0 {
        if s, ok := coffStringAt(coffStrTab, coffStrBase+rd32(src, rec+4), -1); ok {
            return s
        }
        return ""
    }
    return strings.TrimRight(string(src[rec:rec+8]), "\x00")
}
```

`:244-249` 的注释把理由写全了："名字靠**首 dword 单独**区分两种情况，从错误的半边读偏移就是丢符号的原因。"

段名判断是**同构但不同形**的一件事：段名的 8 字节字段不用"首 dword 为 0"作判据，而是用**斜杠前缀**——名字太长装不下时写成 `/4`、`/24`，斜杠后是十进制偏移。解析在 `coffSectionName`（`coff.go:357-374`），调用点 `coff.go:170`。`coff.go:164-169` 的注释指出这有多阴："LLVM 对 `.rdata` 和 COMDAT 成员发长形式，把它当字面文本读会得到一个叫 `/4` 的段——**它匹配不上任何映射，于是以一个没人引用的名字落进镜像**。"注意后果不只是名字难看：段的归属错了，它的字节会被并进错误的 goa 段或直接被丢弃。

**修复**：`coff.go:251` 用首 dword 是否为 0 作唯一判据，为 0 才走字符串表；段名走 `strings.HasPrefix(raw, "/")`（`coff.go:359`），然后逐字符累出十进制偏移（`coff.go:361-366`）。两条路径都以同一个 `coffStrBase` 为基准——因为 `/N` 的 N 是**从字符串表自己的长度 dword 起算**的（`coff.go:151-155`、`coff.go:240-242`），不是从符号表末尾起算的。

**验证与教训**：写单元测试时最容易犯的错是只测"名字长"的符号（走字符串表分支），结果内联分支从未被验证——而 LLVM 生成的短名符号（`.text`、`.data`）全走内联分支。**教训**：当一个字段有两种形态时，测试必须覆盖两种；更普适的是，二值判据的两个分支要**用不同触发条件的输入各测一次**，否则你验证的可能只是"恰好走了对的那条路"。

顺带一个读代码时发现的细节：字符串表基址在文件里算了两遍。`coff.go:155` 的 `coffStrBase := symTableOff + 18*nSyms` **写死了 18**（此时还没读 `SizeOfOptionalHeader`，不知道是不是 bigobj），而 `coff.go:233` 的 `strBase := symTableOff + recSize*nSyms` 用的是实际记录宽度。`readName`（`:252`）和 bigobj 的 flags 路径（`:278`）喂的是前者。classic COFF 下两者相等，所以现在不暴露——但这正是坑 24 末尾要合并进来的 bigobj 会撬动的那块地板。

### 坑 24：符号表必须索引对齐 —— aux 记录也占一个符号槽位

**现象**：重定位引用的索引整体前移，指向错误符号。链接成功、程序能跑，但调用的不是它想调用的函数。**没有报错，甚至没有崩溃**——只是行为诡异。

**为什么会错**：COFF 的符号表是一个**定长记录数组**，重定位里的 `SymbolTableIndex` 是这个数组的**裸下标**。问题是这个数组里不只有符号：每个记录后面可以跟 0 到 N 个 **auxiliary record（辅助记录）**，它们**占满自己的槽位**，索引空间里也有它们的位置。aux 记录没有可用的名字和地址（内容是段定义、`.bf`/`.ef` 帧信息、COMDAT 成员名等），但**索引必须被占用**。

所以解析时如果只前进 1（只记符号、跳过 aux），后面每一个符号的下标都会比文件里的实际下标小——**偏移是累加的，越靠后偏得越多**。

**根因 + 文件:行号**：`coff.go:311-318`：

```go
// Aux records occupy their own slots in the table: a relocation may
// name one, and an index that skips them would shift every later
// reference onto the wrong symbol. They carry no usable name or address,
// so they are recorded as empty placeholders.
for k := 0; k < nAux; k++ {
    o.syms = append(o.syms, coffSym{})
}
i += 1 + nAux
```

`coff.go:86-90` 在类型定义处就把这条约束写死了："重定位记录按**裸表下标**引用符号，所以表必须保持索引对齐：**丢掉条目会静默地把后面每个引用挪到错误的符号上。**"下游的取用端因此可以直接按索引寻址，不用再过滤——`symbolAt`（`coffmerge.go:865-872`）注释："符号表按原始顺序存储，**aux 记录也在内**，所以重定位的符号索引可以直接寻址。"

量化过：`nsym=23` 但只有 16 个非 aux 条目，**差 7 个** = 6 个段符号的 aux + 1 个 `.file` 伪符号的 aux。`i += 1 + nAux` 正是让这 23 个槽位对上文件里 23 条记录的地方。

**修复**：aux 记成空占位符 `coffSym{}`（`coff.go:316`），游标推进 `1 + nAux`（`coff.go:318`）。另外两处配套的跳过条件也是这个"索引要站得住"的一部分：`coffmerge.go:374-381` 跳过 `secNum < 0`（段符号 `-1` = absolute，如 `@feat.00`；文件记录 `-2`）和 `scnClassFile`（`coff.go:57` 定义值为 103）——这些符号不需要镜像地址，但它们**仍然占着槽位**。

**验证与教训**：`UndefinedSymbols`（`coff.go:330-352`）和合并路径用的是**同一套**门控（`coff.go:341`、`coffmerge.go:379`），注释里明说"两边绝不能对'什么算 undefined'产生分歧"。**教训**：当一份数据被两个消费者共用时，把判定逻辑写成两处必然漂移；正确的做法是让它们引用同一个谓词，并在注释里点明这个约束。索引类数据尤其如此——**下标空间是共享资源，占位规则必须是全局唯一的**。

> **bigobj：这条坑的另一半，原稿没提，必须合看。**
>
> **现象**：aux 占位做对了，记录宽度还是错的。符号值变成巨大的偏移，段号指向不存在的段——**但依然不报错**。
>
> **为什么会错**：前面整条坑默认了"符号记录是 18 字节"这个前提。而这个前提本身就是会变的。COFF 有两种符号记录宽度：
>
> ```
> classic: [8]name  [4]value [2]section [2]type [1]class [1]naux
> bigobj:  [4]flags [8]name  [4]value [2]section [2]type [4]class+naux
> ```
>
> bigobj 在名字前面多了一个 4 字节 flags，**总宽 20 而非 18**，而且 `StorageClass` 与 `NumberOfAuxSymbols` **挤在同一个 4 字节字段里**（低字节是 class，高字节是 naux）。
>
> **根因 + 文件:行号**：判据在 `coff.go:213-225`——`SizeOfOptionalHeader == 0x20`（`IMAGE_NT_OPTIONAL_HDR32_MAGIC`）表示 bigobj，classic COFF 是 0，其他非零值直接报错（`coff.go:217-221`）。读取分支在 `coff.go:271-291`：`cls = src[rec+18]; nAux = int(src[rec+19])`（`:290-291`，`coff.go:289` 的注释点明"StorageClass 和 NumberOfAuxSymbols 共享一个 4 字节字段"）。**为什么必须从头部判断而不能靠数据猜**，`coff.go:208-212` 说得非常准："按 18 字节读一个 bigobj 表会产出**看起来合理的垃圾（巨大段偏移、胡言乱语的值）而不是一个错误**，所以格式必须来自头部。"——这一句就是本章标题的最好注解。
>
> **修复**：`recSize` 由头部决定（`coff.go:222-225`），并且它同时影响**符号表末尾（也就是字符串表起点）的计算**——`coff.go:233` 的 `strBase := symTableOff + recSize*nSyms`。用 18 算，bigobj 的字符串表起点会前移 `2*nSyms` 字节，后面每一个长名字都读错。
>
> **验证与教训**：`coff.go:201-203` 说明了为什么这不是罕见路径——**LLVM 对 Windows 目标默认就发 bigobj**，因为一个模块可以超过 classic 的 65,535 符号上限。**教训**：判断二进制格式不能靠"读出来的数看着对不对"，必须找一个**头部里的权威标志位**；当格式有多个变体时，把变体信息固化在文件头解析里，并在读取前就定好记录宽度，而不是读到一半再回头改。
>
> 合起来看这两条：坑 24 说"记录**条数**要对（含占位）"，bigobj 说"记录**宽度**要对"。它们是同一个数组的两个独立维度，只修一个，另一个照样静默出错。

### 坑 25：`IMAGE_RELOCATION` 字段顺序是 `{VirtualAddress(4), SymbolTableIndex(4), Type(2)}`

**现象**：段内偏移看着正常（甚至完全正确），但符号索引是 `0x240004` 这样的垃圾值。链接完成、镜像生成，运行时跳到不该跳的地方。

**为什么会错**：`IMAGE_RELOCATION` 是 10 字节定长记录，字段顺序是 `{VirtualAddress(4), SymbolTableIndex(4), Type(2)}`——**符号索引夹在偏移和类型中间**。如果按 `(off, type, sym)` 的自然顺序去读，第二段的 4 字节会同时吃掉真正的 Type（低 2 字节）和符号索引的高 2 字节，拼出一个看似合理、实则完全错误的索引。

更隐蔽的是 **Type 只有 2 字节**。用 `rd32` 读它，会**吞掉下一条记录的前 2 字节**（也就是下一条的 `VirtualAddress` 的低半），于是下一条的偏移也一起错了——**一个错误污染两条记录**。

**根因 + 文件:行号**：`coffmerge.go:549-555`：

```go
// IMAGE_RELOCATION is {VirtualAddress(4), SymbolTableIndex(4),
// Type(2)} -- the symbol index sits BETWEEN the offset and the type,
// not after it. Type is a WORD: reading it as a dword swallows the
// first two bytes of the next record and yields a nonsense value.
off := rd32(src, rec)
symIdx := rd32(src, rec+4)
typ := rd16(src, rec+8)
```

记录步长是 `cs.relOff + 10*r`（`coffmerge.go:545`），边界检查 `rec+10 > len(src)`（`coffmerge.go:546-548`）——注意这个检查只能保证**不越界**，保证不了**不错位**。这正是本章的核心：越界检查在这里全部形同虚设，因为读错的位置仍然在文件内部。

配套的两个读取器在 `coff.go:93-105`：`rd16` 返回 2 字节，`rd32` 返回**符号扩展**后的 4 字节。`rd32` 的符号扩展在段内偏移这种无符号量上是错的，所以另有一个 `rdu32`（`coff.go:119-124`），`coff.go:107-118` 的注释说明了原因："rd32 会符号扩展，这对**持有符号值**的字段是对的，对**最高位是标志位**的字段是错的——资源树的 Name/Data 字段都拿 bit 31 当判别位。"段特征字读的正是 `rdu32`（`coff.go:175`）。

**修复**：严格按 `{4,4,2}` 分三次读，Type 用 `rd16`。解析出的 `symIdx` 直接交给 `symbolAt`（`coffmerge.go:556`），越界时它的错误信息里带着表长（`coffmerge.go:868-870`）——但如前所述，**读错下标时根本不会走到这个越界分支**，只会安安静静地取出一个错的符号。

**验证与教训**：`coffmerge.go:692-694` 的 `default` 分支是这一族的最后一道网：

```go
return fmt.Errorf("coff: unknown relocation type %d for %s (%s)", typ, cs.name, sym.name)
```

注意它报的 `typ` 是**已经按 2 字节正确解出来的**类型——**不是**错读 dword 得到的那个值。这是有意的：接受 5 种已知类型（`coff.go:46-50`：`ABS`/`ADDR64`/`ADDR32`/`ADDR32NB`/`REL32`），其余报错，而 `coff.go:17-20` 的文件头注释说明了取舍标准——"**其他类型是被拒绝的，而不是被静默链接错**"。**教训**：在一个"错读不报错"的格式里，**穷举合法取值 + 显式拒绝其余**是把静默失败转成响亮失败的最划算手段；反过来，"猜一个最可能的宽度"就是把错误藏起来的最好办法。

### 坑 26：重定位语义换算（goa fixup ↔ COFF）

原文这两条 `ripAdj` 数值都是错的（写成了 `ripAdj = -4` / `-(off+4)`），而且"不需要改 `applyFixup`"这句也不对——**实际改了**，新增了 `Absolute` 字段。以 `coffmerge.go` 为准。

**现象**：改完符号解析、段布局都对了，程序仍然跑飞。属于"IR 全对、镜像全对、运行时地址算错"这一类。

**为什么会错**：goa 的 fixup 是相对算法，COFF 的 REL32 也是相对算法，但两者对 **P（参与减法的那个地址）** 的定义不在同一个位置。goa 的公式以**字段末尾**为基准，COFF 规范以**字段开头**为基准。差 4 字节，而 4 字节的偏移误差落在 RIP 相对寻址上，位移是"合法"的，指令照常执行，只是**指向了目标的旁边 4 字节**。

**根因 + 文件:行号**：三条路径，行为各不相同。

**① COFF `REL32` → `RipAdjust` 保持 0。**推导写在 `coffmerge.go:607-613`：

> IMAGE_REL_AMD64_REL32 是 `S + A - P`，但**微软的 PE 规范把这个类型的 P 定义为字段的地址**，而硬件执行完指令后的 RIP 是**字段的末尾**——所以照字面读 "S - P" 会落到目标之后 4 字节。`ripAdj = 0` 让 goa 的 `target - (base + off + size)` 正好等于 CPU 算出的值。

goa 那边的公式是 `disp := int32(target + f.Addend - (base + f.Off + size + f.RipAdjust))`（`fixup.go:60`）。`RipAdjust` 的定义在 `image.go:79-82`：字段末尾之后、下一条指令开始之前还要补多少字节——它是**为 AT&T 路径的指令尾部（trailing imm 等）**设计的，不是为 COFF REL32 建的。`git log -S"RipAdjust" -- src/gocld/` 只有 `fbc0b42`、`7f22b64`、`407154c` 三次，都不在 COFF 路径上，也印证了这一点。

**② COFF `ADDR32NB` → 新增的 `Absolute` 字段。**`coffmerge.go:679-682`：

```go
img.Fixups = append(img.Fixups, Fixup{
    Sect: sectOf[si], Off: at, Sym: key,
    Absolute: true, Addend: addend,
})
```

它要的是目标的**绝对 RVA**，P 直接消掉，根本不是相对算法——所以讨论 ripAdj 没有意义。`Absolute` 是 `Fixup` 的新字段（`image.go:83-85`），而且 **`applyFixup` 确实被改过**：`fixup.go:33-47` 新增了独立分支，`addr := uint64(target + f.Addend)` 直接写绝对值。注意 `fixup.go:28-30` 的越界检查在这个分支之前已经执行过，所以 `:34-36` 那次重复检查是多余的防御。

**③ 第三种：`ADDR64`。**`relAMD64Addr64 = 0x0001`（`coffmerge.go:47`），走 `Absolute + Wide + Virtual`（`coffmerge.go:683-691`）。`fixup.go:38-42` 为它加上 `ImageBase`：

```go
if f.Virtual {
    // The preferred load address is above 4GB, so a 32-bit field
    // would truncate it; only the 64-bit form can hold one.
    addr += uint64(ImageBase)
}
```

`ImageBase = 0x140000000`（`pe.go:24`）确实超过 4GB，32 位字段装不下。`Wide` 让字段宽度变成 8 字节（`fixup.go:25-27`），`Virtual` 决定加基址——`image.go:61-64` 的注释说明这两个布尔不是独立的，"wide 和 virtual 只有一起才有意义，而 applyFixup 就是强制这一点的地方"。

**所以链接器实际支持 3 种重定位，不是坑 32 说的 2 种**（那是**实测那个对象**的结论）。三个 case 加一个 `default` 拒绝分支在 `coffmerge.go:603-695`。

另外重定位 offset 是**"COFF 段内偏移"**，必须加上该段在 goa 段内的 `baseOf`：`at := baseOf[si] + off`（`coffmerge.go:602`）。`coffmerge.go:595-601` 的注释说得很清楚："忘记这个偏置会让**每一个补丁都提前几十字节**，污染的是无关代码而不是大声失败。"

> **一个值得记的读代码收获**：`coffmerge.go` 的**文件头注释 `:24-33` 至今仍写着旧模型**——"So ripAdj = -size makes the two identical"、ADDR32NB 是 "ripAdj = -(off+size)"。这两句与 `:607-613` 和 `:679-682` 的实际代码**直接矛盾**。修复只改了实现，没回头改文件头。
>
> 这恰好是本章主题的另一个版本：**错误的注释比没有注释更危险**，因为读代码的人会优先相信它。`coff.go:13-14` 的 "measured on LLVM 23.1.2, **not guessed**" 之所以被我当作本章的权威依据，就是因为它明确标注了自己的可信度来源——而 `:24-33` 没有任何这样的标注。
>
> **教训**：改语义的时候，把文件头当作必须同步修改的代码。文件头注释是最容易被跳过的，因为它不影响编译。

**验证与教训**：三种重定位的区分点是**"目标语义是距离还是地址"**，不是字段宽度——`fixup.go:5-10` 的文件头注释把这句讲透了："每个形状都写开来，而不是从字段宽度推导，因为**宽度不足以区分它们**——下一条指令的 4 字节位移和一个 4 字节地址，只差一个是否加镜像基址。"**教训**：当多个分支共享同一个表象（都是"改 4 个字节"）时，判据必须是**语义**而不是**尺寸**；把语义差异显式编码成一个字段（这里是 `Absolute` / `Wide` / `Virtual`），比在调用点靠 `if` 猜要可靠得多。

### 坑 27：COFF REL32 分支没读 addend —— 控制台输出全哑、文件写正常

这是最值得写的一条"IR 全对但运行时结果错"。它也是本章"静默"二字最纯粹的样本：**编译成功、链接成功、镜像合法、符号全对**，只是每一个带偏移的数据访问都落在了目标的基址上。

**现象**：LLVM 后端下**所有控制台输出全丢**（puts / printf / putchar / fprintf(stderr)），显式 `fflush`、写 2000 字节、甚至 `> file` 重定向都没输出；但 **fopen + fwrite + fclose 完全正常**（out.txt 内容正确）。细节：`putchar` 返回 EOF 且置 `_err`；`fwrite` 返回 0 且不置错。

用户代码直接 `GetStdHandle(-11)` + `WriteFile` 能正常打印——**这一条极有价值**：它证明 Win32 API 调用链本身是通的，坏的是"经由镜像内 FILE 结构的数据访问"。

**定位三件套**：

1. **运行时探针**：镜像 FILE 布局写 `struct F{...}`，强转 `stdout`，把字段用 exit code 传出。结果 **native 位图 = 51**（fd+writable+base+off==-1），**LLVM 侧 = 1**（只有 fd 非 0）→ `_writable/_base/_size/_off` 全是 0。完美解释症状：`fwrite` 因 `!f->_writable` 直接 return 0（不置错），`fputc` 同样短路并置 `_err`。而**文件 I/O 走 heap FILE 逐字段赋值**——它不需要"通过 RIP 相对位移访问一个全局的成员"，所以完全不受影响。
2. **`objdump -d -r t1.obj`** 看 addend，**`objdump -d t1.exe`** 看落点。
3. **落点对照表**：偏移 0 的引用全对（`stdin_file` / `stdout_file` / `out_buf`），**带偏移的全错**（`stdin_file+32` → 落到 `stdin_file+0`）。

**为什么会错 —— 为什么这个症状指向 addend 而不是符号解析**：这是本条坑的推理核心，值得单独拆开。如果符号解析错了，事情应该长成"**所有**引用都错"，因为符号的 RVA 本身就会是错的。但实测是**分裂**的：`stdin_file+0` / `stdout_file+0` / `out_buf` 这些**零偏移**引用全部落在正确位置，入口桩的 `call main` 也一直正常。这说明**符号的地址算对了**，错的只是"符号地址 + 常数"这个加法里的**常数**。而 COFF 的重定位条目里根本没有 addend 字段——

> **COFF 重定位记录没有 addend 字段，A 就存在待修补字段自己的未重定位字节里。**

所以必须 `rd32(cs.data, off)` 在补丁覆盖它之前把它读出来。

**根因 + 文件:行号**：`coffmerge.go:645-648` 就是修复后的样子，旁边 `coffmerge.go:614-627` 的注释把整件事写全了：

> A 在这里是**必须的**，丢掉它是静默损坏而不是可见错误。COFF 重定位记录没有 addend 字段：**A 活在字段自己的未重定位字节里**，所以必须在补丁覆盖它之前从段里读出来。LLVM 对全局的每个 RIP 相对成员访问都这么用——`mov %rax, stdout_file+32(%rip)` 携 A=32，`movq $1, stdin_file+8(%rip)` 携 A=4（尾部 imm32 已经折进去了）——**而 call 携 A=0**。留下 A 缺失会让每一个这样的 store 落在符号的基址而不是成员上：stdio 初始化器于是把 `_writable/_base/_size/_off` 直接写到 `_fd` 上面，于是 FILE 起来的 `_writable == 0`，**每一次 stdio 写都返回错误而控制台上什么都没有**——而走 heap FILE 逐字段设置的文件 I/O 照常工作。

**"为什么偏偏是它没事"是定位的关键线索**：`call` 携 A=0，所以 `addend := rd32(...)` 读到 0 恰好等于正确值。入口桩的 `call main`、`call puts` 全都是 A=0，于是**入口桩一直是好的**——bug 精确地绕开了唯一那个"如果这里也坏了就立刻定位到"的路径。`coffmerge.go:629-637` 还解释了另一条为什么 A=0 的原因：REL32 的字段若目标是导入，opcode 必须是 `E8`/`E9`（call/jmp），因为 `call` 的编码天然不带偏移。

goa 侧的公式是 `target + f.Addend - (base + off + size + RipAdjust)`（`fixup.go:60`），把 COFF 的 `S + A - P` 逐项对齐后：**addend = 字段值、RipAdjust = 0**，不需要额外的 trailing 补偿（因为 A 里已经含了）。这也说明为什么 `RipAdjust` 在 COFF 路径上恒为 0 是自洽的，而不是"忘了设"。

**修复**：`coffmerge.go:645` 读出 addend，`:646-648` 传进 `Fixup`。同时 `coffmerge.go:638-644` 加了导入 thunk 的重定向：

```go
if off > 0 && off <= len(cs.data) {
    if op := cs.data[off-1]; op == 0xE8 || op == 0xE9 {
        if _, isImport := img.Exts[sym.name]; isImport {
            key = "thunk:" + sym.name
        }
    }
}
```

注意它检查的是 `cs.data[off-1]`——**REL32 字段紧跟在 opcode 之后**，所以字段往前一个字节就是 opcode。这是个很省的办法：不用认识指令编码，只要 A=0 的 call/jmp 形态对上了就行。

**验证与教训**：`-fllvm -S` 落 `.s` + `objdump -d -r` 是最快的定位路径。**教训**：现象"只有控制台写失败、文件写成功"很容易误导向 FILE 层/缓冲逻辑（第一版就误判成"goclib stdio 的 `_pos` 维护"）。判断依据是**症状的分裂性**：
- 如果坏的是**状态维护**，两种 I/O 会**一起**坏；
- 如果坏的是**地址计算**，则按访问形态分层——**零偏移的引用是对的、带偏移的引用是错的**，且走完全不同代码路径的调用方（heap FILE vs 全局 FILE）表现不同。

**分裂的症状指向地址计算，不指向业务逻辑。** 更一般地说：一个 bug 若只在部分输入上出现，先别怀疑被测逻辑，先把"好的那批"和"坏的那批"之间**唯一的结构差异**找出来——那就是变量。

### 坑 28：`.bss` 段没有文件字节，必须用 `vsize` 推进游标

**现象**：`.bss` 里定义的全局 `counter` 的地址，和 `.pdata`（或紧邻的下一个段）里的东西撞在一起。运行时访问冲突，或者计数器莫名其妙被 unwind 表改写。

**为什么会错**：COFF 的每个段有两组尺寸：**文件里实际占的字节**（`SizeOfRawData` / `PointerToRawData`）和**它在内存里要的虚空间**（`VirtualSize`）。绝大多数段这两个相等。`.bss` 例外：它只有虚空间，**一个文件字节都没有**。

`coff.go:178-186` 判定得很宽——两个条件任一成立即算 `.bss`：`IMAGE_SCN_CNT_UNINITIALIZED_DATA (0x80)` 位置了，或 `PointerToRawData == 0`。`:187-192` 随后就**根本不读它的数据**。所以 `.bss` 的 `sec.data` 长度是 0。

如果游标推进写成 `gs.VSize += len(cs.data)`，`.bss` 这一段的游标**停在 0**。接下来：`BuildPE` 看到 `len(Data) == 0` 认为这个段没有内容 → **整个段被跳过** → 段里的符号解析到**下一个段拿到的地址**上。

**根因 + 文件:行号**：`coffmerge.go:302-308`：

```go
if mapped.bss {
    // An uninitialised section has no file bytes; its size is virtual.
    // Advancing only by len(data) would leave the cursor at zero, the
    // image builder would skip the section entirely, and the symbols in
    // it would resolve to whatever address the NEXT section got -- which
    // is how a .bss counter ends up aliasing the unwind table.
    gs.VSize += cs.vsize
}
```

符号落位是 `img.Syms[s.name] = SymLoc{Sect: sectOf[si], Off: baseOf[si] + int(s.value)}`（`coffmerge.go:501`）——`baseOf[si]` 来自 `coffmerge.go:301` 的 `baseOf[i+1] = gs.VSize`。**游标停在 0，`baseOf` 就是 0，段内偏移照样加，于是符号地址等于那个"下一个段"的基址。** 这是典型的"不报错的静默错位"。

**修复**：区分两个尺寸——非 `.bss` 用 `len(cs.data)`（`coffmerge.go:310-313`，且有 `if len(cs.data) > 0` 的守卫），`.bss` 用 `cs.vsize`。`Section.Bss` 的语义在 `image.go:36-38` 说明："写到内存，从不写进文件，**它的尺寸是 VSize（不是 len(Data)，后者保持 0）**。"

对齐填充也要一并正确——`padSection`（`coffmerge.go:885-893`）在 `.bss` 上**只推进游标不写零字节**，这正是 `.bss` 需要的语义。

**验证与教训**：这条坑和坑 27 共享一个失败模式：**游标推进必须精确，且推进量必须来自正确的字段**。坑 27 里游标多推了 4 字节（thunk 多了个前导字节），这里少推了整段。两者都不会报错。

**教训**：在"把一段字节拼进一个大缓冲"的循环里，**游标推进量必须来自该段的元数据，不能来自你手边恰好有的那个切片长度**。`len(data)` 看起来是自然的写法，实际上是在问"文件里有多少字节"，而这里需要问的是"它要占多少地址"。这两个问题在**绝大多数段**上答案相同——正因如此，写错时不会有任何测试报警，直到第一个 `.bss` 出现。

### 坑 29：`.pdata` / `.xdata` 曾合并进 merged blob，现改为**合并但不映射**（**方案已反转**）

坑因是对的：unwind 表项存的是"**相对自己段起点的 RVA**"，搬进共享 blob 会让每一条都失效。

**为什么会错**：Win64 的 `RUNTIME_FUNCTION` 三元组（`BeginAddress`, `EndAddress`, `UnwindData`）和 unwind code 都编码成**相对于各自段起点的偏移**，不是镜像绝对 RVA（也不是文件偏移）。段名 `.pdata` / `.xdata` 的内容一旦和别的段拼接，段起点就变了，表里每一个偏移的含义都变了。而且**它是静默的**：loader 拿到一个格式完全合法的异常目录，只是回溯时算出荒谬的地址。

**第一版修法**：`planUnwindSections`（`pe.go:70`）/ `unwindSectionOut`（`pe.go:101`）/ `imageEndOf`（`pe.go:50`）规划独立段，异常目录（data directory index 3）写 `pdataRVA / pdataSize`（`pe.go:512-518`），`Fixup` 加 `absolute` 字段（ADDR32NB 要写绝对 RVA，不能走"target − 字段位置"的相对算法）。**这些函数和字段今天都还在。**

**但这个方案已经被推翻。** 现在 `coffmerge.go:279-281` 给这两个段打了 `Unmapped = true`：

```go
if name == ".xdata" || name == ".pdata" {
    gs.Unmapped = true
}
```

`Unmapped` 的语义在 `image.go:40-42`："标记一个**字节会被合并、符号和重定位正常解析**，但**不进最终镜像**的段。"

于是：`pe.go:78-83` 把 Unmapped 段置 nil → `planUnwindSections` 不给它们分配地址 → `img.PdataSize` 停在 `0, 0`（`pe.go:72` 的初始化）→ `pe.go:512` 的 `if img.PdataSize > 0` 不成立，**异常目录压根不写**。

注意 `coffmerge.go:275-278` 的措辞很讲究：这两个段**仍然会被合并**——"符号会解析、重定位会应用，所以什么都不悬着"——**只是字节不进镜像**。这是"正确性"和"体积"两个关注点被显式拆开的写法。

**根因 + 文件:行号 —— 为什么反悔**（`coff.go:26-34` 的原话）：表很小（每个函数 12 字节 `.pdata` + 约 11 字节 `.xdata`），但 PE 的一个段在文件里要占 `FileAlignment` 的**整数倍**，而 **512 是 Windows 接受的最小值**——三个函数就是 1024 字节，只为存 72 字节的表。`image.go:44-51` 补上了代价：

> 每个函数 12 字节 `.pdata`、约 11 字节 `.xdata`，但一个 PE 段在文件里要占 `FileAlignment`（512，Windows 接受的最小值）的整数倍，所以**一个只有三个函数的程序付了 1024 字节去存 72 字节的表**，而且每个镜像都得至少多这一份。丢掉它们是一个**体积决定**：崩溃的程序无法事后回溯，而这个代价原生代码生成器**本来就在付**（它也不生成 unwind 信息）。

`coff.go:33-34` 把对比讲完了："所以每个 `-fllvm` 镜像都比同样程序的原生路径构建**至少大一个 kilobyte**，而原生路径也不生成 unwind 信息。"

**验证与教训**：这条坑现在是一条**"优化掉的正确性"**——功能（异常回溯）确实丢了，换来的是每段至少 512 字节的文件对齐开销。和坑 37 是同一个 commit（`af2f695`，"修 -fllvm 下 print("hello world") 比原版大 50%：裁剪 goclib 全局 + 丢弃 Win64 unwind 段"）的两面——那边省的是没用的段，这边省的是 unwind 表。

**教训**：**"修好了"和"该修"是两件事**。第一版修法在正确性上是对的（独立段 + 写异常目录），但它引入了新的失败模式（体积回归）。真正要问的是"这个功能值得它引入的失败模式吗"——这里答案是不值得，于是整个功能被移除，而不是被修好。保留 `planUnwindSections` / `unwindSectionOut` 这些死代码、并让 `PdataSize == 0` 的分支自然跳过异常目录，是比删干净更好的选择：**它把决策记录在了代码里**，下一个想重新开启 unwind 的人能看到当初的账。

### 坑 30：Win64 没有数据重定位，必须走 `.refptr` 槽

**现象**：`int *gp=&g; return *gp;` 段错误。解码 `.text` 发现 `lea rax,[rip+X]` 的目标 = **0x1000（.text 首）**而不是 `.data`——**位移是合法的，指令能执行，所以表现为崩溃而非链接错误**。

**为什么会错**：COFF 是可重定位目标格式，但 **Win64 COFF 刻意没有数据重定位**。原因在 `coffmerge.go:289-295`：对未定义数据的引用不能是"指向符号的 RIP 相对位移"，因为**那个地址还不存在**——没有任何地方可以指向它。

LLVM 的答案是：把指针槽放进一个**以符号命名的独立 8 字节段** `.rdata$.refptr.<name>`，里面放着"宿主将来会填进去的地址"。所以对象里对 `g` 的引用，实际是对**这个槽**的引用。goa 三处都不认识它：

1. 段名不在 `coffSectionMap`（`coffmerge.go:43-54`）→ 单独建段 → **镜像构建器只输出已知段，槽被丢弃**（`coffmerge.go:251-254`："给这个片段一个自己的段会让镜像里没有它——镜像构建器只输出已知的段——于是每个穿过它的引用都读到零"）；
2. undefined 符号解析不到 → RIP 相对位移算成"从段首起"（`coffmerge.go:395-399`："没有这一步名字解析不到，对它的 RIP 相对引用会编码成'从段开头算起的位移'——**一条合法却读错字节的指令，所以失败是崩溃而不是链接错误**"）；
3. `attdirective.go:458` **早已理解 `.refptr`**（AT&T 路径），但 COFF 合并路径没接。

**根因 + 文件:行号**：三处改动。

**① 段名归一。**`coffmerge.go:255-257`：

```go
if k := strings.LastIndex(cs.name, "$.refptr."); k >= 0 {
    name = ".rdata"
}
```

随后 `coffmerge.go:259-262` 把 `mapped` 重新指向 `.rdata` 的属性并置 `known = true`。注意判定用的是 `LastIndex` 找 `$.refptr.`——段的**完整**名字是 `.rdata$.refptr.G_x`，前缀 `.rdata$` 是 COMDAT 选择分组，后缀才是槽名。

**② 记录槽位置。**`coffmerge.go:296-299`：

```go
if k := strings.LastIndex(cs.name, "$.refptr."); k >= 0 {
    refptrFor[cs.name[k+len("$.refptr."):]] = refptrLoc{
        sect: sectionIndexOf(img, gs), off: gs.VSize,
    }
}
```

**③ 符号解析到槽。**`coffmerge.go:400-403`：

```go
if rp, ok := refptrFor[s.name]; ok {
    img.Syms[s.name] = SymLoc{Sect: rp.sect, Off: rp.off}
    continue
}
```

`coffmerge.go:389-399` 的注释解释了为什么这样就够："槽里放着地址，宿主把地址写进那里——**把名字解析到槽，正是让地址算对的那一步**。"

**补一个原稿没写的细节：为什么八字节槽能被 `mov` 直接读到。**槽位置记的是 `gs.VSize`，而这个 `gs.VSize` 是**对齐填充之后**的游标——`coffmerge.go:282-288` 先按 `coffAlign` 补齐：

```go
want := coffAlign[cs.name]
if want == 0 {
    want = 8
}
if pad := align(gs.VSize, want) - gs.VSize; pad > 0 {
    padSection(gs, pad)
}
```

槽的段名是 `.rdata$.refptr.X`，**不在 `coffAlign` 表里**（`coffmerge.go:60-63` 只列了 `.text`/`.rdata`/`.data`/`.bss`/`.xdata`/`.pdata`），于是 `want` 取默认值 **8**。所以：**槽的起始偏移是 8 字节对齐的，槽本身又是 8 字节长 → 整个槽自然 8 字节对齐**。这正是 `mov` 能把它当一个 `quad` 直接读的原因，也解释了为什么 `att_e2e_test.go:294-296` 里那八个字节是全零、且注释说"被测的是**重定位**而不是初始化"——对齐是结构保证的，测试不必操心。

**导入跳板链——Win64 链接的关键区分。**`coffmerge.go:504-512` 的注释：

> 对象的、指向导入函数的相对 call/jump（针对 undefined 符号的 REL32）**不能直接指向 IAT 槽**：槽里存的是函数地址，当作**数据**执行会 fault。所以每个导入发射一个跳转 thunk——`jmp [rip+rel32]` 到 IAT 槽——并把 call/jmp 的 fixup 改走它，和 MSVC 链接出来的形状一样。**数据引用（lea/mov RIP 相对）继续直接指向 IAT 槽，那才是正确的"取地址"语义。**

实现在 `coffmerge.go:513-527`：每个导入 6 字节 `FF 25 <rel32>`（`:519-521`），fixup 打在 `off + 2`（跳过那 2 字节 opcode，`coffmerge.go:522-524`）。改指向由 `coffmerge.go:638-644` 完成（前面在坑 27 引用过）：**只看 `cs.data[off-1]` 是不是 `E8`/`E9`**，是且目标是导入，就换成 `thunk:` 键。

> **"call/jmp 走跳板、lea/mov 仍指向 IAT 槽"这个区分，是 Win64 链接最容易漏掉的一环。** 少做一半会 fault（call 去执行地址表的字节），做多一半会拿到"函数地址的地址"（call 跳到一个装着指针的内存里当代码执行），**两种错都是静默的**。

**修复**：见上面 ①②③。

**验证与教训**：**这是 gocl 能出 exe 的最后一道阻碍。** 回归防线在 `src/gocl/cmd/gocl/main_test.go:5-8` 的注释里，它记录了为什么这些测试必须是端到端的：

> 这些测试驱动**构建出来的二进制**，而不是调用包内函数，因为值得测的是整条链：预处理、解析、检查、降到 IR、用 libLLVM 编译、布局镜像、链接。**一个直接调 `TranslateProgram` 的测试会通过，而它产出的可执行文件段错误——`.refptr` 间接层缺失时正是如此**，而它被抓住的唯一原因是一个会去跑结果的测试。

`src/goa/att_e2e_test.go:117-123` 和 `:301-303` 是配套的测试装置：前者解释"`.s` 里出现的是 `G_x` 而非 `x`，LLVM 定义的是 `.refptr.G_x`，**这个测试必须提供另一半**"，后者把 `.quad G_x` / `.refptr.G_x` 这对模式固化下来。

**教训**：**回归测试必须断言"程序的行为"，而不是"函数的返回值"**。坑 30 是一个返回类型完全正确的函数在一个合法地址上解引用——任何检查内部结构的测试都会通过。唯一能抓住它的是"把生成的 exe 跑起来看输出"。这条防线对本文本章的所有坑都成立，而**静默错位（坑 27 的 addend、坑 30 的 refptr、坑 28 的游标）最容易复发**，因为它们不改变任何签名、任何返回值、任何退出码——除了让程序算错。

### 坑 31：其他静默坑

本节五条都是"解析/链接一路绿灯，运行时出事"的形态。共同点：**它们没有一个共同的根因**——这正是这一类坑难防的原因，只能靠逐条钉死。

**① 导入名必须带 `.dll` 后缀。** 同一 DLL 出现两个描述符（`kernel32` 和 `kernel32.dll`），loader 精确匹配找不到 `kernel32` → `0xC0000139`（`STATUS_ENTRYPOINT_NOT_FOUND`），**进程第一条指令都跑不到且零诊断**。

根因在 `coffmerge.go:438-445`：`parseExtern` 会把名字规范化成"追加 `.dll`"，而 loader 是精确匹配的——所以两个分支都要写全，代码里是 `img.Exts[s.name] = dll + ".dll"`，注释也点出了那个可见症状："**同一个 DLL 的两个描述符**"。

`coffmerge.go:918` 把这个错误码的含义写清楚了："`STATUS_ENTRYPOINT_NOT_FOUND (0xC0000139)`，它对原因**什么也没说**。"

**② `__main` 是 LLVM 的 CRT 初始化桩。** 对象里**带全局构造函数的模块中每个函数**都引用它（不是无条件——`coffmerge.go:126-129` 的注释限定了这个条件），不映射就链接失败。合成一个内容为 `ret` 的 anchor 符号（`coffmerge.go:139-146` 追加 `0xC3` 并登记 `coffNoOpAnchor = "__goc_coff_anchor"`，常量定义在 `coffmerge.go:981`）。

关键细节在 `coffmerge.go:126-130`：`__main` **必须解析成一个函数**，因为调用点会 `ret` 进它所变成的东西。`ret` 会弹掉调用者自己的返回地址，程序继续往下走——**"这正是本意"**（`coffmerge.go:132-134`）。解析时 `coffmerge.go:404-411` 走 `continue`（不登记符号），但重定位阶段 `coffmerge.go:576-578` 把 key 换成 `coffNoOpAnchor` 指向那个 `ret`——**符号登记和 fixup 指向是两条不同的路径**，只做一处会得到"有地址但引用没指向它"。

**③ `ADDR32NB` 的 addend 要从被修补的那个段读**，不是从目标段读。`coffmerge.go:670-678`："从目标段的同一偏移读出来的是那里的任意字节。" 实现在 `coffmerge.go:675-678`，读的正是 `cs.data`——**当前正在被补丁的那个段**，和 `coffmerge.go:645` 的 REL32 是同一个来源。这是段符号重定位的特性：符号本身 `value` 为 0，要的偏移在字段里（`coffmerge.go:650-654`）。

这条有个**双重失败**的注释（`coffmerge.go:655-658` 与 `:665-668` 各说了一遍）：漏掉 addend 会让每一条 `RUNTIME_FUNCTION` 都指向段首，loader 拒绝，程序在第一次异常回溯时死于 `STATUS_PRIVILEGED_INSTRUCTION`。**为什么提两次**——一次是"忘了读"，一次是"读错来源"，是同一个坑的两条路径。

**④ 整个程序只能有一个 `Image`。** 对象里的 `main` 在第一个 Image，桩的 `call main` fixup 在第二个，**永不相遇** → `undefined symbol referenced: main`。ELF 侧的报错串在 `elf.go:133`，COFF 侧在 `coffmerge.go:708`。

顺带一提，`coffmerge.go:698-705` 的注释解释了为什么错误要**整个对象读完才报**："这样一遍就能把缺的东西全报出来，而不是停在第一个。" 而且只在**所有对象都读完之后**才判（`coffmerge.go:706-718` 把名字存进 `img.pending`）——链接 a.o 和 b.o 时，报 `add` 未定义是错的，**因为 b.o 才定义它**。

**⑤ `.refptr` 修复时的变量遮蔽。** `if i := strings.LastIndex(...)` 里的 `i` 是**字节偏移**，同函数里 `baseOf[i+1]` 越界 → `index out of range [7] with length 5`（读起来像越界，实际是用了错的那个 `i`）。已修，`coffmerge.go:296` 用独立的 `k` 变量——**而 `:255` 那处 `LastIndex` 同样用的是 `k`**。

> **补一条原稿没写的：`___chkstk_ms` 栈探针（三下划线）。**
>
> LLVM 把超过一页的栈帧降级成一次对 C 运行时栈探测 helper 的调用，帧大小放在 `RAX` 里。goc 镜像不链接任何 C 运行时，所以这个引用必须在 `coffmerge.go:161-191` 里就地合成。
>
> **helper 的 ABI 约束**（`coffmerge.go:153-155`）：LLVM 的 Win64 大帧序言是 `mov eax,size; call ___chkstk_ms; sub rsp,rax`，所以 helper **只能碰护页（probe）**，且必须**让 `rsp` 和 `rax` 都保持不变**——`rsp` 是留给调用者那句 `sub rsp,rax`，`rax` 是因为那句 `sub` 还要用帧大小。
>
> **前两版都以 `STATUS_STACK_OVERFLOW (0xC00000FD)` 收场**（`coffmerge.go:155-159`）：一个**读 `rcx`**——调用者从不设置 `rcx`，于是按垃圾尺寸探测栈；另一个**自己减页**——双重分配。现版用 **r11/r10** 作游标（`coffmerge.go:160`："r10/r11 按 Windows ABI 是易失的，可以当自由暂存"），编码在 `coffmerge.go:166-190`：
> - `4C 8B D4` = `mov r10,rsp`（注释说明了为什么不用 `89` 变体：**那会是 `mov rsp,r10`，用垃圾 `r10` 把栈指针弄坏**）；
> - 探测块 `sub rsp,4096 ; mov [rsp],r11 ; sub r11,4096` = **18 字节（7 + 4 + 7）**，`coffmerge.go:177-181`；
> - `jg loop` 的偏移**必须算出来不能写死**（`coffmerge.go:182-187`）——写死的字面偏移会跳到探测块外面；
> - 收尾 `mov rsp,r10 ; ret`（`coffmerge.go:188-190`）。
>
> **游标必须精确推进**，`coffmerge.go:173-176` 的原话："探测块是 18 字节（7 + 4 + 7）；**游标若不是恰好前进追加的字节数，从 COFF 对象合并进来的每个符号都会提前落位**——入口桩调 `main` 时会跳到函数前面的那个对齐字节上，立刻 fault。"
>
> 这和坑 28 是同一个"静默错位"的家族：辅助块多写/少写一个字节，符号全体偏移，后面所有引用都指向别处。

**验证与教训**：这五条的共同教训是——**"链接器报告 undefined symbol"其实是这条链上最好的结果**。①②③⑤ 一旦错位没被抓住，产物是一个合法但行为错误的可执行文件；只有 ④ 会大声报错（而且报的是 `undefined symbol referenced: main`，指向真正的根因）。

**教训**：**在静默失败的一层之上，优先保证它的上一层仍然响亮**。`coffmerge.go:430-434` 的取舍值得记住——未知符号不当导入，**宁可报链接错误**："一个靠猜出来的导入构建出的镜像能正常加载，然后死于 `STATUS_ENTRYPOINT_NOT_FOUND` 并且毫无解释。"（`coffmerge.go:415-416`、`coffmerge.go:320-322` 重复了这条标准。）把不确定性转成错误，是静默失败链条上唯一可靠的刹车。

### 坑 32：COFF 对象的真实结构（实测数字，可作基线）

- **段共 6 个**：`.text`(0xd8) `.data`(0x20) `.bss`(4) `.xdata`(0x18) `.rdata`(0xf) `.pdata`(0x18)
- **重定位只有 2 种类型**（比预想可控得多）：`REL32` × 10（代码内 call / lea rip-relative）、`ADDR32NB` × 6（`.pdata` 的 RVA）
- **`@feat.00`**：`sec = -1` = absolute，安全 cookie 表
- **COMDAT**：`.pdata` 带 aux `comdat 0`，链接时要去重

这些数字可以在源码里逐条对上：6 个段名与 `coff.go:16` 的列表一致，而 `coffSectionMap`（`coffmerge.go:43-54`）正好收录了这 6 个；`.bss`(4) 那 4 字节是虚空间（坑 28 的 `cs.vsize`）；`.xdata` / `.pdata` 的 0x18 = 24 = 两个函数 × 12 字节，与坑 29 的"每函数 12 字节 `.pdata`"吻合；COMDAT 去重走 `coffmerge.go:460-466`（"只有 external linkage 算重复——**段定义是 static 符号，而每个对象都有 `.text`**，把它们当重复会让任何多对象链接全部失败"）。

**"只有 2 种"是那个被测对象的结论，不是链接器的上限。** 链接器实际接受 **3 种**（多一个 `ADDR64`，见坑 26；完整分支在 `coffmerge.go:603-695`，常量在 `coff.go:46-50`）。这一节的权威出处是 `src/gocld/coff.go:13-24` 的文件头注释，它开头就写着 "measured on LLVM 23.1.2, **not guessed**"，逐条列出了 6 个段、2 类重定位、undefined 符号、每份 unwind 贡献一个 COMDAT 组、`@feat.00`（这三项在 `coff.go:21-24`，其中 COMDAT 那条对应 `coffmerge.go` 的 dedup 路径）。

**读这份注释比读实测数字可靠——它是把测量结论固化在代码里的**，而且它和实现是同一次提交改的，不存在"注释过期"的问题（对比坑 26 里 `coffmerge.go:24-33` 那个仍然写着旧 `ripAdj` 模型的过期文件头）。`coff.go:19-20` 还给了这份基线一个明确的安全属性："**其他类型是被拒绝的，而不是被静默链接错**"——这条与实测到的 2 种形成互补：**基线之外的取值不会静默通过**。

> **bigobj：这条基线没覆盖到的最大变体。**
>
> `coff.go:200-225` + `:271-291`：`SizeOfOptionalHeader == 0x20` 表示 **bigobj**，符号记录宽度是 **20 而非 18**，布局为 `[4]flags [8]name [4]value [2]section [2]type [4]class+naux`，而且 `StorageClass` 与 `NumberOfAuxSymbols` **挤在同一个 4 字节字段里**（`coff.go:289-291`：`cls = src[rec+18]; nAux = int(src[rec+19])`）。为什么必须从头部判断而不能猜，`coff.go:208-212` 说得很准："按 18 字节读一个 bigobj 表会产出**看起来合理的垃圾，而不是一个错误**"——**静默失败，正是本节标题说的那种坑**。
>
> **LLVM 对 Windows 目标默认就发 bigobj**（`coff.go:201-203`，因为模块可能超 65,535 符号上限），所以这不是罕见路径。坑 24 只讲了 aux 要占位，没提记录宽度本身会变，**两条必须合看**——详见坑 24 末尾的合并分析。
>
> 顺带指出一个 `coff.go` 内的不一致：`coff.go:155` 的 `coffStrBase := symTableOff + 18*nSyms` **写死了 18**，而 `coff.go:233` 的 `strBase := symTableOff + recSize*nSyms` 用的是实际记录宽度。`readName`（`coff.go:252`）和 bigobj 的 flags 路径（`coff.go:278`）喂的是**前者**——classic COFF 下两者相等所以不暴露，但这块地板正是 bigobj 会撬动的。

**验证与教训**：**教训**：把实测结论**写回代码注释**是一种高回报的习惯——`coff.go:13-24` 让后来的每一次修改都有了一个可对照的基线，不必重新 `objdump` 一遍。而这条注释之所以值得信，恰恰是因为它**标出了自己的可信度来源**（"measured on LLVM 23.1.2, not guessed"）。对照本章的教训 3：坑 26 那个过期文件头没有这样的标注，于是它被当成了权威。**一个注明了测量方法和新旧时间的注释，和一个没写来源的注释，可信度差着一个数量级。**

### 本章回归防线

静默错位（坑 27 的 addend、坑 30 的 refptr、坑 28 的游标、坑 31 的 `___chkstk_ms` 游标）不改变任何签名、任何返回值、任何退出码——**除了让程序算错**。所以它们的防线只有一处，但必须是这一处：

**跑它。** `src/gocl/cmd/gocl/main_test.go:5-8` 把理由写在文件头：

> 这些测试驱动**构建出来的二进制**，而不是调用包内函数，因为值得测的是整条链：预处理、解析、检查、降到 IR、用 libLLVM 编译、布局镜像、链接。**一个直接调 `TranslateProgram` 的测试会通过，而它产出的可执行文件段错误——`.refptr` 间接层缺失时正是如此**，而它被抓住的唯一原因是一个会去跑结果的测试。

配套的测试装置在 `src/goa/att_e2e_test.go`：`:117-123` 解释为什么测试必须替前端补上 `.refptr.G_x` 这个"另一半"——否则一个完全正确的翻译单元会**因为一件它本来就不该定义的东西而"链接失败"**；`:301-303` 把 `.quad G_x` / `.refptr.G_x` 这对模式固化下来；`:294-296` 说明那八个字节为什么是全零——**被测的是重定位，不是初始化**。

**教训（本章总结）**：写解析代码时，"读到了看起来合理的值"不是验收标准——**"读到的值和字节布局一致"才是**。本章十条坑里有九条的共同根因都是"某个定长字段被按错误的宽度或错误的半边读了"，而它们的失败形态高度一致：**不报错、产出合法镜像、行为错误**。对抗这种失败只有三种手段，而且都不在代码里：

1. **找到头部里的权威标志位**（坑 23 的首 dword、坑 24 的 `SizeOfOptionalHeader`、坑 25 的 `Type` 枚举穷举 + `default` 拒绝）；
2. **把测量结论写成带来源的注释**（`coff.go:13-24` 的 "not guessed"）；
3. **让测试跑产物，而不是跑函数**（`main_test.go:5-8`）。

至于怎么判断一个偏移究竟是"符号地址算错了"还是"符号地址对了但加上常数错了"——**看症状的分裂性**：坑 27 里零偏移引用全对、带偏移引用全错，这就是答案；而这个技巧本身，就是把一条静默故障变成一条可定位故障的唯一办法。

## 五、体积：必须落到段表 + 对齐上量

> 本章数字分两类：**今天的实测**（`-O2`，Win64 PE32+，仓库 HEAD `c9e2b58`，`stat -c%s`）与**历史值**（标注"历史"）。每条都注明。之所以要分开，是因为这批数字自己就会随修复而反转——见坑 34。

### 全章的纲：exe 大小与"你写了多少代码"几乎无关

先说结论，它是后面 8 个坑的公共解释：

**exe 大小 ≈ Σ（每个段的 raw size 按 `FileAlignment` 向上取整）+ 头部**，而不是"我写了多少行代码"。

这个式子只有三个因子：**段的数量**、**每段的字节数**、**每段的对齐粒度**。三者都与代码量脱钩：

- 一个 **36 字节**的段和 一个 **0 字节**的段，在文件里都占 **512 字节**（`FileAlignment` 的最小合法值）；
- 一个 **93 字节**的段，在 `FileAlignment=0x1000` 下占 **4096 字节**——44 倍浪费，且与代码量无关；
- 把 `.pdata`/`.xdata` 两个段从镜像里去掉，一个函数都不少，三个函数的程序立刻省 **1024 字节**。

所以省体积的手段有严格优先级：

1. **第一性：减少段的数量。** 段数是乘数，每多一个段就至少多付一次 `FileAlignment`。这是唯一能按"个"省的钱。
2. **第二性：调对齐粒度。** `FileAlignment` 是乘数，`SectionAlignment` 是页大小、改不了。
3. **第三性：裁代码。** 裁代码减少的是**某一段的字节数**，在段已经存在之后，它的效果被对齐粒度整除掉——这就是为什么先裁代码常常测不出体积变化（坑 37）。

**这一章的顺序就是按这个优先级排的**：坑 33/36 是"减少段的内容"（其实只是让某段变短，属于第三性，但因为它是入口，先讲），坑 35/39 是"裁代码"，坑 37/38 才落到段表和对齐上——那才是第一性和第二性手段。**先做第三性再做第一性，是本轮最大的时间浪费**，坑 37 就是现场。

---

### 坑 33：IR 前端无条件发射全部运行时函数

**现象.** `-fllvm` 链接出来的 exe 是 100k+ 量级，而原生后端同样程序约 13k。第一版修法是加可达性游走。

**为什么会错.** 原生后端有 `c.need` 不动点：只发可达的十几个函数。IR 前端**没有这一层**——它拿到 `goclib` 的全部函数表，逐个翻译、逐个发射，不管程序用不用。等于把整个 C 库塞进每个 exe。

**根因.** `src/gocl/translate.go:207-209` 的注释写着这件事：*"Emitting every function in the runtime (~380 of them) is what made the -fllvm binary 100k+ next to the native build's ~13"*。

修法是 `llvmRoots`——不动点可达性游走，IR 前端版的 `c.need`。**签名是四个参数**：`llvmRoots(prog, lib, linux, tr)`，定义在 `src/gocl/translate.go:297`，调用在 `:214`。多出来的 `tr` 是为了把 05 里那张 `UserDefines` 表传进同一个特化判定（见坑 36），不是因为算法需要它。

注意"380"这个数字**今天已经过时**：实测 `common.Build()` 返回的运行时**有函数体的函数是 530 个**（`lib.Order` 也是 530 项，无纯原型），其中 **528 个 LLVM 合资格**、2 个不合资格（`gthr_spin_lock` / `gthr_spin_unlock`，用到前端还没建模的构造）。所以今天"全量发射"意味着 528 个，不是 380。

**修复.** `src/gocl/translate.go:214` 拿到 `need` 后，只把 `need[name]` 为真的名字进 `wanted`；函数体发射循环（`:255-265`）之前，所有名字先在 `m.defined` / `defined` / `tr.funcDefs` 里登记（`:249-253`）。

**验证与教训.** 用 `-dump-ir` 数 `^define`，**今天的实测**：

| 程序 | 裁剪后 define | 关掉裁剪（把全部 528 个合资格函数标为可达） | 倍数 |
| --- | --- | --- | --- |
| `int main(){return 42;}` | **2** | 534 | 267× |
| `print("hello world")` | **4** | 534 | 133× |
| `printf("hello, world\n")` | **14** | 534 | 38× |
| `puts("hello")` | **17** | 534 | 31× |
| `printf("%d\n",3)` | **22** | 534 | 24× |
| `printf("%f\n",1.5)` | **30** | 534 | 17× |
| `printf("%%\n")` | **43** | 534 | 12× |

关掉裁剪那一列是**今天的实测**（在隔离副本里把 `src/gocl/translate.go:227` 那个 `if !need[name] || emitted[name]` 的 `need` 条件去掉重建，实测每个程序都是 534 个 define——528 个合资格运行时函数 + `main` + 5 个 LLVM 为常量除法生成的 `@div`/`@ldiv`/`@lldiv` 等匿名辅助函数）。

**教训**：空程序只要 2 个 define（`main` + `__goclib_exit`）这件事，比"省了多少字节"更有说服力——它是**离散**的、可数的、无争议的。体积是连续量，谁都可以争"这个改动省了 2% 但值得"；函数数没有这种余地。

---

### 坑 34：printf 调用点特化不在 IR 前端（数字已被后续修复反转）

**现象.** `printf("hello, world\n")` 一个空 main，LLVM 侧比 goa 后端大——**修复前**是 16896 vs 6656（约 2.5×）。

**为什么会错.** `printf` 的核心是 `vfmt`，一个**函数体含覆盖所有格式符的 switch**（含 `%f`）。所以静态引用收集只要碰到 `printf`，就把整条浮点格式化链拖进来：`vfmt` → `double_to_hex` / `double_to_exp` / `fmt_int_part` / `exp` / `log` / `frexp` / `ldexp` / `signbit`……本程序一个都没用到。

**根因是架构缺口，不是 bug**：特化逻辑放错层了。goa 后端在 codegen 路径上有调用点特化，LLVM 侧完全没有——特化挂在**后端**，不在 IR 前端。

今天实测（`-O2`）的 `.asm` 对照：

| | .asm 行 | 标签数 | exe 字节 |
| --- | --- | --- | --- |
| goa 后端 | 1224 | 61 | **5632** |
| LLVM 侧 | 917 | 69 | **4096** |

**结论反转了**：今天 LLVM 侧 4096 < goa 侧 5632，**小 27%**。原稿的 16896 vs 6656 是**历史值**，2.5× 这个比值今天不成立。

反转发生的原因就是后面几个坑：`printfspec.go` 的共享特化（坑 35/36）+ 两项段对齐修复（坑 37/38）。5632 → 4096 这一步，靠的正是把 `.pdata`/`.xdata` 移出镜像并把 `FileAlignment` 降到 0x200。

**教训（本坑最值得记的一条）**：**别把体积数字当结论记，要记"哪个机制让它变小"。** 今天如果有人翻到"LLVM 侧 hello world 16896"这个数字，会得出"LLVM 后端在 printf 上有 2.5× 缺陷"的结论，然后去优化 LLVM 的 printf——而实际缺陷在"特化放错了层"，且早就修好了。**数字会随修复反转，机制不会**。所以本章每条都把数字和产生它的机制分开记。

---

### 坑 35：靠 LLVM 自己折叠格式串——两条都走不通

**现象.** 最省事的想法是：让 LLVM 自己把 `printf("%d",3)` 优化掉。实测走不通。

**为什么会错.** 两个独立原因，任何一个都足以否掉这条路：

1. **`printf` 不是 LLVM 认识的符号。** `SimplifyLibCalls` 只处理**已知 libc 符号**，而 goclib 的 `printf` 被当普通函数发射成 `define i32 @printf(ptr, ...)`。实测（`%e` 那一档，`-dump-ir`）：`pce.ix.ll:7503` 是 `define i32 @printf(ptr %p0, ...)`——**是 `define` 不是 `declare`**。从 LLVM 角度看这是个恰好长得像标准原型的本地函数，它不认。
2. **走第 2 条路（让 LLVM 看得见字面量）也不行**：把格式串改成常量全局，**过不了可达性裁剪**——全局的引用会把裁剪的证据改掉。

> **这里要更正原稿一处观察。** 原稿写"格式串在 IR 里是 `alloca` + 逐字节 store + `getelementptr`，即使认得 printf 也看不到字面量"。**今天前端已经改好了**：格式串是**具名常量全局** `@.str.0 = global [4 x i8] c"%e\0A\00"`，`main` 里直接 `getelementptr inbounds i8, ptr @.str.0, i64 0` 当参数传给 `printf`。那个 `alloca [5 x i8]` 逐字节 store 的链**确实还在，但它是死代码**——`%t2`…`%t6` 各自只被下一条 store 用一次，没有任何活的使用者。所以"LLVM 看不到字面量"今天已经**不成立**；真正卡住的只有原因 1。

**修法.** 特化判定抽到 **`src/common/printfspec.go`**（注意路径：它在新建的 `common` module 内，`src/common/go.mod`，module 名 `goc/common`）。`SpecializePrintfCall` 在 `:50`，两个后端各在自己的发射点调它：

- `src/gocl/call.go:108` —— IR 前端
- `src/goc/codegen.go:10039` —— 原生后端

参数化成两个查询（`:26-32` 的 `PrintfQueries`）：`UserDefines`（程序是否自定义该名）/ `ShadowedByVar`（有同名函数指针变量）。

**对照实验说明膨胀确实来自 printf 链**：绕开 printf 用 `puts`，LLVM 侧 4608 < goa 侧 6144；用 `print("hello world")`（走 `str_print`，**printf 特化链完全没参与**），LLVM 侧 1536 < goa 侧 2048。今天这两档都是 LLVM 更小——**膨胀已经不在了，因为特化补上了**。

**验证与教训.** 今天的实测：`printf("hello, world\n")` gocl 4096 / goa 5632（坑 34 那档）；`printf("%d\n",3)` 5632 / 6144。

> **教训**：**"让优化器去做"是一个偷懒的诱惑，但它把成本从你控制的地方挪到了你控制不了的地方。** 格式串折叠在 LLVM 里不成立（原因 1），就算成立，它的效果也落在**别人后端的优化水平**上——同一份 IR，libLLVM 版本一改，体积就变，而你的编译器对此没有控制权。特化只有 40 行、放在自己手里、两个后端共享，才是能被测试钉住的东西。

---

### 坑 36：【铁律】可达性裁剪必须走同一个特化判定，否则互相抵消

**现象.** 坑 35 的特化加上后，**体积一点没变**。

**为什么会错.** `llvmRoots` 原本独立扫**原始 AST**，看到的是 `printf` 而不是改写后的 `fwrite`/`printf_lite`。于是：**裁剪把 `printf` 拉回来 → 特化被抵消 → 等于没改**。两个正确的组件，方向相反，互相抵消。

**根因+文件:行号.** 特化判定与可达性判定**必须是同一个决定**，而不是两个碰巧结论相同的决定。现在 `llvmRoots` 里对每个 `Call` 引用先问一次特化（`src/gocl/translate.go:349`），拿改写后的名字去标记可达。

**修复.** 三条纪律 + 两条原稿没写的顺序约束：

1. `llvmRoots` 也走 `common.SpecializePrintfCall`（`src/gocl/translate.go:349`）。
2. **Call 与 Ident 两类引用要合并进一次遍历**（`:343-368`）。两遍并存时，第二遍会把刚摘掉的 `printf` 又加回来——第一版就犯了这个。
3. 改写引入原调用没有的引用，要一并标记可达（`:357-358`）：`fwrite` 拿到的 `__goclib_stdout()` 是个 **Call**，原 `printf` 里根本没有，不显式加就链不出符号。

**两条顺序约束（原稿未写，是真实踩过的）**：

4. **`lib.Order` 里同一函数名可能出现两次**——运行时源文件是拼接的（`src/gocl/translate.go:216-221`），两个源文件定义同名函数就会在 order 列表里出现两次，重复 `define` 报 `invalid redefinition of function`。修法是用 `emitted` 集合去重，且**用户同名函数优先**：`emitted` 先用 `prog.Funcs` 的名字填满（`:223-225`），再处理 `lib.Order`。**一个 module 只能有一个同名 definition，而用户程序自己定义的名字已经被占了。**
5. **先标记全部 `define` 再逐个生成函数体**（`:244-253`）。否则前一个函数体里的调用会为后一个函数发出 `declare`，LLVM 把 declare + 同名 define 视作 redefinition。注释写得很直白：*"A call to a function defined later in the list would otherwise emit a `declare` for it during the earlier body's generation, and the later `define` would collide with it."*

顺带修了个**静默 bug**：shadow 判定查的是 `tr.funcDefs`，但那张表**同时装用户函数和 goclib 的 wanted 函数** → `fwrite` 永远被判"被遮蔽"，特化从不触发，而且**不报错**。修法是新增 `tr.userDefs` 只装用户函数（`src/gocl/types.go:43`，填充在 `src/gocl/translate.go:89`），两个后端的查询都改问它（`src/gocl/call.go:265`、`src/gocl/translate.go:323`）。

**验证与教训.** **这个坑比它看起来更值得记，因为它的教训是关于"契约"而不是关于代码。** `printfspec.go:22-25` 把契约写在注释里：两个谓词**只能问用户的声明，绝不能问运行时自己的副本**，否则每个库函数看起来都被遮蔽。而"必须在编译期决定"同样是硬约束（`:46-49`）：*"A run-time 'try lite, fall back to vfmt' probe would leave vfmt reachable and nothing would be pruned"*——**运行期试一下 lite、失败再退回 vfmt 的探测会让 vfmt 始终可达，什么都裁不掉**。

这两条不是风格偏好，是坑 36 铁律的**机制本身**：特化只有在编译期把调用点改掉、裁剪才可能把 vfmt 裁掉；任何"运行期再决定"的方案都会让 vfmt 可达。**探测式（try/fallback）设计和可达性裁剪在原理上互斥。**

> **教训（可迁移）**：**当两个组件必须对同一个问题给出一致答案时，不要让它们各自推导——把决定提取成一个共享函数，并把"只能问哪些事实"写成契约注释。** 这里的代价是两个正确组件互相抵消、且**不报错**（裁剪悄悄把特化undo了）。凡是"发射端和裁剪端都对某件事做判断"的地方，都要检查它们问的是不是同一个对象。

---

### 坑 37：PE 段级文件对齐——72 字节的表吃掉 1024

**现象.** `print("hello world")`（走 `str_print`，**printf 特化链完全没参与**）LLVM 侧比原版大。**今天实测 1536 vs goa 2048**（原稿的 3072 / 2048 是历史值）。

**为什么会错（我先查错了方向）。** 我先认定是"IR 前端无条件发射全部 lib.globals"（原稿说 25 个），改完发现**体积纹丝不动**：实测 `print("hello world")` 的 IR 里 `G_` 全局定义数是 **0**（不是 25），`.data` 从 231 降到 183 字节，但**总大小不变**。

> **教训**：改完必须重新看段级数据；不能因为"找到一个看起来合理的缺陷"就认定它是根因。我当时改的那个东西**是真的改进了**（少发射 25 个全局），只是**它不在这条体积曲线上**。

**真根因 = PE 段级文件对齐。** LLVM 多的 `.pdata`/`.xdata`（Win64 SEH 展开表）表本身很小（每函数 `.pdata` 12B + `.xdata` ~11B），但 **PE 每个段在文件里占 `FileAlignment` 的整数倍**，所以两个段 = 实打实一个对齐单位，与内容多少无关。

**今天用隔离构建实测的对照**（把 `src/gocld/pe.go` 的 `fileAlign` 保持 0x200，只把 `coffmerge.go:280` 那行 `gs.Unmapped = true` 去掉，重新构建一个 gocl，其余不变）：

| | `.pdata` VSize | `.xdata` VSize | 文件里各占 | 段数 | exe |
| --- | --- | --- | --- | --- | --- |
| 保留展开表 | 36 | 36 | **512 + 512** | 4 | **3072** |
| 移出镜像（现状） | — | — | 0 | 2 | **1536** |

**三个函数（36/12 = 3 个 `.pdata` 项）花 1024 存 72 字节。规模越大亏越多**——每个函数 12B `.pdata` + ~11B `.xdata`，但每**个段**都要付满一个 `FileAlignment`。函数少的时候最亏。

顺带印证全章的纲：**这里省下 1536 字节，靠的是"段的数量"从 4 降到 2，不是一行代码被裁掉。**

**根因+文件:行号.**

- `Section.Unmapped` 定义在 **`src/gocld/image.go:40-52`**（不是 `coff.go`），注释里已把账算清：*"a program with three functions paid 1024 bytes to store 72 bytes of table, and every image carried at least that. Dropping them is a size decision: a program that crashes cannot be post-mortem walked, which is a cost the native generator was already paying."*
- 置位在 `src/gocld/coffmerge.go:277-280`，条件是 `name == ".xdata" || name == ".pdata"`，**同时覆盖两个段**（05 篇那条注意事项同源）。
- COFF 对象写出跳过它们：`src/gocld/coffwrite.go:338-344`（注释：*"has no bytes in the finished image by design -- it is regenerated at link time from the UWFunc records -- so there is nothing to emit"*）。
- PE 不给镜像地址：`src/gocld/pe.go:75-83`——`planUnwindSections` 里 `xdata`/`pdata` 命中 `Unmapped` 就置 nil，因此**既不贡献段记录，也不占异常目录的槽**。

**修复.** COFF 合并**照常并入**这两个段的字节（符号照常解析、`ADDR32NB` 重定位照常应用 → **不留悬空引用**），只是不给镜像地址。

**关于"Unmapped 是纯 PE 概念"的一个重要补充**：这句只在**语义来源**上成立——语义确实来自 PE 的 Win64 展开表规定。但 **ELF writer 也消费这个字段**：`src/gocld/elfwrite.go:236`（`collectSections` 跳过 `Unmapped`）和 `:380`（符号落在未发射的节上时跳过该符号，注释：*"The symbol lives in a section that was not emitted (Unmapped) ... writing it as undefined would be a lie -- it would look like something this object expects a linker to supply"*）。原因是**通用问题，不是 PE 特有**：任何"节被合并进镜像但符号还指向它"的格式都会遇到"符号指向未发射的节"，所以 ELF 也需要一个 `secIdx` 查不到的兜底。**如果不消费它，Linux 路径上就会写出悬空符号。**

丢的是崩溃后 post-mortem 回溯栈。goc 只编 C、无 C++ 异常、无自己的 unwinder，且**原生后端早就接受了同样损失**（goa 自己不发这两个段，见 `src/gocld/pe.go:67-69`）→ **体积决策非正确性决策**。

**验证与教训.** 测试**直接读 PE 段表 + data directory[3]**，不比总大小——表将来变大也不会在重新引入浪费时仍通过。今天实测 `data directory[3]`（异常目录）= **0x0**，段表只有 `.text`/`.data`（+ 视情况 `.bss`），没有 `.pdata`/`.xdata`。这个断言在 `src/gocl/printfspec_test.go:267`（`TestUnwindSectionsStayOutOfImage`），注释说明了为什么用异常目录当代理：*"pe.go only fills it when a .pdata section was given an address, so a zero here means the tables were left out on purpose rather than by accident."*

> **教训（可迁移）**：**体积断言必须断言"结构性事实"，不能断言"总大小"。** 总大小是所有优化的和，任何一处改进都能掩盖另一处退化——我删掉 25 个全局后总大小不变，正是这个原因。直接断言段表里没有某个段，才会在有人重新引入时立刻失败。

---

### 坑 38：`FileAlignment` 设 0x1000 导致 93 字节的 `.text` 占 4096

**现象.** hello.exe 16384 字节（历史值）。

**为什么会错.** `SectionAlignment` 必须保持 `0x1000`（页大小，PE 载入器要求，改不了）。但 **`FileAlignment` 只管文件内的 padding，与内存布局无关，是可以降的**。两者混淆了，就把文件粒度也设成了 4 KiB。

**根因+文件:行号.** `src/gocld/pe.go:18-20`：

```
sectAlign = 0x1000
fileAlign = 0x200
```

注释（`:14-17`）：*"SectionAlignment is the page size (4 KiB) and cannot shrink; FileAlignment only governs padding inside the file, and 512 is the smallest value the PE spec allows. Using 512 instead of 4096 is what keeps a 300-byte .text section from costing 4 KiB on disk."*

**修复.** `fileAlign` 从 `0x1000` 改成 `0x200`（PE 规范最小值，也是 MSVC 的默认）。

**今天用隔离构建实测的对照**（只把 `fileAlign` 从 0x200 改回 0x1000，`Unmapped` 逻辑不动）：

| 程序 | `0x200`（现状） | `0x1000`（对照） | 差 |
| --- | --- | --- | --- |
| `int main(){return 42;}` | 1536 | 12288 | +10752 |
| `print("hello world")` | 1536 | 12288 | +10752 |
| `printf("hello, world\n")` | 4096 | 12288 | +8192 |
| `puts("hello")` | 4608 | 12288 | +7680 |
| `printf("%d\n",3)` | 5632 | 16384 | +10752 |
| `printf("%f\n",1.5)` | 7680 | 16384 | +8704 |
| `printf("%%\n")` | 23552 | 32768 | +9216 |

段表（`print("hello world")`，`fileAlign=0x1000`）：`.text` RawSize **512** → 文件里占 **4096**；`.data` RawSize **512** → 占 **4096**。**两个段共 8192 字节的文件成本，装 1024 字节的实数据。**

**附带坑：符号基址表 `symBase` 和节的 VirtualAddress 是两回事。** `src/gocld/pe.go:314-320` 里 `symBase` 是**每个源节**用于解析符号引用的基 RVA，其中 `.data` 是 `dataBase + dOff`；而 `.data` **节头里的 VA** 是 `dataBase`。节的 VA 是 `dataBase`，`.data` 里符号的 RVA 是 `dataBase + 偏移`——**两者不能混用**，`symRVA[name] = symBase[s.Name] + loc.Off`（`:329`）里加的是**节内偏移**，不是节的 VA。

**验证与教训.** `SectionAlignment` 保持 `0x1000` 不变，只动 `FileAlignment`。今天实测 `print("hello world")` 的段表确认 `FileAlignment=0x200 / SectionAlignment=0x1000`，两个段 RawSize 都是 512、文件成本都是 512。

> **教训（可迁移）**：**"必须对齐"这句话要问清是内存对齐还是文件对齐——两者的最小值和可调性都不一样。** 这两个常量在名字上只差一个词，在后果上差 8 倍。

---

### 坑 39：`printf("%%\n")` 是真漏洞（两边共有，且比想象更糟）

**现象.** `%%` 是字面百分号，无参数、无副作用，**看起来完全无害**——但它让整个程序退回全量 `vfmt`。

**为什么会错.** `printf("%%\n")` 这个格式串**含有 `%`**，所以走不进"无 `%` → 直接 `fwrite`"那条分支（`src/common/printfspec.go:73`）；而 `ScanLiteFormat` 对 `%%` `continue` 但**没有置 `has`**（`:165-167`），于是 `liteTargetFor`（`:126`）在 `:128` 的 `if !has` 返回 false。**两条分支都不接，整个调用退回普通 `printf`，整套 `vfmt` 被拉入。**

**今天的实测（比原稿更糟）**：

| | gocl | goa |
| --- | --- | --- |
| `printf("%%\n")` | **23552** | **42496** |

（原稿的 22016 / 37376 已过时。）对比同批的 `printf("hello, world\n")` 是 4096 / 5632——**一个"什么都不做"的字面量贵了 6 倍。** IR 侧印证：`%%` 发射 **43** 个函数且**没有 `printf_lite`**，整套 vfmt + double_to_buf + exp/log/frexp 全被拖进来；而 `%d` 那档是 22 个函数。

**一个额外的精确发现（今天探出来的）**：这个 bug 的形状不是"漏了一个转义"，而是**两个分支之间的缝**。而且它**和测试的意图不一致**：

- `printfspec.go:42` 的文档注释明确宣称 lite 集合包含 `%%`（*"only %s, %c, the integer conversions, %f and %%"*）；
- 但 `src/gocl/printfspec_test.go:191` 却断言 `ScanLiteFormat("%%")` 应返回 `(false, false)`，理由是 *"a literal percent is not a conversion ... That makes it the fwrite case, not the lite one."*

**测试是对的，注释是错的**——而 `%%` 实际既不是 lite（被 `has=false` 挡掉），也进不了 fwrite（被 `Contains("%")` 挡掉）。所以缺陷的准确形状是：**`%%` 需要"解转义"这一步，而两个分支一个都不做它**。只改任一个分支都不够：把它算作 `has=true` 但不解转义，lite 会原样打印两个 `%`；让它走 fwrite 但不解转义，`fwrite` 会写两个 `%` 而不是一个。

**修法方向（未做）。** `%%` 应视作"无转换但需解转义"。要么在进入 `fwrite`/`lite` 前把格式串**预处理**成解转义后的新字面量（这是唯一正确的做法，且能同时喂饱两条分支）；要么给 lite 加一个解转义开关。前者更好，因为它把转义处理收敛到一处，而不是让每个下游各解一次。

**`fprintf`/`sprintf`/`snprintf` 的特化状态（今天实测，比原稿精确）**：

- `fprintf` **部分特化**：`fprintf(stderr, "plain\n")`（无 `%`）**确实**走 `fwrite`——实测 14 个函数、4096 字节，与 `printf` 的无 `%` 档同档。
- `fprintf(stderr, "%d\n", 3)` **不特化**，43 个函数、23552 字节。原因是 lite 格式化器不接受 stream 参数（`printfspec.go:103-107`：*"The lite formatters take no stream, so there is no equivalent for fprintf: routing it to one would write to the wrong FILE."*）。**这是有意的正确性决策，不是 bug**——把它路由到 `printf_lite` 会写错文件。
- `sprintf`/`snprintf` 一律未特化（没有对应 lite 入口）。

**验证与教训.** 这个坑的教学价值在于**它不在"常见写法"里**。`%e`/`%g`/`%ld` 不特化是**已知的、有意的**（需要 vfmt 的字段/指数机制）；而 `%%` 是**一个字面上无害的转义**，用户不会想到它昂贵。**特化的每一个"不匹配"分支都是体积悬崖**，因为退回路径拉进来的是整个格式化引擎。

> **教训（可迁移）**：**白名单式的特化（"满足条件才走快路径"）里，每一条被拒绝的条件都是一个体积悬崖，而且越"无害"的条件越危险。** `%%` 的教训是：当判定函数把某个输入分类为"既不走快路径、也不触发错误"时，它不是安全的默认，而是**静默地把整条大依赖链拉进来**。所以判定函数的输出应该是三态（走A / 走B / 明确不支持），并且每一条"不支持"都要在体积实测里有一档——`%%` 有，`%s` 有，`%f` 有，缺的档就是隐患。

---

### 坑 40：体积实测对照表（**今天的实测**）

全部为 **2026-10-07 实测**，`-O2`，仓库 HEAD `c9e2b58`，PE32+ / Win64，`stat -c%s`。**这一节的数字全部是今天的实测，没有历史值。**

| 档 | 程序 | LLVM 侧 | goa 侧 | delta | IR define |
| --- | --- | --- | --- | --- | --- |
| 空程序 | `int main(){return 42;}` | 1536 | 1536 | **0** | 2 |
| `print`（非 printf 链） | `print("hello world")` | **1536** | 2048 | −512 | 4 |
| 无 `%` | `printf("hello, world\n")` | **4096** | 5632 | −1536 | 14 |
| 无 `%` | `puts("hello")` | **4608** | 6144 | −1536 | 17 |
| 整数 lite | `printf("%s\n","hi")` | **5632** | 6144 | −512 | 22 |
| 整数 lite | `printf("%d\n",3)` | **5632** | 6144 | −512 | 22 |
| 浮点 lite | `printf("%f %f\n",1.5,2.5)` | **8192** | 13312 | −5120 | 30 |
| 浮点 lite | `printf("%f\n",1.5)` | **7680** | 13312 | −5632 | 30 |
| math.h | `printf("%f\n",sqrt(2.0))` | 8192 | 13824 | −5632 | 31 |
| **未特化** | `printf("%e\n",1.5)` | 23552 | 42496 | −18944 | 43 |
| **未特化** | `printf("%ld\n",3L)` | 23552 | 42496 | −18944 | 43 |
| **未特化** | `printf("%%\n")` | 23552 | 42496 | −18944 | 43 |
| **未特化** | `fprintf(stderr,"%d\n",3)` | 23552 | 42496 | −18944 | 43 |
| **未特化（部分）** | `fprintf(stderr,"plain\n")` | **4096** | 5632 | −1536 | 14 |

**今天不存在任何一档 LLVM 侧更大的情形。** 这与原稿的结论方向相反——原稿 21 个用例的总和是 LLVM 侧 229888 / goa 侧 366592（小 37%），而今天**逐档都是 LLVM 更小或相等**。原因是原稿那张表测的是"特化还没搬进 IR 前端"的状态；坑 35/36 修完之后，LLVM 侧不再有 printf 链的劣势。

IR 侧函数数印证特化生效（今天的实测）：

| 档 | define | 有 `printf_lite` | 有 `vfmt` |
| --- | --- | --- | --- |
| 空程序 | 2 | 否 | 否 |
| `printf("hello, world\n")`（字面量无 `%`） | 14 | 否 | **否** |
| `puts("hello")` | 17 | 否 | **否** |
| `printf("%d\n",3)` | 22 | **是** | 是（仅 `vfmt_i`） |
| `printf("%s\n","hi")` | 22 | **是** | 是（仅 `vfmt_i`） |
| `printf("%f\n",1.5)` | 30 | **是**（`_f`） | 是（仅 `vfmt_i`） |
| `printf("%%\n")` | 43 | **否** | **是（整套）** |

数字与机制一一对应：**14/17** 是"字面量无 `%` → 直接 `fwrite`，vfmt 完全不参与"；**22** 是"整数 lite → `printf_lite` + 只带整数分支的 `vfmt_i`"；**43** 是"退回普通 `printf` → 整套 `vfmt` + `double_to_buf` + `exp`/`log`/`frexp`/`ldexp`/`signbit` 全进来"。**43 那档的体积（23552）与 22 那档（5632）差 4 倍，而差的就是这 21 个函数。**

---

### 小结：三条可迁移的教训

1. **exe 大小几乎与"你写了多少代码"无关，只与"段的数量 × 段对齐粒度"有关。** 72 字节的表吃掉 1024、93 字节的 `.text` 占 4096，都是这条式子的直接推论。**省体积先数段，再调对齐，最后才裁代码**——顺序反了就是在第三性手段上花掉全部时间（坑 37 的教训），而第一性手段一个改动值 1536 字节。

2. **别把体积数字当结论记，要记"哪个机制让它变小"。** 同一份 hello world 的三个数字：16896（历史，未修）、5632（历史，对齐修复前）、4096（今天实测）。**它们会随修复反转**——今天 LLVM 侧比 goa 小 27%，而原稿记录的是大 2.5×。数字会过期，机制不会；只记数字的人会去优化一个早就修好的东西。

3. **凡是"判定"和"裁剪"都要问同一个对象、必须在编译期完成。** 特化问用户声明而不是运行时副本（`printfspec.go:22-25`）、特化在编译期决定而非运行期探测（`:46-49`）——这两条不是风格偏好，是可达性裁剪**能成立**的前提（坑 36）。同理，体积断言要断言**段表**而不是总大小（坑 37），否则任何一处改进都会掩盖另一处退化，而且**退化时不报警**。
## 六、AT&T 前端：六个翻译难点

goa 原本只吃 Intel/NASM 语法，而 LLVM 的 AsmPrinter 输出 AT&T/GAS，两者不兼容——早期只能让 libLLVM 直接吐 COFF。加了 AT&T 前端后（`attTranslate`（`src/goa/att.go:41`）逐行译成 goa 内部语法再喂既有 `processLine`，**x86 编码逻辑一行未改**），链条才真正打通。

六个难点**全部由 72 个真实样本暴露，不是推演**。但它们有同一个根：**x86 指令集是同一套，AT&T 只是另一种拼写法**。于是每一个坑都是"用 NASM 的直觉去写 AT&T"——而 NASM 直觉在下面六处系统性地失效。

### 坑 41：操作数方向不能一律翻转

AT&T 是**源在前**，goa/NASM 是**目标在前**（`src/goa/att.go:13-18`）。但"一律翻转"这条规则在四个方向上错得不一样，判据全在 `attFlips`（`src/goa/att.go:284`）里。

第一类，**尾随内存操作数就是目标**——AT&T 罕见地把目标放在末尾（因为源在左），所以这一对**顺序已经是对的，不能翻**。`addq %r14, 64(%r15)` 翻成 `add [r15+64], r14` 会被 goa 拒绝，而且拒绝得对：算术指令的 r/m 目标要走 `0F` 转义而不是短码（`src/goa/att.go:66-71`）。第二类，`mul`/`div`/`idiv`/`neg`/`not` 写死固定寄存器对（`src/goa/att.go:286`），单操作数在两种语法里同义，没有可重排的东西。

第三类必须翻，且只有这一类里的两种翻：纯存储 `mov` 与立即数。`movq %rax, -8(%rbp)` 若保持 AT&T 顺序，goa 会读成"从内存**加载**到 rax"，编码出完全不同的指令——**字节合法、不报错、语义全反**。立即数更物理：它没有自己的字段，只能跟在指令尾字节里，所以 `movl $x, 32(%rbp)` 必须变 `mov dword [rbp+32], x`（`src/goa/att.go:300-303`）。判据落在白名单 `attIsPureStore`（`src/goa/att.go:321`）——只有那十二个"纯写内存、不先读"的指令在翻转白名单里，`add`/`cmp` 配寄存器源因此天然不翻。

第四类是三操作数 `imul`，它是**轮转不是交换**（`src/goa/att.go:73-80`）：AT&T 写 `imul $imm, %src, %dst`，Intel 写 `imul %dst, %src, $imm`——目标在 AT&T 里是**最后一个**、在 Intel 里是**第一个**。两两交换会把立即值留在中间，编出一个不同的乘积。

> NASM 直觉在这里是负资产：Intel 那条 `imul dst,src,imm` 读顺了以后，会强烈暗示"两端对齐、中间不动"的通用规则，而真实规则是"循环移位"。
>
> **教训：语法翻译里最贵的错误不是报错，而是"翻了一半"的正确字节。**方向判定必须从操作数形态推出来，不能从助记符或固定规则推。

### 坑 42：内存操作数自带逗号

AT&T 的内存操作数是 `disp(base,index,scale)`——**逗号是寻址语法的一部分**。`8(%rax,%rbx,4), %eax` 这种两操作数行里naive `split(',')` 会切成 `8(%rax`、`%rbx`、`4)`、`%eax` 四片，寻址彻底丢失。

`attSplitOperands`（`src/goa/att.go:456`）是整个文件里**唯一一处真正的解析**，状态机有两个变量：`depth`（括号深度）和 `inStr`（当前引号字符，`0` 表示不在串里）。逗号只在 `depth == 0` 时才切（`src/goa/att.go:485-489`），进入 `'`/`"` 则置 `inStr` 且整个串内跳过所有语法字符，转义字符还要多跳一格（`src/goa/att.go:467-469`）。

第二个状态变量不是多余的：`.asciz "a,b"` 里的逗号在深度 0 处，光看括号会切开字符串字面量，GAS 的字符常量用的是同一套引号。函数头注释（`src/goa/att.go:448-455`）明写了这个四片撕裂的例子。

回归测试在 `src/goa/att_test.go:200` `TestATTOperandSplitting`，五个用例里有两个专门钉住这个：`-8(%rbp,%rbx,4), %eax` 必须保持两片，`$1, "a,b"` 必须保持带逗号的串。

> NASM 直觉在这里是"操作数用逗号分隔，逗号不是任何操作数的合法字符"——在 NASM 里这句话成立，在 AT&T 里正好是错的。
>
> **教训：分隔符的合法性取决于它所在的语法层。**只要输入语言里任何一个合法 token 能包含你的分隔符，naive split 就必须换成"跟踪嵌套状态"的切分。

### 坑 43：内存宽度只能从助记符后缀取

`decl -4(%rbp)` 是 32 位递减，但这个内存引用**本身不携带任何宽度信息**——寄存器操作数能从自己的名字推出宽度（`%eax` 就是 4 字节），纯位移寻址没有寄存器可看。唯一的线索是助记符后缀里的 `l`。

AT&T 语法里**没有** `dword` 这类尺寸前缀，所以宽度不能"贴"在操作数上，只能借 goa 自己的拼写夹带进去。机制分两步：先把后缀宽度翻译成 **NASM 尺寸关键字**——`attSizeKeywords`（`src/goa/att.go:115`）把字节数映射成 goa 解析器认识的形式，4 字节是 `"dword "`（注意带尾空格，`src/goa/att.go:119`）；然后只在**操作数确实是内存、且还没带尺寸前缀**时才贴上去（`src/goa/att.go:104`），否则会重复前缀。`hasSizeKeyword`（`src/goa/att.go:133`）是这个"还没有"守卫。

最后由 goa 既有的解析器接住：`parseOperand` 剥掉 `[...]` 后把尺寸关键字落到 `o.memWidth`（`src/goa/asm.go:1063`）。**AT&T 前端没有新增任何宽度表示**，只是把信息搬进 goa 早就会处理的字段。注释（`src/goa/att.go:85-90`）说明了后果：不贴的话编码器默认 64 位并发一个多余的 `REX.W`。

注意 `attSizeKeywords`（`src/goa/att.go:115`）是**按宽度索引**的数组而非 map，条目 0 空置、列表走到 8（`qword`，`src/goa/att.go:116-120`）——索引即宽度，所以查表前必须先有 `width > 0` 的判断（`src/goa/att.go:94-96`）。

> NASM 直觉是"内存操作数在括号里写全宽度"（`mov dword [rbp-4], 1` 显式无歧义），所以移植时会以为 AT&T 也有等价物——它有，但是**长在助记符上**。
>
> **教训：跨语法搬运信息前先问"这条信息在新语法里长在哪里"。**同一个语义属性（宽度）在两种语法里的载体可以完全不同，找不到就得显式搭桥。

### 坑 44：`movq`/`movd` 的双重含义

同一个 token 在 AT&T 里指两条不同的指令：`movq %rsi, %rcx` 是 64 位**通用寄存器**mov；`movq %xmm0, %rax` 是 SSE quadword 在 xmm 与 gpr 之间搬运。同一个 `movq`，走**完全不同的编码器**（`encodeMov` vs SSE mov），字节毫无相似之处。

名字本身无法区分，所以判据必须落到**操作数列表**上：`attMnemonic`（`src/goa/att.go:183`）先查 `attAmbiguous`（`src/goa/att.go:261`，只含 `movq`/`movd`），命中后再问 `attHasXMM`（`src/goa/att.go:249`）——任一操作数（去掉 `%` 后）以 `xmm` 开头就判 SSE，返回原名并报宽度 8；否则归化成 `mov`，宽度 8 但由寄存器自己定（`src/goa/att.go:189-194`）。顺序不能反：先剥后缀就会把 `movq` 读成"宽度 8 的 mov"，SSE 那一路直接被吃掉。

顺带 `movabsq` 也是同一类"名字带尺寸"的坑，但它简单：goa 没有 `movabs`，`attCanonical`（`src/goa/att.go:241`）把它归一成 `mov`，因为 `encodeMov` 本来就从目标寄存器推宽度，imm32 符号扩展或完整 imm64 都对。

这里能看出 `attMnemonic` 里的**顺序**就是全部难点：先查 `attNoOps`（零操作数）、再查 `attAmbiguous`（歧义）、再查 `attIsSSEMnemonic`（SSE 整族），最后才进常规剥后缀循环（`src/goa/att.go:184-202`）。任何一步提前都会吃掉后面某类指令。

> NASM 直觉是"`movq` 就是 64 位 mov"——在 NASM 里 `movq %xmm0, %rax` 根本不合法，所以这个歧义永远不会浮现。AT&T 让它合法了。
>
> **教训：引入歧义的是语法共享 token 空间，不是指令集。**翻译层必须保留原始 token 完整信息直到判据可用完，不能提前归一。

### 坑 45：GAS 无操作数惯用语按**源**宽度命名

`cqto` 看着像 `cdq` 加个 `o`，直接按字面译过去就错：GAS 给这类惯用语起名时按**源操作数**的宽度，而不是结果宽度。`cqto` 把 eax 符号扩展进 `edx:eax`，**结果落在 rdx:eax 共 64 位**，所以它对应 goa 的 `cqo`；译成 32 位的 `cdq` 只写 eax，`rdx` 高半段**未定义**——而后续几乎总有一条 `mov %rdx, %rax` 把它当完整 64 位读，于是错误在很后面才炸，且炸得莫名其妙。

映射表 `attNoOps`（`src/goa/att.go:175-181`）把五条全部按源宽度对齐：`cqto`→`cqo`、`cwtq`→`cwde`（ax 拓宽到 eax）、`cltq`→`cdqe`（eax 拓宽到 rax）、`cltd`→`cdq`、`cwtd`→`cwd`。这张表只在**零操作数**分支生效（`src/goa/att.go:184-188`）——有操作数时 `attMnemonic` 继续走常规路径。

`attMnemonics`（`src/goa/att.go:408`）把 `cqo`/`cdq`/`cwde`/`cdqe`/`cwd`/`cwtq`/`cltq`/`cltd` 全收成合法助记符，就是为了这条映射出来的名字还能被后面的编码器接住。注释（`src/goa/att.go:169-174`）把"`cqto` 当 32 位 `cdq` 会让 rdx 高半段未定义"写在了表上方。

这张表还有个副作用值得注意：`cwtq`/`cltq`/`cltd` 这些 **GAS 名字本身也进了 `attMnemonics`**（`src/goa/att.go:417-418`），所以即使零操作数分支没命中，它们仍能穿过剥后缀循环、落到末尾的 `return raw, 0`（`src/goa/att.go:223`）保持原样——不会因为"词干不认识"被误改成别的指令。

> NASM 直觉是"助记符里的字母是操作数宽度"——`cqo` 在 NASM 里就真是 64 位，于是读 `cqto` 时会顺手当成"64 位的某种 cdq"，正好把最关键的语义信息丢掉。
>
> **教训：起名约定不跨工具链。**同一个后缀字母在一侧描述源、在另一侧描述结果时，任何"看词干猜语义"的规则都得改成查表。

### 坑 46：dot 标签有两种作用域

`attIsBlockLabel`（`src/goa/attdirective.go:254`）的判据只有三个前缀：`LBB` / `Ltmp` / **`Lfunc`**（`src/goa/attdirective.go:259-261`）。别漏第三个——LLVM 的 `.Lfunc_begin0` 是函数作用域，漏了它就会去按文件级处理。

goa 的 `qualify`（`src/goa/asm.go:282-293`）原本给**所有** dot 名拼上最近的全局标签名，这对为手写 NASM 建的假设是对的，对 LLVM 输出是错的：`.str.0` / `.LCPI0_3` / `.Lswitch` 是**文件级私有名且被十几个函数共用**，逐函数加前缀会让第二次引用就悬空。所以 AT&T 模式下它改问 `attIsBlockLabel`——是块标签才加前缀，否则原样返回（`src/goa/asm.go:286-291`）。这个分流是双向的：`isLocalLabel` 只认 `.` 前缀（`src/goa/asm.go:269`），而 AT&T 前端里**全部** 7 处符号引用都要过 `qualify`，一处漏掉就静默串标签。

配套的是 `label`（`src/goa/attdirective.go:265`）的**早退**结构：块标签走 `attIsBlockLabel` 分支立刻 `defineSym` 并返回（`:270-273`），**不碰 `curGlobal`**；只有非 dot 标签才更新 `curGlobal` 并关掉上一函数的 unwind 记录（`:274-279`）。这个顺序是必须的——若块标签也去更新 `curGlobal`，函数内标签就会把自己当成新函数，后续所有块标签前缀都错。

> **现状要说清楚：这个洞在 PE 路径上今天依然没修干净。**`defineSym`（`src/goa/asm.go:512`）遇到重名只做"last wins"，唯一的例外判断（`src/goa/asm.go:519-523`）**仅限 ELF 目标下、且该名字是已登记的系统调用符号**——为了让 `call exit` 总是进内核桩，而不是被 goclib 的 `void exit(int)` 抢走。PE 路径没有任何这样的保留逻辑。也就是说，**`qualify()` 是唯一的防线**；它漏掉一个前缀（比如把 `Lfunc` 漏了），同名 dot 标签就会跨函数互相覆盖，而且不会有任何报错。
>
> NASM 直觉是"点号开头 = 局部标签"，这条直觉在这里同时**过于宽**（会误伤文件级共享名）和**过于窄**（`Lfunc` 不在直觉里）。
>
> **教训：把命名空间的正确性押在单个函数上时，这个函数就是单点故障。**哪怕当下测试全绿，也要显式记录"哪些类别还缺保护"。

### 坑 47：跳转表重定位需要一对符号

72 个真实 LLVM 样本只有 **4 个**能链成 PE，其余 68 个全卡在同一个 token 上：`.long .LBB29_14-.LJTI29_0`——switch 跳转表的一项，含义是"本 case 目标相对**表基**的偏移"。

难点在于两个标签**通常分居不同段**（case 目标在 `.text`，表在 `.rdata`），汇编期无法化简，只有两个地址都定下来才知道值。而 goa 的 `Fixup` **一处只记一个符号**（`sym`），表达不了成对重定位——它退化成"一个名字里带减号的符号"，链接期报 undefined。

修法是给 `Fixup` 加 `sym2` 携带被减数，注释（`src/goa/asm.go:122-133`）说得很直接：这个重定位本质是**针对一对符号的 REL32**，任何单符号字段都表达不了，所以减法由链接器代做。`applyFixup` 在 `sym2` 非空时直接写 `sym - sym2` 而不是"相对字段所在处"（`src/goa/asm.go:577-588`）；PE/ELF 两侧对称解析（`src/gocld/pe.go:353-357`、`src/gocld/elf.go:135-139`），共用同一个 `applyFixup`（`src/gocld/fixup.go:48-58`）。assemble 侧由 `attSplitSymDiff`（`src/goa/attdirective.go:586`）识别 `A-B`。

**切分的真实理由不是"减号不出现"**，而是：从**右**用 `LastIndex` 扫（`src/goa/attdirective.go:589`），避免前导 `-`（负常数）被当成分隔符；扫完再用 `isSymName` **双侧校验**（`src/goa/attdirective.go:594`），任一侧不像符号名就放弃。比"符号名不含减号"这个单向理由稳得多——它同时挡住了 `-8`、`.L1-` 和 `.L-1` 这几种边角。

> 72 这个数字要看清楚：**样本是生成物，不在仓库里**。`src/goa/testdata/att/` 今天只有 4 个 `.s`（`intprint.s`、`longmin.s`、`print_thin.s`、`variadic.s`），`att_e2e_test.go:30` 全量 glob 并在空集时 `t.Skip("no AT&T samples -- run: bash tools/gen-att-samples.sh")`（`:35`）。72 样本由 `tools/gen-att-samples.sh` 从 `examples/*.c` 生成，一次性测量后没有提交。复现：`bash tools/gen-att-samples.sh`。
>
> **教训：遇到"只有特定输入才能复现"的 bug，先把输入的可获得性写进文档。**否则半年后没人能验证那条 72/72 的结论，测试还是绿的（它 skip 了）。

### 坑 48：验证方法——必须真跑

合成 6 路 switch，链成 PE 后用 `tools/peun.py` 在 Unicorn 里**真跑**，每个 case 累加不同权重（1/10/100/1000/10000/100000），退出码即总和。

**这一条必需**：表项算错时字节仍合法、镜像仍能加载，**只有真正执行才暴露**（跳进无关代码或直接 trap）。这个道理写在测试的文档注释里（`src/goa/att_jumptable_test.go:16-26`）。测试本体在 `src/goa/att_jumptable_test.go:27`，跑 `runPEUnderUnicorn`（定义在 `:116`，调用在 `:100`），退出码比对在 `:101-103`（`111111 % 256`）。`runPEUnderUnicorn` 还做了容错：进程非零退出在这里是**数据**（客体的退出码），只有起不来或 Python traceback 才算失败（`src/goa/att_jumptable_test.go:132-141`）。

样本是**手写 AT&T** 而非让 LLVM 生成，注释（`src/goa/att_jumptable_test.go:31-33`）说明了原因：要复现 LLVM 发射的那种确切形状。取表项那一步也必须是 `movslq`（`:54`）——偏移可以是负的，符号扩展漏了就会跳到表前。

跑不动的环境也不含糊：`runPEUnderUnicorn` 在找不到 `tools/peun.py`（`:28-30`）或没有 python3（`:122-125`）时 **skip 而不是 fail**；但注释（`:112-115`）保证下面的汇编级断言覆盖了同一套重定位算术，所以 skip 不会让行为失去验证。

> 更正原稿一处归因：曾写"`peun.py` 对 LLVM 产物**不可用**（栈模拟不建映射）"。源码不支持这个说法——`tools/peun.py:137-138` 明确**建了栈映射**（`mem_map(STACK_TOP - STACK_SIZE, ...)`）并在 `:139` 设 `RSP`，全文也没有任何区分"LLVM 产物 / goa 产物"的分支。
>
> 另一条读源码才发现的坑：`ExitProcess` 在 Unicorn 里取的是 **RCX** 不是 RAX（`src/goa/att_jumptable_test.go:79` 的注释、`:80` 的 `movl %r15d, %ecx`）。比对退出码时用错寄存器，会在表项**完全正确**的情况下得出"表算错了"的假结论——这类假阴性比假阳性更费时间。
>
> **教训：验证手段的强度必须匹配缺陷的可见性。**缺陷在字节层不可见（表项偏移算错仍是合法 4 字节、镜像照样能加载）时，静态断言和"能加载"都是假安慰，只能执行。顺手也提醒：**先确认测试桩自己的约定**，否则你会追一个不存在的 bug。

### 坑 49：AT&T 路径的四个静默 bug

四个都**静默**：不报错，或者报一个指向别处的错。

1. **段名解析把反斜杠混进字符集**。原来用 `IndexAny(nm, "\",")` 截引号——那个字符串是 `"`、`\`、`"` 三个字符，**反斜杠在 Windows CRLF 的 `\r` 面前先命中**，`.section .rdata,"dr"` 被截成 `dr`，再退化成一个名为 `dr` 的匿名数据段（表被放错地方）。现已改成只按双引号切 `IndexByte`（`src/goa/attdirective.go:356`）+ `TrimRight` 去尾逗号（`:359`），**且 `attNamedSection`（`:461`）又截一次**（`:467`）——注释（`:462-466`）明写理由："在调用点之外也裁一次，是为了让保证保持局部：不管段名从哪条路进来，都没法把分隔符夹带进镜像。"
2. **MSVC 栈探测缺失**。LLVM 把超过一页的栈帧降级成 `call ___chkstk_ms`（**三个下划线**，x64 MSVC ABI 名），goc 镜像不链 CRT，必然链接失败。现已实现在 `src/gocld/coffmerge.go:161-191`。注意 `:156-159` 记着前几版为什么错：helper **只能**碰保护页（探测），且必须让 `rsp` 和 `rax` 都保持不变——`rax` 里还留着帧大小给调用方紧跟的 `sub rsp,rax`。之前的实现要么读 `rcx`（调用方从不设置，探测按垃圾尺寸走栈），要么自己减页（双重分配），都以 `STATUS_STACK_OVERFLOW` 收场。
3. **分号注释未剥离**。GAS 用 `#`（LLVM 也发 `#`），但手写汇编常用 `;`；不剥离时行尾注释被当成后续操作符，报错莫名其妙。`stripATTComment`（`src/goa/attdirective.go:117`）的 `;` 分支（`:136-148`）**只在行首或空白之后**才当注释——这样既避开 `.def name;` 里的 GAS 语句终止符，也避开字符串内的数据；同时还支持 `//`（`:150-153`）。这段的双状态机（`inStr`）和坑 42 是同一份逻辑。
4. **无点 `section`/`global` 掉进指令流**。入口桩自然写成 goa 语法 `section .rdata,"dr"`，而 `head` 的 `.` 前缀判断（`src/goa/attdirective.go:202`）会把它拦掉 → 落到指令路径 → 属性串被当操作数 → 凭空冒出一个叫 `"dr"` 的段。修法是在指令路径之前加一张 `section`/`global`/`globl` 的转发表（`src/goa/attdirective.go:215-217`）；`extern` 不在表里，因为 pass 1 已经先吃掉了所有 `extern` 行（`:30-36`）。

> NASM 直觉在第 1 条上尤其坑：手写 NASM 从不手写 CRLF 从不与引号混战，所以这个字符集 bug 在 NASM 侧**不可想象**，一读源码只会觉得"`IndexAny` 用得很自然"。
>
> **教训：字符集常量是跨输入源的隐性假设。**任何 `IndexAny`/`Trim(x, cutset)` 都要问一句"我的输入真的只有我规定的那些字符吗"——同一段代码在 LF 与 CRLF、在自造输入与真实输入下行为不同。

### 坑 50：两份实现只会互相遮蔽

为 `___chkstk_ms` 新建了 `src/goa/winstack.go`，后来发现 **COFF 合并路径（`src/gocld/coffmerge.go`）早已正确实现**，且**带 16 字节对齐填充**——缺了对齐会让从 COFF 合并进来的每个符号落位偏移，入口桩调 `main` 会跳到函数**前面**的对齐字节上立刻 fault。删掉重复实现后实测 8192 字节栈帧的函数 `rc=0`，证明那条路径本来就是好的。

注释（`src/gocld/coffmerge.go:148-160`）把契约写得很清楚：helper **只能**碰保护页（探测），且必须让 `rsp` 和 `rax` 都保持不变——`rax` 里还留着帧大小给调用方紧跟的 `sub rsp,rax`。之前几版实现的两种错法都记在那里：读 `rcx`（调用方从不设置，探测按垃圾尺寸走栈），或自己减页（双重分配），都以 `STATUS_STACK_OVERFLOW`（`0xC00000FD`）收场。

对照 goc 自己的 codegen 就更清楚为什么它是纯补齐：`src/goc/codegen.go:6105-6115` 里超过一页的帧是**内联展开**的探测循环（`mov r11,N` + 逐页 `sub rsp,4096` / `mov rax,[rsp]` / `jg`，末尾 `sub rsp,r11` 补回多减的量），压根不调用 `___chkstk_ms`。只有 LLVM 后端会把这个 helper 当成外部符号发出来。

> 行号已漂移：当前实现是 **`src/gocld/coffmerge.go:161-191`**（原稿写的 `122` 是写文时的旧值；文件在 `src/gocld/` 而非 `src/goa/`）。删除 `winstack.go` 的 commit 是 `9621d75`，该文件今天确已不存在。顺带当前实现用 **r11/r10** 作游标（`:166-169`）而非 rcx，探测块 **18 字节**（`sub rsp,4096` 7 + `mov [rsp],r11` 4 + `sub r11,4096` 7，`:172-181`）——游标必须按实际追加字节数推进，否则符号全部提前落位；`jg loop` 的 rel8 偏移也得算而不能写死（`:182-186`）。

**教训：加"发射某符号"的代码前，先 grep 同名符号的其它发射点。** 两份实现不会二选一地生效——先定义的那份静默赢，另一份看起来"在跑"却永远不执行；更糟的是它会让人以为路径已覆盖。

> 顺带一个测试方法上的要点：`att_test.go` 用**字节级等价对照**——同程序分别用两种语法写，要求 `.text` 完全相同，且 **Intel 一侧手写而非从前端导出**，否则测试是同义反复（拿同一份输出的两个副本互相对比，永远相等）。
>
> **教训：等价性测试的两侧必须真正独立。**从被测对象导出期望值，等于把被测对象的错误同时写进了期望。

## 七、线程局部存储：表象是"递归坏了"，根因完全不同

这是最近才修的一个，值得单开一节——因为**它是我判断错了一次的方向**。

这一节的价值不在"怎么修"（修法很短），而在**"我是怎么错的"**。坑 51 是一次完整的误诊：现象指向递归，实际坏在 TLS。误诊本身比修复更值得记录，因为它的排查路径里藏着一个可复用的方法。

> **时态说明**：坑 51 和坑 52 描述的是**修复前**的现象，两者都已由提交 `28a8d72`（2026-10-07 02:08:26 +0800，「gocl: 支持线程局部存储（`_Thread_local`/`__thread`）」）修好。`bench/nim/BENCHMARK.md:6` 现在记的是 gocl **全部 5/5** 个内核与 gcc 逐位一致，并写明"此前 fib 失败，根因是 gocl 的线程局部存储（TLS）支持缺失，已修；**与递归无关**"。下面保留原始排查过程，因为它本身是有价值的记录。

### 坑 51：Nim 生成的 fib 递归在 LLVM 后端静默无输出（**修复前的现象**）

**现象**：gocl 跑 Nim 生成的 C 递归（fib）**静默无输出**，同一次构建的 goc 正确输出 `fib 317811`。第一反应——"递归代码生成有问题"。

**排查**：只能逐步隔离，因为没有任何报错：

| 用例 | goc | gocl（修前） |
| --- | --- | --- |
| 32 位 `fib(30)` | 832040 | 832040 ✓ |
| 64 位 `long long fib(30)` | 832040 | 832040 ✓ |
| 尾递归 `sum(100)` | 5050 | 5050 ✓ |
| goto 早返 + 调用间错误检查的递归 | 317811 | 317811 ✓ |
| **递归中读一个 TLS 变量** | 832040 | **0** ✗ |
| **非递归读一个 TLS 变量** | **5** | **1073754112** ✗ |

上面四行"纯 C 递归"全部正确这一事实，已固化在 `bench/nim/BENCHMARK.md:51`。

**转折点**：**"纯 C 递归全部正确"这个负面结果是整次排查的支点**。它一次性排除了 codegen 的递归路径——递归怎么生成的是对的、尾递归是对的、早返回是对的、64 位是对的、调用约定修饰（`N_NIMCALL` 在 x64 上是无修饰）也不对不上。于是怀疑对象被迫收缩到一个很窄的地方：**"递归中恰好会读的那个东西"**。而表里最后两行正好指着它。

**为什么 Nim 会触发**：一旦怀疑对象收缩到"读某个东西"，机制就很好推了。Nim 每个递归调用后执行 `if (NIM_UNLIKELY(*nimErr_))`，读一个 **TLS 错误标志 `nimInErrorMode`**；gocl 把这次读生成成垃圾 → 误判为真 → **提前返回 0 / 静默无输出**。整棵递归塌成 0。这段机制今天记在 `bench/nim/BENCHMARK.md:52`。

**根因**：gocl 缺 TLS 支持，与递归无关。最小复现把"递归"整个去掉也照样坏：`_Thread_local int g_x = 5; int readx(void){ return g_x; }` → gocl 返回 **1073754112**（垃圾），而 `5` 正是这个变量该有的值。递归只是**把一个已经存在的坏值放大成了可见症状**。

> **教训**：当"某类代码全对、某类代码全错"时，负面结果比正面结果信息量更大——它能一次性砍掉一整条怀疑路径。所以排查应该**从最容易证伪的假设开始**，并且刻意去找那个"如果假设成立、这里应该全错"的对照组。我这次的错误不在于没测递归，而在于**测完递归正确后还留着递归这个假设**，没有立刻去想"那到底哪一行代码只在这类程序里才出现"。

### 坑 52：为什么 gocl 的 TLS 是坏的（**修复前的两层原因**）

**现象**：坑 51 的那个最小复现，任何 `_Thread_local` 读取都返回垃圾。同一段 C 在 goc（原生后端）下完全正确——**原生后端 TLS 是好的**，所以这是 gocl 独有的两层叠加缺陷。

**第一层（IR 侧）**：把 TLS 全局当普通 `extern` 全局（`noteExternGlobal`），LLVM 于是生成 `load @G_x`——**直接读静态模板地址，完全绕过 Windows `gs:0x58` / Linux `fs` 的 per-thread 机制**。模板地址对每个线程都读出同一份初值，而 `.tls` 段此刻根本不存在，读到的只能是镜像里恰好残留的字节。修法是在 `noteExternGlobal` **之前**就跳过 TLS 全局：`src/gocl/translate.go:177-179`（`if gl.IsTLS { continue }`，注释在 `:173-176`，理由写得很直接："绝不作为直接全局，所以这里不声明 IR 符号"）。运行时全局走同一套，见 `src/gocl/translate.go:502-506`。

**第二层（链接侧）**：`linkData` 里 TLS 全局被 `continue` **跳过了**，所以 `Data.TLSVars` 是空的 → 链接器根本没铺 `.tls` 段、没建 TLS 目录、没定义 `G_goc_tls_index`。注意这一层是**独立于 IR 侧的第二处缺陷**：就算 IR 侧改对了，只要 `TLSVars` 还是空的，`.tls` 段就不会出现在镜像里（`src/common/link/emit.go:315` 的铺段与 `src/gocld/pe.go:388-400` 的 TLS 目录都以 `len(d.TLSVars) > 0` 为前提）。现在 `src/gocl/cmd/gocl/main.go:239` 已填上 `TLSVars: gocl.ComputeTLSLayout(prog, common.Store(cfg.Linux), cfg.Linux)`，其上方 `:233-238` 的注释点明了"访问代码和段存储在每个偏移上都一致"这件事。

**为什么两层要一起修**：只修 IR 侧，得到的是"访问代码正确、存储缺失"；只修链接侧，得到的是"存储正确、访问代码还在读模板地址"。任一单独修都不会让最小复现变对。这两层在提交信息里也是分开写的两条。

> **教训**：一个"读出来是垃圾值"的缺陷，往往**同一件事在多个层上各错一次**。只验证其中一层修好了，很容易得出"已经修好"的错误结论。判断修没修好，要用**那个最小复现**，不要用任何间接证据。

### 坑 53：修法——复用原生后端已有的设施，不新造一套

**关键认知**：**原生后端早就把 TLS 做完整了**——codegen 手写访问序列（`src/goc/codegen.go:7850` 的 `tlsPlace`、`:963` 的 `genTLSAddr`）、`emit.go` 铺 `.tls` 段与 `G_goc_tls_index`（`src/common/link/emit.go:306-317`、`:315-322`）、`pe.go` 建 PE TLS 目录（`src/gocld/pe.go:382-400`）。所以 gocl 不需要新造一套机制，只需要**按同一套布局产出访问代码**，再把段和目录交回链接器。

**第一处，`ComputeTLSLayout`（`src/gocl/translate.go:531-562`）**：按原生 `tlsPlace` 规则给每个 TLS 全局分配 `.tls` 偏移。规则是 **8 字节对齐**（`:540` 的 `off = (off+7) &^ 7`，注意不是 4）+ `link.TLSAlignedSize` 的槽宽（`src/common/link/global.go:94` 包出的 `tlsAlignedSize`，`:76-89`；最小 8 字节）。同名变量——用户程序和 C 运行时可能都有——**只分配一次**，靠 `seen` map（`:533` 声明，`:536-539` 查重）加后续的遍历两段（`:549-553` 用户程序、`:554-560` 运行时）。这个"只分配一次"不是洁癖：如果同名分配了两个偏移，IR 和链接器会各挑一个，变量就会安静地读错位置。

**第二处，链接侧用同一个函数**：`src/gocl/cmd/gocl/main.go:239` 的 `TLSVars` 填的就是同一个 `ComputeTLSLayout` 调用结果。

**第三处，访问路径改经 helper**：`src/gocl/expression.go:475` 发 `call ptr @__goc_tls_slot(i64 %d)`，`src/gocl/module.go:188` 声明 `declare ptr @__goc_tls_slot(i64)`，lvalue 侧在 `src/gocl/call.go:23-25`（`if off, ok := e.c.tlsOffset(n.Name); ok { return e.tlsAddr(off) }`）。

**第四处，汇编进入口桩**：`src/common/link/emit.go:182-207`，在 `len(d.TLSVars) > 0` 时发射。

**真实序列**（寄存器以源码为准，原文写错过一次）：

```
; Windows x64 —— 索引用 edx，TEB 指针取到 rcx，末步是 add 不是 lea
mov rax, rcx                    ; 取传入的偏移（Win64 第二参在 rcx）
mov edx, [rip+G_goc_tls_index]  ; 模块 TLS 索引
xor ecx, ecx
mov rcx, gs:[rcx+0x58]          ; TEB.ThreadLocalStoragePointer
mov rcx, [rcx+rdx*8]            ; 按索引取槽
add rax, rcx
ret
```

对应 `src/common/link/emit.go:199-205`，注释在 `:195-198`——它明确说这是"native 后端 `genTLSAddr` 生成的同一序列"，所以两侧行为一致不是巧合。

```
; Linux x86-64 —— 不是裸 lea
mov rax, rdi                    ; 取传入的偏移（SysV 第一参在 rdi）
lea rdx, [rip+__tls_start]
add rax, rdx
ret
```

对应 `:190-193`，注释 `:185-189` 交代了为什么 Linux 侧可以这么简（Linux 上 `.tls` 就是唯一那份，多线程下用 `fs` 基址，所以地址就是"段基 + 偏移"）。

**三个必须注意的点**：

- **`noteExternGlobal` 是陷阱**：把 TLS 声明成 extern 全局就直接退化成"读模板地址"，而且**编译期不报错、链接期不报错、运行期只是读出垃圾**。必须 `continue` 掉。
- **IR 访问偏移与链接器 `.tls` 布局必须用同一个函数算**，否则逐条错位。实际有**三个**消费点共用它，不止两个：IR 访问（`translate.go` 把结果记进 `irMod.tlsOffsets`，字段声明在 `src/gocl/module.go:71-76`）、链接布局（`main.go:239`）、emit 侧符号解析（`src/gocl/module.go:737-738` 的 `tlsOffset` 注释直接写"这个偏移正是 `ComputeTLSLayout` 分配的，和链接器的 `.tls` 镜像用的是同一个，所以 IR 的访问和段的存储不可能漂移"）。
- **`__goc_tls_slot` 是双后端共用定义，不是 gocl 专属**。它住在共享的 `src/common/link/emit.go:175-181` 的注释里——原文是："原生后端不调用它，但多一个未使用的定义不花什么代价，而且让两个桩保持完全一致。"

**结果**：8 文件 +181/−27，与 `git show --stat 28a8d72` 精确一致（`bench/nim/BENCHMARK.md` / `src/common/link/emit.go` / `src/goa/asm.go` / `src/gocl/call.go` / `src/gocl/cmd/gocl/main.go` / `src/gocl/expression.go` / `src/gocl/module.go` / `src/gocl/translate.go`）。Nim 五个内核 gocl 从 4/5 变 **5/5**。提交信息自证两条：`asm.go` 的改动仅为 gofmt 对齐（我复核了 `git show -w 28a8d72 -- src/goa/asm.go`，只剩提交信息、没有 diff hunk，确实是纯空白差异）；"gocregress 476 ok / 30 fail，失败集合与修复前逐条相同"——那 30 个 fail 是本机 Windows 跑不了的 Linux 腿既有问题，不是 TLS 引入的。

> **一个遗留缺口（坑修好了，但回归没入库）**：提交信息说验证过 `readtls`（非递归读）、`fibtls`（递归中读 TLS）、`tlstest`（int/long long/double 多类型 + 写入回读）、`tlsaddr`（取地址写回）四类用例，`bench/nim/BENCHMARK.md:61` 也记了这四个名字。但我 grep 全仓库，这四个标识**只命中 `BENCHMARK.md:61` 这一处**——源码树里既没有对应的 `.c` 用例，也没有 Go 测试（`src/gocl/link_e2e_test.go` 只有 3 个测试函数，全仓库唯一的 TLS C 用例是 `src/examples/tls_basic.c`，且它测的是原生后端）。这批用例要么当时是临时文件，要么已删除。
>
> 也就是说：**这四类里最该长期守住的两类——多类型混合、取地址后写回——现在完全没有回归保护**。任何人重构 `ComputeTLSLayout` 或 `__goc_tls_slot`，不会有任何测试变红。这是一个真实的覆盖缺口，值得补进 `tests/`。
>
> **教训**：修一个缺陷时用过的临时用例，如果不入库，就等于**这个缺陷在回归意义上从未被修过**——它只是在你手边被修好了。判断"修完了"和"修完了并且以后不会回来"是两件事，前者靠提交信息自证，后者只能靠入库的测试。

## 八、Nim 生成的 C

这一章的两个坑都不是后端问题，而是**前端常量折叠能力不足**和**运行时覆盖率不足**。把它们放在一起看很清楚：一个 C 后端能不能吃 Nim 生成的代码，取决于两件与"后端"无关的事——**能不能折出静态初始化器里的常量**，和**goclib 有没有拉进来的那批运行时函数**。

### 坑 54：`NIM_STRLIT_FLAG` 被静态初始化器折成 0

**现象**：`echo 3.5` 在**goc 和 gocl 两个后端下都段错误**。一个只打印浮点数的程序崩溃，这个症状看起来完全不像编译器的 bug——所以第一反应是查运行时。

**触发链**：Nim 的字符串字面量标志是 `NIM_STRLIT_FLAG = ((NU)(1) << 62)`，出现在每个字符串字面量的静态初始化器里，形如

```c
static const struct{ NI cap; ... } TM = { 0 | NIM_STRLIT_FLAG, "" };
```

`setLengthStrV2` 据此判断这个字符串需不需要新分配。而 `echo 3.5` 走的就是 float→string 转换，会碰这个结构。

**根因**：`foldConstInit`（`src/common/link/global.go:195`）**不处理 `CastExpr`**。`((NU)(1) << 62)` 的外层是一个类型转换，转换本身是合法常量表达式，但 folder 认不出来 → 返回 `ok=false` → **静态初始化器被写成 0** → `setLengthStrV2` 看到 `cap == 0`，误判需要重新分配 → 对一个本该是常量只读的字符串做重分配 → 段错误。

这条链上**没有任何一环会报错**：语法合法、类型合法、静态初始化器合法（0 也是合法的 `NI`），崩溃发生在运行期深处，跟"编译期常量折叠漏了一种表达式"这件事隔了很远。

**定位方法**：最小复现证明 `((NU)(1) << 62)` 在 goc 下 cap 被算成 0、gcc 下正确。这一步是决定性的——**同一个表达式两个编译器结果不同，就必然是编译器侧的常量折叠问题**，不可能是运行时或 Nim 语义问题。

**修法**：`foldConstInit` 补上四种折叠：

- **一元 `!`**（`global.go:221-222`，`case "!": return boolVal(v == 0), true`）；
- **三元 `CondExpr`**（`:224-233`）——条件本身也递归折叠，`c != 0` 取 `Then` 否则取 `Else`；
- **`CastExpr`**（`:234-246`）——新增，重心在这一支；
- **`_Bool` / `_BitInt` 截断**——见下。

**`CastExpr` 比"加个 CastExpr 分支"复杂得多**，这是这次修复里最值得记的部分。新增的 `foldCastInt`（`global.go:329-356`）要按 C 的整数转换规则分别处理不同目标类型：

- `KInt` → `truncInt(v, t.Width, t.Signed)`，按字节宽度截断/符号扩展；
- `KBitInt` → 按**位宽**而非字节宽掩码（`:343-349`），并对 `t.Bits <= 0 || t.Bits > 64` 拒绝（`:337-339`）；
- `KBool` → `boolVal(v != 0)`（`:350-351`），即 C 的"转 `_Bool` 就是看是否非 0"；
- **`default: return v, true`（`:352-354`）**——这是原稿没写的一点：目标类型是**指针或其他非整数类型**时**保留原位模式**，注释写明"这样 `(void*)0` 空指针还是 0"。也就是说 `foldCastInt` 并不只处理整数，它对非整数目标是**故意透传**的，不是"没考虑到"。

**两个边界值得单独记**：`t == nil` 时返回 `(0, false)`（`:330-332`），这是**整个 folder 唯一会"放弃"的情形**——目标类型缺失时它无法推断该按什么规则折。其余情况都返回 `ok=true`。以及 `truncInt`（`:360-371`）对 `width <= 0 || width >= 8`（`:361-363`）直接透传：宽度未知或已经是 64 位时，截断是无意义的操作。

**最硬的一条证据在源码注释里**：`global.go:239-241` 直接点名 `NIM_STRLIT_FLAG`——"没有这个分支，`((NU)(1) << 62)`，即 Nim 的 `NIM_STRLIT_FLAG` 定义，折不出来，它的静态初始化器被发射成 0，这破坏了 Nim 的每一次 float→string 转换"。**注释里写着一个具体宏名和一个具体失效范围，这比任何转述都可靠。**

**为什么这条与后端无关**：`foldConstInit` 住在共享的 `common/link/global.go`，goc 和 gocl 用的是**同一份**。所以一个缺陷同时打中两个后端，不是因为"两个后端犯了同样的错"，而是因为**它们共用同一个折器**。这也解释了为什么现象是"两个后端都在同一个地方段错误"——同一个 bug 的同一处代码。

> **教训**：`echo 3.5` 段错误的直觉归因是"运行时/格式化有问题"，实际在**编译期的常量折叠**。折算能力的边界往往比语言规范窄得多——一个语法完全合法的常量表达式，folder 认不出来时不会报错，只是安静地返回"算不出"，而调用方把这个"算不出"翻译成了"值是 0"。**"`ok=false` 被下游当成 0"是常量折叠类缺陷最危险的传播方式**：一个诚实的"我不知道"被静默降级成了一个错误的确定值。写这类代码时，`ok=false` 必须一路传到能报错的地方，不能在中途变成 0。

### 坑 55：goclib 缺 Nim 运行时函数

**现象**：完整 `bench.nim`（含 `std/strutils`）**拉进 `resize__system_u3063`、`eqdestroy___system_u3728`** 等 Nim 运行时函数，goclib 尚未实现 → 链接失败，暂不可编。

**这是运行时覆盖率的事，与后端无关**，所以基准改用 5 个内核（sieve / fib / intloop / strbuild / float）。这一判断是对的：缺的是 `goclib` 的函数（`src/goclib/` 下 20 个 `.c`，grep `strutils` 零命中），不是编译器生成的代码。

> **更正"自包含"这个说法**：那5 个 `.nim` 文件**今天仍然写着 `import std/strutils`**——`bench/nim/k_sieve.nim:15`、`k_fib.nim:15`、`k_intloop.nim:15`、`k_strbuild.nim:15`、`k_float.nim:15` 全都还在（`bench/nim/bench.nim:15` 也是）。`BENCHMARK.md:23` 把它们称作"5 个自包含内核"，这个说法今天是不准确的。
>
> 但**更正的理由和原来写的不一样**，需要再说清楚一层。原稿说"它们能编过纯粹因为源码里没实际调用 strutils 过程"——**这个说法是错的**：`bench/nim/k_float.nim:66` 明确调用了 `formatFloat(floatLoop(), ffDefault, 6)`，而 `formatFloat` 正是 `std/strutils` 的过程（`bench.nim:70` 是同一句）。所以 `k_float` 确实用到了 strutils。
>
> 那它们为什么能编过？真实原因是**编译器依赖分析只拉进"被引用到的"那部分运行时**：这 5 个文件总共只触到 `formatFloat` 这一个 strutils 过程，而它拉进来的运行时集合里没有 `resize`/`eqdestroy` 那批。而 `resize__system_u3063`、`eqdestroy___system_u3728` 这两个名字我 grep 全仓库**零命中**——它们今天根本不在仓库里，说明当初拉进它们的那份 `bench.nim` 构建产物没有入库（`bench/nim/` 目录里只有 `.nim` 源文件和 `BENCHMARK.md`、`runbench.sh`，**没有 `.nc_*` 缓存目录**，Nim 生成物与 gcc/goc/gocl 可执行文件都未入库）。
>
> 所以准确的说法是：**不是 import 被删了，也不是没调用 strutils，而是依赖分析恰好只拉进了一个不含缺失符号的子集**。这是更脆弱的一种"能编过"。
>
> **教学点**：一个"看起来多余"的 import 会让这套基准在**某天被误用时立刻编不过**——比如有人往 `k_fib.nim` 里加一行 `strutils.align(...)`，依赖分析就会把 `resize`/`eqdestroy` 拉进来，链接失败，而失败原因离真正的原因（那行新加的调用）很远。更严格地说，**"能编过"和"依赖是干净的"是两件事**：前者是当前恰好没触发，后者的 import 就是应该删掉的。严格更干净的做法是把那几行多余的 import 删掉。
>
> `bench/nim/BENCHMARK.md:23` 那句"完整 `bench.nim` 目前不能直接编"本身是准确的（原文写 `:26`，是旧行号）。

> **教训**：**"当前能编过"是一个关于现在的陈述，不是关于代码正确性的证明**。依赖分析兜住你的那一刻，它同时掩盖了"这里有个不该存在的依赖"这个事实。判断一个 import 是否真的多余，要看**引用它的那一行代码**在不在——`k_float.nim` 那句 `formatFloat` 就是证据。

## 九、架构与工程坑

### 坑 56：混合双生成器是本轮所有 bug 的根源

**现象**：这一轮踩的坑不是零散的类型推导 bug，而是**同一批症状反复出现**——链接期报 undefined symbol，或者更糟，链接过了但运行结果是错的。三个具体分歧同时存在：全局变量的汇编级名字（`G_` 前缀两边不一致）、undefined 符号到底算"导入"还是"对方已经定义"、以及变参调用（原生路径要靠检查格式串来重写）。

**为什么会错**：最初的设计是"用户函数走 LLVM、goclib 走原生"。这个分工听起来省事，实际后果是**两个生成器必须共享同一个符号表**——因为一个 C 程序里用户代码和库代码本来就会互相调用，一方定义的全局被另一方引用是常态。共享符号表就意味着：**每个名字都要回答"谁定义它"，每个 undefined 都要回答"是没定义，还是在对方那边有定义"**。这两个问题都不属于任何一侧的局部推理，而是跨越两侧的一致性约定，而一致性约定最容易被静默违反。

注意这三处的失败模式**不一样**：全局名不一致是**响亮的链接错误**（一眼看到）；undefined 语义分歧有时响亮有时**静默**；而变参格式串重写那一处**完全是静默的**——它产出的是"能链接、能运行、结果错"。这也是为什么整批 bug 的定位成本这么高。

**根因**：`src/gocl/translate.go:29-30` 把这三处并列写了下来——"Sharing a program that way needs a symbol table in both, and they disagreed: over a global's assembler-level name, over whether an undefined symbol was an import or something the other half already defined, and over the variadic calls the native path rewrites by inspecting a format string. Every one of those produced either a link error or a silently wrong answer."**同一段注释还点明了静默那一档**："a silently wrong answer"。

**修复**：不是把三处各自打补丁，而是**换掉分工的坐标系**——IR 侧**拥有全部 C 代码**（用户函数 + goclib 全部可达函数 + 全局变量含初始值）；goa 只负责**启动桩**（它不是 C，是按平台 ABI 设置进程栈的一段代码）+ 链接 COFF 对象。分工从此变成"**两种不同种类的代码**"，而不是"两个编译器抢同一批符号"。`src/gocl/translate.go:20-22` 用了"two different *kinds* of code rather than two compilers racing over the same symbols"这个说法。

设计原则被写进了 `src/gocl/cmd/gocl/main.go:1-11` 的文件头注释，作为整个后端的立论基础：

> The pipeline has the same shape as goc's, with **one owner for the whole program**. Every C function -- the user's and the C runtime's alike -- becomes LLVM IR, libLLVM compiles that IR to a single object, and goa contributes the entry stub and lays out the image. **One owner means there is never a question of which half defined a symbol**, which is what let the two-generator design accumulate link errors over a global's name, over an undefined symbol, and over variadic calls.

关键在于"**一个属主意味着永远不会问'哪一半定义了这个符号'**"——不是把问题回答得更好，而是让问题**不再存在**。第三处分歧（变参格式串重写）是这样消掉的：既然格式串重写是原生路径为了适配自己的调用约定而做的**转换**，而现在 C 侧全部由 IR 生成、原生侧根本不碰 C 函数体，这个转换就**没有存在的地方**了。`translate.go:29` 那句"the variadic calls **the native path** rewrites"里的定语是关键——重写是原生路径的产物。

配套的一条原则：**IR 翻译器必须与 codegen 零耦合**。IR 生成器是 CG 的**另一个输出后端**，所以直接复用它的 `exprType`/`lookupVar`/`scopes`；自己写第二套类型推导，等于对同一程序给两个答案，两者终将分歧然后静默编译错。这条其实是坑 56 的另一个侧面：如果翻译器自己推导类型，它就等于第二个前端，而第二前端的推导结果一旦和第一个不一致，症状同样是静默的错误结果。

**验证与教训**：这一处的"验证"不是跑测试，而是**读得出来**——`main.go:1-11` 的头注释今天仍然完整地陈述着这条原则，说明它没有退化成"临时补丁集合"。可迁移的教训是：**当一类 bug 反复出现且分布在不同层面时，别修 bug，去掉让 bug 成为可能的那个结构**。分歧点越多，越说明分工的坐标系选错了。

### 坑 57：`link.Data` 的布尔开关不够用

**现象**：为了把 goclib 切成"全量进 IR"，`link.Data` 里加了一个布尔开关来表示"这些符号已经在对象里了"。但真正跑起来之后，出现了一个不能自圆其说的现象：**同一次编译里，某些全局变量由对象提供、另一些仍需 goa 侧发射**——两种情况并存于同一个后端。一个开关表达不了"并存"。

**为什么会错**：因为真实的判定粒度是**按符号**，不是按模式。`src/common/link/link.go:239-244` 举了一个关键例子：一个全局变量的地址被某个**静态初始化器**取走时（`int *gp = &g;`），这个值是一个**重定位**，对象自身无法包含它，于是 IR 前端把它发成 `external global` 且**不带存储**。这个全局的 `.data` 映像**仍然必须由 goa 侧发射**——**而其他全局都不需要**。同一个开关对这两类符号必须给出相反的答案，这在逻辑上就不可能。

同时**第二个问题被顺带暴露出来**：不只是"谁定义了"，还有"谁要桩补"。goa 没有数据重定位（`src/common/link/emit.go:41-42`："goa has no data relocations, so a pointer value cannot live in .data"），所以**只有字符串字面量的指针槽**需要入口桩用 `lea` 算出地址再写进那个零填充的槽位。这**是另一个集合**，与"谁在对象里定义"不是同一个集合。

**根因**：`Data` 里当时只有一个 `bool`，它被两处语义不同的消费点共用。修法是**把一个布尔拆成两个按符号判定的谓词函数**，补 nil 语义已三态化：

- `SymbolInObject func(name string) bool`（`src/common/link/link.go:248`）——回答**谁定义它**；
- `NeedsSlotBinding func(label string) bool`（`:265`）——回答**谁要桩补**；
- `needsDefinition()`（`:271-273`）**用前者**：`return d.SymbolInObject == nil || !d.SymbolInObject(label)`；
- `emit.go:46-48` **用后者**：`if d.NeedsSlotBinding != nil && !d.NeedsSlotBinding(d.Globals[g.Name]) { continue }`。

`Data` 里已无任何 `Data bool` 字段（grep `bool` 在该结构体内零命中）。**nil 的含义被写成了三态**，且两个谓词的 nil 含义**不同**，这点很要紧：`SymbolInObject` 为 nil 时 = **原生生成器的安排**（对象里什么都没定义，所有全局都在这边发射）；`NeedsSlotBinding` 为 nil 时 = **每一个全局都要桩补**。前者见 `link.go:246-247`，后者见 `:264`。这不是随手写的默认值，而是**让调用方不填这个字段就等于选中了旧的原生行为**——`link` 包因此不需要知道调用方是哪个后端。

gocl 侧的填值在 `src/gocl/cmd/gocl/main.go:252`（`SymbolInObject: func(label string) bool { return claimed[label] }`）与 `:255`（`NeedsSlotBinding`，遍历 `externals` 列表）。`main.go:249-251` 的注释说明了为什么要问前缀映射而不是一个模式标志："The predicate is asked per label, which is why answering needs the prefix map rather than a mode flag."

**修复**（以及一个把设计逼到正确的具体事故）：传 nil map 会直接 panic。`src/common/link/global.go:551` 是写入点——`d.StrLabs[sl] = lab`。当时给 `d.StrLabs` 传了 nil map（而不是"非 nil 但内容为空"的 map），于是字符串字面量一进来就 `panic: assignment to entry in nil map`。来源 commit `c80d6bb`「gocl 能独立出 exe 了；顺带补上 goa 缺失的 Win64 .refptr 间接」，它动了 `src/common/link/link.go`（+339）、`global.go`（+889）、`emit.go`（+366）三处，**正是引入这两个谓词的同一个提交**——也就是说这个 panic 是新抽象的伴生缺陷，立刻被踩到。

**验证与教训**：`link.go:268-273` 那段注释是这个坑最值得记下来的部分，它把"重复定义"的后果说清楚了——"A duplicate definition is not a performance loss: **which of the two wins depends on link order rather than on anything the programmer wrote**."同理，`emit.go:259-262` 警告绑定一个对象已经初始化过的槽位"is not harmless"。可迁移的教训是：**当一个配置项开始需要"按 X 决定"的时候，它就不是配置项了**——把它换成谓词函数，并且**给谓词的"未设置"状态一个明确的、等于旧行为的默认值**，这样多后端共用一个包时不必互相知道。

### 坑 58：`-fllvm` 在 goc 里已是死标志

**现象**：`goc -fllvm` 能被接受、不报错，但**行为与纯 `goc` 完全一致**——都走 goa 后端，产物同为 7680B。想验证某件事时用 `-fllvm` 跑了一遍，得到"没变化"的结论。

**为什么会错**：因为 LLVM 后端**已经从 goc 里搬出去了**，变成独立编译器 `src/gocl`。goc 这个二进制里**根本不存在 LLVM 对象**可供链接——`src/goc/main.go:494-495` 的注释已经明说："The LLVM back end is a separate compiler now (src/gocl), so there is no object to link here: the assembly below is the whole program."换句话说，`-fllvm` 指向的那条路径**今天是不存在的**，标志被解析、被接受，然后什么都不做。

**根因**：全仓 `grep "\.llvm\b"` **只有一处命中**——`src/goc/main.go:759` 的 `cfg.llvm = true`。字段声明在 `:654`（`llvm    bool`），**无任何读取点**。一个只写不读的字段是"看起来还能用的标志"的典型形态：编译器不报错（它是个合法赋值），链接器不报错（没有符号需要解析），运行时不报错（行为没变），**只有你对它的期待会落空**。三条独立的检查链全部沉默，这就是死标志比崩溃更危险的原因。

**修复**：不是"让 `-fllvm` 重新起作用"——**LLVM 后端只能经 `gocl` 进入**，这才是正确的入口。goc 侧的 `-fllvm` 属于历史遗留。

后果值得单列：**验证裁剪后的 libLLVM.dll 时，第一次误用 `goc -fllvm` 测的其实是 goa，结论无效**，得用 gocl 重测。一个死标志最贵的代价不是"它没用"，而是**它会让你以为某个实验已经做过了**——而实际上你测的是另一个后端。

**验证与教训**：判据是 `grep -rn "\.llvm\b" src/` 加上"确认每个命中点是否被读取"。可迁移的教训是：**"加一个命令行标志"和"这个标志接到某个实现上"是两件独立的事，而编译器不会替你验证第二件**。删标志比留死标志更诚实——留着的代价是它会持续骗过那些"我记得有这个开关"的人（包括我自己）。

### 坑 59：`go build -o x.exe .` 对 `package compiler` 产出的是 archive

**现象**：Linux 上跑 `./bin/goc`，**81 个例子全部报 `(compile): ./bin/goc: Permission denied`**。从症状看像是编译器彻底崩了——81/81 全红，任何"编译器的某个功能坏了"的假设都解释不了这个比例。

**为什么会错**：因为产出的**根本不是可执行文件**。`src/goc/` 在改成 `package compiler` 之后，**包名是 `compiler` 而不是 `main`**（`src/goc/headers.go:1` 第一行就是 `package compiler`，`src/goc/main.go:1` 同样是 `package compiler`）。真正的 `main` 包在**子目录** `src/goc/cmd/goc/`。于是 `go build -o bin/goc .` 里的那个 `.` 指向的是**一个普通库包**——Go 按规则把库包编译成**归档文件**，`-o` 只是决定归档叫什么名字，它不会因为你给了 `.exe` 就把归档变成可执行文件。归档没有执行权限，Linux 于是报 `Permission denied`。

**这是本次唯一靠复现确认的坑，实测结果**：`cd src/goc && go build -o /tmp/x .` 成功退出，产物 3339714 字节，`od -c` 第一行是：

```
0000000   !   <   a   r   c   h   >  \n   _   _   .   P   K   G   D   E
```

`!<arch>` 是 ar 归档的魔数，后面跟着 `__.PKGDEF` 成员名——**确凿是 archive，不是 PE**。作为对照，正确形式 `go build -o /tmp/x_goc ./cmd/goc` 的首字节是 `M   Z 220  \0`（`MZ`，即 PE 的 DOS 头），6055424 字节。**一个字节之差，一个是归档一个是可执行文件**，而症状在 Linux 上只是 `Permission denied`。

CI 里一直用的是对的形式，`.github/workflows/ci.yml:38`：

```
(cd src/goc && go build -o ../../bin/goc ./cmd/goc)
```

`build.sh:78` 同样是对的（`./cmd/goc`，并带 `-ldflags` 注入版本号）。**正因为本地脚本与 CI 都写对了，这个错误只发生在"手敲一条 go build"的时候**——这也是它能活下来的原因。

**修复**：判据是——凡是把 `-o` 指向 exe 的 `go build`，都要确认**包参数指向 main package**（`./cmd/xxx` 或带 `func main` 的目录），而不是 `.`。一个可以立刻用的自查：`od -c <产物> | head -1`，看到 `!<arch>` 就是包参数给错了；看到 `MZ` 才是可执行文件。**产物类型是可以在半秒内证伪的假设，不该靠 81 个失败用例去猜。**

**验证与教训**：`file bin/goc bin/goc.exe` 确认 `bin/` 里两个都是 `PE32+ executable for MS Windows 10.00 (console), x86-64`——即今天的正式产物没问题，坏的是当时的构建命令。可迁移的教训是：**症状的"整齐程度"是重要线索**——81/81 全红几乎排除了功能性原因，指向的是"产物根本不对"或"环境整体缺失"。另外这条坑提醒了一件事：`-o` 的后缀（`.exe`）只是给链接器的提示，**它不参与决定产物类型**。

### 坑 60：库查找必须「上下交替」，且 `filepath.Join(dir, "..")` 不会爬升

**现象**：编译器找不到自己的 C 库，报缺头文件/缺符号。库明明就在仓库里。

**为什么会错**：因为**库和可执行文件是兄弟目录，不是父子**。库在 `src/goclib/`、exe 在 `bin/`，两者都在仓库根下。从 `bin/` 出发：只上溯，走的是 `bin/` → 仓库根 → 父目录 → 卷根，**永远穿不到 `src/`**；只下潜，从 `bin/` 只能碰到 `goc-out*` 那几个构建目录。`src/common/source.go:110-115` 把这个困境写成了散文：

> the library is neither an ancestor of the binary (climb from bin/ goes to the repo root, the parent, the volume, and never through src) nor a child of it (descend from bin/ reaches the goc-out* build directories). **Only going up one level and then back down finds it**, and a search that tries all of one direction before starting the other cannot express that.

也就是说，**唯一能找到库的路径形状是"上溯一层再下潜"**，而"先试完一个方向再试另一个"的搜索结构**表达不了"上溯到根再下潜进 src/"**——它会一路把下潜走完才转向，那时已经错过了。

**根因**（两层，第二层是 Go 特有的）：

第一层是**方向**：`probe()`（`src/common/source.go:119-131`）因此设计成**每层 descend → climb 交替**：

```go
for depth := 0; depth < 4; depth++ {
    if root, ok := descend(dir, consider); ok { return root, true }
    parent := filepath.Dir(dir)
    if parent == dir { return "", false }  // reached the volume root
    dir = parent
}
```

`descend()` 在 `:136`，它只试**一层**（`dir` 本身 + 它的子目录），因为更深的东西靠"先爬到父目录再从那里下潜"找到——`:133-135` 的注释明说"One level is enough here because probe alternates"。`source.go:117-118` 还说明了为什么要交替而不是并行搜索："Each level is tried as descend-then-climb, so **a nearer hit always wins over a farther one regardless of direction**."这让搜索结果与方向无关。

第二层是 **`filepath.Join` 的语义陷阱**：上溯必须用 `filepath.Dir()`（`:124`、`:212`、`:217`），**不能**用 `filepath.Join(dir, "..")`。`source.go:199-202` 用了两行注释讲这件事，字面上就写着 "`filepath.Join(dir, "..")` **不是**上溯的方式"：

> Join cleans the ".." away and returns the parent, so **a loop written with it never climbs at all**. An earlier version asked for the parent of the parent and silently got the parent, which is why the library went missing from src/goc.

**"silently got the parent"** 是这里最难查的部分：因为 `Join` 确实**返回了一个有效的父目录路径**，它不是错值、不报错、也不是空——它只是**永远只爬一层**，于是 `for` 循环以 `depth` 计数正常迭代、每轮拿到的都是同一个目录，**在有限次之后安静地放弃**。这类"看起来在循环、实际原地踏步"的 bug 没有任何报错信号。

**修复**：改成 `filepath.Dir()` 逐级上溯，与 descend/climb 交替。`source.go:195-197` 还补了一条相关的历史：**库移动过多次**（`src/goclib` → 仓库根 → 又回到 `src/`），"each move broke a hardcoded depth"——所以不能硬编码层数。`:203-204` 补上了另一个收益："an installed layout has goclib/ beside the exe, while the repository has it under src/. **Climbing covers both without a special case**."搜索起点是 exe 目录（`:208-213`，先 `EvalSymlinks` 解开软链），其次是工作目录（`:216-219`）；`:206-207` 说明为什么 exe 在前——"so a copy of the toolchain run from an unrelated project still finds its own library"。

**验证与教训**：`FindRoot()` 在 `src/common/source.go:166`，是查找的公开入口。判据是拿一个已知库目录（如 `src/goclib`）跑一遍 `probe`，并**在每个方向上单独验证上溯确实发生**（打印每轮 `filepath.Dir` 的结果）。可迁移的教训是：**路径拼接函数的行为差异要当契约记住，而不是当直觉**——`Join` 的 Clean 会把 `..` 吃掉，所以它**永远不是上溯的工具**；而在写"上溯 + 下潜"这类搜索时，**两轮之间必须有可观测的状态变化**，否则一个不动的循环看起来和一个在收敛的循环完全一样。

### 坑 61：CI 连红三次 —— 本地全绿 ≠ CI 绿

**现象**：本地跑全套测试全绿，推上去 CI 连续三次红，同一个原因。是 `go vet` 在 Linux 上报 10 处 undefined。

**为什么会错**：先说清一个容易被误判的点：**这不是新引入的代码问题，而是目录重组让依赖图变化后暴露**。当时的问题是 `src/goa/llvm.go`——整个文件用 `syscall.LazyDLL` 做 LLVM 绑定（这是 Windows-only 的 API），却**整个文件没有 `//go:build windows`**。本地是 Windows，`syscall.LazyDLL` 存在，所以本地永远是绿的；CI 有 Linux 腿，那里 `syscall.LazyDLL` 不存在，于是所有引用它的符号全部 undefined。

**为什么"三次"值得记**：因为**第一次和第二次的诊断方向都是错的**。第一次以为是最近的功能改动，第二次以为是 go.mod / 依赖版本漂移——都发生在往"是不是我刚写的代码有问题"这个方向上。真正定位靠的是**去看 undefined 的具体符号名**，发现它们全都来自同一个文件的同一族 API，才定位到构建约束。

> **路径更正**：当时是 `src/goa/llvm.go`，但**这个路径今天已不存在**——`src/goa/` 下现在**没有任何 `llvm*.go`**（`ls src/goa/llvm*.go` → No such file）。三文件后来整体搬到了 `src/gocl/`：`llvmapi.go`（1000 B）、`llvm.go`（19116 B）、`llvm_stub.go`（2292 B），外加一个 `llvm_test_helpers_test.go`。原因是 LLVM 后端已成为独立编译器。

**修复**：不是给整个绑定加标签——那会让所有提到 `goa.OpenLLVM` 的调用方都编不过——而是**拆三文件**：

- `src/gocl/llvmapi.go`（**无约束**）：`ErrNoLLVM`（`:15`）+ 档位常量（`LLVMCodeGenOptLevel`，`:19`；四档 `LLVMOptNone/Less/Default/Aggressive`，`:22-25`）——**错误与档位是调用方要能说出名字的东西，必须跨平台可用**；
- `src/gocl/llvm.go`（`//go:build windows`）：整个绑定；
- `src/gocl/llvm_stub.go`（`//go:build !windows`）：同名导出的桩（`OpenLLVM`/`LLVMAvailable`/`CompileToObject`/`CompileToAssembly`），返回 `errUnsupportedPlatform`。

**为什么不能用 `ErrNoLLVM`** —— `src/gocl/llvm_stub.go:21-23` 的注释把这层区别写明了：

> It is not `ErrNoLLVM`: that one says the **library is missing**, and on this platform **no amount of installing it would help**.

两个错误回答的是**两个不同的问题**：库没装（可修）vs 这个平台用不了（不可修）。桩还承担了一个额外责任——它**必须让不支持的程序体面地失败**，`:15-18` 的注释说这是"the same behaviour as a Windows build with no libLLVM installed, which is the situation a user is most likely to hit"。即：非 Windows 平台上的用户看到的行为，和"Windows 上没装 libLLVM"的用户看到的行为**一致**。

对应提交：加约束是 `94a76f2`「给 LLVM 绑定加构建约束：CI 的 Linux 腿连着红三次」；随后 `407154c`「把 LLVM 绑定搬进 gocl，并抽出 gocld 链接器」做了整体搬迁。

另一个教训：**加约束要连测试一起加**，否则 Linux vet 会在 `undefined: llvmAPI` 上再红一次。

铁律：改完必须双平台验（逐 module `go vet ./... && GOOS=linux go vet ./...`）；**推送后要 `gh run list` 看结果**，不能推完就当完事。

> **同一类教训的新成员（本次核实发现）**：当年"双平台验"的教训如今又有新成员没被 CI 覆盖。仓库现有 **8 个 `go.mod`**（实测 `find . -name go.mod`：`src/common`、`src/frontend`、`src/goa`、`src/goc`、`src/gocl`、`src/gocld`、`src`、`tools`），而 `.github/workflows/ci.yml:29-34` 只 vet 了 **6 个**——`src/frontend`、`src/goc`、`src`、`src/gocld`、`src/goa`、`tools`——**缺 `src/common` 和 `src/gocl`**。也就是说本篇 05 里修的那批 `src/common/printfspec.go` 改动，以及**整个 gocl 后端**，目前都不在 CI 的 vet 范围内。这两个 module 恰恰是本轮 LLVM 后端改动最密集的地方。
>
> 同类还有两个 CI 结构性细节值得记。`ci.yml:18-26` 的 gofmt 检查**刻意不用 `.`**，`:20-24` 的注释说明了原因：`.gitignore` 排除了本地 `scratch/`，而 `gofmt -l .` 会把不入库的目录也判红——"the job can be red for a reason that exists only on one machine, and **there is no way to fix it from a commit**"。这里的设计原则是：**CI 的判红必须对应一个能进 commit 的修复**。
>
> `ci.yml:42-46` 的 `msgboxcheck` 改为**交叉编译而非跳过**：`GOOS=windows go build`。`:42-45` 记录了原因——裸 `go build` 会因 build constraints 排除全部 Go 文件而报 `build constraints exclude all Go files`，"which **silently took the whole Linux job down** once"。改法是"**Cross-compile instead of skipping it**"——注意不是"加 `|| true` 跳过"，而是**换一个能真正验证它的构建方式**。这与本坑"Linux vet 再红一次"完全同型：**一个平台的失败把另一个平台的有效信号一起埋掉了**。

**验证与教训**：判据是 `GOOS=linux go vet ./...` 逐 module 跑一遍——**本地全绿只证明了一件事：你只测了你所在的那个平台**。可迁移的教训是：**让"本地绿 / CI 红"这类分歧可归因的最好办法，是把 CI 的覆盖面写成可枚举的清单（几个 module、几个 OS、几个架构），然后对着清单数**——本例中数出来的正是"8 个 go.mod、只 vet 6 个"这个可查的缺口。附带一条：**CI 里出现 `|| true`、`|| echo skip` 的时候要警惕**，它可能是在掩盖一个"这个平台根本编不了"的真相。

### 坑 62：rebase 的四条铁律

**现象**：这是一条**流程性**经验，源码无法证伪——但四条都在同一次 rebase 上同时用上了，所以一并记下来。

1. **只解冲突文件，其余原样保留**。曾试图用 `git show <mycommit>:<file>`"恢复"自动合并的 4 个文件——这个操作看起来无害（我改过的文件，恢复我的版本总该对吧），实际后果是**覆盖掉远端的自动合并**。远端给 `CompileToObject` 加了第 5 个参数 `linux bool`，用旧版会 `have/want` 签名不匹配、build failed。**最贵的一句是"且伪装成"**——签名不匹配的报错出现在前端，看起来像 `__builtin_va_list` 解析错，**浪费了一整轮排查**。
2. **判断"失败是否我引入"要双向对照**：先把工作区置为纯 origin 跑全套确认全绿，再恢复我的改动跑，才定位到归属。**只跑自己那一版得到红，是没法判断归属的**——红可能是本来就红的。
3. 合并冲突区若两侧结构差异大，**整函数重建比逐块拼更可靠**；重建后必删被自动合并保留的重复尾段（两边都有、且都"看起来对"的尾段，留着就是编译错）。
4. **`gofmt -l` 对冲突文件必跑**——rebase 后必查，**冲突解决不会自动格式化**。

**修复**：第 4 条如今已被 CI 固化，不必靠人记得——`ci.yml:26` 用 `out="$(gofmt -l src tools/elfcheck tools/msgboxcheck)"` 在 push 前拦截，`:26` 之后紧跟 `if [ -n "$out" ]; then ... exit 1; fi`。这是四条里**唯一可以自动化的一条**，也说明"靠人记得"本来就是个不该长期维持的状态。

第 1 条提到的 `CompileToObject` 第 5 参 `linux bool` 现存于 `src/gocl/llvm_stub.go:39`（Windows 侧 `llvm.go` 里是同一个签名）：

```go
func (l *LLVM) CompileToObject(ir []byte, outPath string, opt LLVMCodeGenOptLevel, passes string, linux bool) error
```

**验证与教训**：可迁移的教训是**"恢复成我的版本"绝不是一个安全的默认动作**——在合并的语境里，"我的版本"是合并结果**之前**的状态，恢复它等于把对方的工作扔掉。第 2 条则是通用方法论：**归因要靠对照实验，不能靠推理**，因为"红"这件事本身不携带"是谁引入的"这一信息。

### 坑 63：`-S` 被静默 no-op，且第一轮修复也是错的

**现象**：`gocl -S file.c` 不输出汇编文件，实际仍然产出 exe。

**为什么会错**：这里有**两层错**，第二层比第一层更值得记。

第一层：`-S` 的解析存在、标志位存在，但**没有接到任何实现上**——典型的静默 no-op：不报错、不警告、不产出你要的东西。跟坑 58 的死标志同型，但坑 58 是遗留，这里是**未完成**。

第二层（第一轮修复）：把 `-S` 接到 `cfg.DumpAsm`（写 goa 入口桩）——**桩里没有用户的代码**。这个错误特别值得展开，因为它**看起来是对的**：`DumpAsm` 确实是"输出汇编"，`-S` 确实是"输出汇编"，接上去之后 `-S` **不再静默、也确实产出了 `.s` 文件**。但那个 `.s` 里只有启动桩（设置进程栈的那段），**用户写的 C 函数一个字都没有**。如果只检查"有没有产出文件"，这个修复会被判为成功。

**根因**：`DumpAsm` 的语义是"**调试用的入口桩**"，而不是"程序的汇编"。`gocl/cmd/gocl/main.go:505-511` 的注释把这个区分写清楚了：

> "Stop before the executable" -- lower the program to native assembly text with LLVM's AsmPrinter and write it, the way `gcc -S` does. gocl's C code goes through LLVM into a COFF object, so **the assembly worth showing is the AsmPrinter's .s of the C bodies, not the entry stub goa assembles (that one is the -dump-asm debugging aid)**.

所以 `-S`/`-c` 与 `-dump-asm` 是**两种不同的产物**：今天在 `main.go:505` 与 `:501-502` 是两个独立的 case，置不同的标志位（`cfg.EmitLLVMAsm` vs `cfg.DumpAsm`）。产物文件名也不同——`-S` 给 `-o` 就落到该路径、没给落到 `<源>.s`（`main.go:134-138`），而 `-dump-asm` 没给 `-o` 时落到 **`<源>.stub.asm`**（`main.go:167`），带 `.stub` 就是为了让两者**不会互相覆盖**。`main.go:40-46` 的字段注释把分工写成了对照："It is a debugging aid for the assembler path, **distinct from EmitLLVMAsm (-S)**, which writes the AsmPrinter's .s for the C bodies themselves."

`EmitIRAssembly` 全仓**只有一个调用点**——`gocl/cmd/gocl/main.go:139`，在 `cfg.EmitLLVMAsm` 分支内（`:129-142`），写完 `return "", nil` 就结束，**不进入 `CompileIR` / 链接**（`:143` 才是那条路）。这条"只此一个消费点"就是"没接错目标"的结构性证明。

**修复**：走 LLVM 的 AsmPrinter 生成 C 函数体的 AT&T `.s`。实现是**复用早已存在但一直没接线的 `emitIRAssembly`**——`src/gocl/compile.go:87`：

```go
func emitIRAssembly(ir string, outPath string, opt int, linux bool) error
```

它内部 `OpenLLVM()` 后调 `api.CompileToAssembly([]byte(ir), outPath, level, irPasses(opt), linux)`（`compile.go:96`）。`compile.go:82-86` 的注释点出了函数名里"IR"的含义：输出是"AsmPrinter's output, **not the entry stub that genWith emits** once `claimed` covers every function"。

`compile.go:101-106` 把它导出为 `EmitIRAssembly`，注释直接点明这是 `gcc -S` 的对等物：

> This is the LLVM back end's answer to `gcc -S`: the readable artifact is the assembly **the C bodies became**, not the entry stub goa assembles. The file is not fed back to goa; **it is the final artifact**, exactly like the .s a native `-S` build writes.

**"not fed back to goa; it is the final artifact"** 这句把输出边界定死了。输出命名：给了 `-o` 落到该路径，没给落到 `<源>.s`。内容边界：**只含 C 函数体，不含入口桩**——与 `gcc -S` 不含 CRT 启动一致，这是对等物语义的一部分，不是简化。

commit 链是三步，每一步都有独立价值：`ca1cc69`「-fllvm -S: 走 AsmPrinter 落真实原生汇编 (.s)，而非空入口桩」（走 AsmPrinter 落真实 .s）→ `e12d42d`「抽出 goc/common 与 gocl：LLVM 后端成为独立编译器」（抽出 goc/common 与 gocl）→ `62dcea4`「gocl: fix Win32 import resolution, drop import-table bloat, wire -S to AsmPrinter」（修 Win32 import 解析、去 import-table 冗余、把 `-S` 接到 AsmPrinter）。

> **本次核实发现的两处"注释落后于代码"**（不影响行为，但正是本坑的同型残留）：`compile.go:83-84` 仍写着 "Used by **-dump-asm** with the IR kept"，而 `EmitIRAssembly` 的唯一调用点是 `main.go:139` 的 `EmitLLVMAsm` 分支——**今天 `-dump-asm` 根本不经过它**（`-dump-asm` 在 `main.go:161` 单独写 stub 到 `.stub.asm`）。同样，`main.go:162` 的行内注释开头还写着 "**-S / -dump-asm:** write the assembly gocl emits"，而这个分支现在只由 `DumpAsm` 进入。两处都是**第一轮错误修复留下的痕迹**：那时 `-S` 确实被接到 `DumpAsm` 上，后来改对了，注释没跟着改。**代码行为是对的（`.stub.asm` 与 `.s` 两个文件名不冲突），但注释会误导下一个读代码的人去查 `-dump-asm` 的 AsmPrinter 输出——而那条路不存在。**

**验证与教训**：验证 `-S` 不能只看"文件出来了"，要看**文件里有没有你的函数**——比如 `grep` 函数名，或看 `.s` 里 C 主体是否存在。可迁移的教训是：**当一个标志被接错目标时，"不再静默"和"正确"是两件事**，前者会伪装成后者。所以修好静默行为之后必须再问一步："产出的内容是调用者要的那个东西吗？"——第一轮修复恰恰在这一点上通过了所有"看起来合理"的检查。

### 坑 64：其他环境坑（都会反复踩）

**现象与要点**（前两条今天**依然成立**，后一条的现状已变）：

- **Git Bash 直接跑 PE 会误报崩溃**：`./x.exe` 报 `Segmentation fault`，但 `cmd //c "x.exe"` 退出码正确。**验证 PE 必须走 cmd**——Git Bash 用 MSYS2 的方式加载 PE，把"缺 DLL / 入口点不对"之类的加载失败报成了段错误。**症状（Segmentation fault）在 Windows 上几乎不携带信息量**。
- **同秒内批量"编译 + 运行"多个程序会假失败**：libLLVM.dll **并发加载竞争**。每个之间 `sleep 0.2~0.3s`。**症状是间歇性的**，最难缠的一类——跑一次过了、再跑就红了，容易怀疑代码而非怀疑并发。
- **只看顶层 `--- FAIL` 行会误判成"全红"**：e2e 实际 9/10 通过。顶层 FAIL 是**汇总**，不是明细。
- **`bin/goc` 遮蔽 `bin/goc.exe`**：`bin/` 里一个陈旧的无扩展名文件，PATH 优先选它 → 所有"改了源码没生效"的假象都源于此。**goc 项目一律用绝对路径 `bin/goc.exe`。**
  > **这条今天依然成立，尚未清理（本次已 `ls -la bin/` 复核）**：`bin/goc`（**4020224 B，10-06 07:48**）与 `bin/goc.exe`（**4210688 B，10-07 02:18**）并存；`file` 确认**两者都是** `PE32+ executable for MS Windows 10.00 (console), x86-64, 8 sections`——**无扩展名的那个更旧**（早约 19 小时、小 190464 B）。也就是说遮蔽是真的、而且指向一个可执行但过期的产物（不是"不是可执行文件"那种一眼能看出的错）。同目录里 `bin/goa`（2193920 B，10-06 07:48）vs `bin/goa.exe`（2215424 B，10-07 02:19）、`bin/gocl`（3342848 B，10-06 08:24）vs `bin/gocl.exe`（3483648 B，10-07 02:18）是**同样的三对**，可见这是当初"不带扩展名"那套产物的统一残留。
- **`go build` 缓存不刷新 `//go:embed`**：新增头文件后产物 embed 仍是旧头。症状是 `skipping unavailable system header`。
  > **这条今天只属 standalone 构建（本次已核实）**：`src/goc/headers.go` 现在**不含任何 `go:embed`**——全文 16 行，`package compiler` 之外**全是注释**，`:3-6` 描述的是"They live as real header files under goclib/ ... and are **read from there at run time** so `#include <name.h>` resolves with no system headers and no install step"，`:15-16` 指向 `libfs.go` 说明搜索顺序。迁移痕迹留在 `src/common/source.go:15`："goc used to carry the whole C library inside the executable: `//go:embed` baked goclib/*.c and goclib/*.h into the binary"（`:10-14` 的小标题就叫"The C runtime on disk."）。普通 `goc`/`gocl` 现在**运行时读磁盘**（入口 `FindRoot()`，`source.go:166`），**新增头文件不需要 `touch` 了**。今天全仓**唯一真正的 `//go:embed goclib/*` 指令**在 `src/main.go:46`，属于 `goc-standalone`（`src/main.go:43-46` 注释说明 `src/goc/cmd/` 下需要 embed 才能拿到 `goclib/`）。**所以遇到 `skipping unavailable system header` 时，先查 `GOCLIB_PATH` / 库查找路径（坑 60 的 `probe`/`FindRoot`），而不是 `touch src/goc/headers.go`。**
- **批量 sed 改路径后必须 grep 目标串确认为 0**：某次 8 处漏改，全表现为"某腿 FAIL"而非编译错误；其中 `./cmd/goa` 被误改成 `./goa`，而 goa module 的主包目录就叫 `cmd/goa`，**与 cmd/goc 不是一回事**。**sed 的漏改不会报错，它只让某个名字指向错误的目标。**
- **`go mod tidy` 联网超时；本地 replace 的 module 也要 go.sum 条目**（**间歇性**，清缓存时才报）。**加 module 后逐个检查所有 go.mod，别只加被直接 import 的那个。**
- **`commit -m` 的反引号会被 shell 吞掉**（bash 双引号串里是命令替换，报 `command not found`，**提交成功但消息留空洞**）。**长提交信息一律用 `git commit -F <file>`。**——注意这个失败模式：**命令本身成功了**，只有消息被吃掉，所以不检查就完全不会发现。
- **这个仓库的 shell 与 Python heredoc 会把 `\n` 写成字面 `/n/`**：症状是 LLVM 报 parse error，**我因此误判过一次"段错误"**。写多行 Go/asm 字符串**必须用文件写入工具**。这条与第一条同型：**症状指向了一个完全错误的层**（LLVM 的 parser / CPU 的段错误），而根因在更早的一步——文本在进入编译器之前就已经坏了。

**验证与教训**：这一节里没有一条能靠读代码发现，全部是**环境与流程的耦合**。可迁移的教训是：**当症状跨越了"看起来无关的层"（汇编解析 / 段错误 / Permission denied）时，先怀疑最平凡的输入侧原因**——文本有没有被 shell 吃掉、`-o` 指向的包对不对、PATH 里的 exe 是不是旧的。这类排查的成本几乎全在**验证平凡假设**上，而不是在推理上。
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

