---
title: "MicroPython入坑记（四）关于MicroPython的代码保护"
date: 2018-03-31T16:47:00+08:00
categories: ["嵌入式"]
tags: [MicroPython, mpy, 固件, 代码保护]
original: "https://www.cnblogs.com/yafengabc/p/8682910.html"
aliases: ["/cnblogs/p8682910/"]
weight: 140
draft: false
---

脚本开发东西，可能面临的第一个问题就是：拷给别人，代码怎么写的他不就都知道了？不行，我要保住我的小秘密！

先说下结果：没有攻不破的堡垒，即使你写成C语言，只要能拿到二进制结果，都可以反汇编逆向出你是怎么实现的，关键是值不值得

另外，这跟逆向者对系统的了解程度有关，比如对方连代码都不会上传，那你即使把源文件放进去也他也无可奈何。

好了，言归正传，我们知道普通python有个编译成字节码的功能，也就是源代码会在解释时先编译成一个类似java中间代码的结果，这是不可读的（但是这也不排除反编译的可能，毕竟JAVA的class文件是有反编译软件的）。

micropython也有这个功能，不过这个文件的扩展名是mpy，也不能在运行时自动生成，需要一款软件：mpy-cross.exe，这是micropython官方提供的，可以用python pip直接安装，安装完成后，可以运行mpy-cross.exe pythonfile,py，就可以生成一个pythonfile.mpy的字节码文件，这文件使用跟py文件是等效的。把所有文件生成mpy，可以在一定程度上保护你的代码。

更进一步的方式：把mpy文件藏到固件中去，这不光能保护代码，还能降低程序的内存占用，官方的描述如下：

## Cross-installing packages with freezing

For the low-memory [`MicroPython ports`](http://docs.micropython.org/en/latest/esp8266/reference/glossary.html#term-micropython-port), the process described in the previous section does not provide the most efficient resource usage,because the packages are installed in the source form, so need to be compiled to the bytecome on each import. This compilation requires RAM, and the resulting bytecode is also stored in RAM, reducing its amount available for storing application data. Moreover, the process above requires presence of the filesystem on a device, and the most resource-constrained devices may not even have it.

The bytecode freezing is a process which resolves all the issues mentioned above:

- The source code is pre-compiled into bytecode and store as such.
- The bytecode is stored in ROM, not RAM.
- Filesystem is not required for frozen packages.

Using frozen bytecode requires building the executable (firmware) for a given [`MicroPython port`](http://docs.micropython.org/en/latest/esp8266/reference/glossary.html#term-micropython-port) from the C source code. Consequently, the process is:

1. Follow the instructions for a particular port on setting up a toolchain and building the port. For example, for ESP8266 port, study instructions in `ports/esp8266/README.md` and follow them. Make sure you can build the port and deploy the resulting executable/firmware successfully before proceeding to the next steps.
2. Build [`MicroPython Unix port`](http://docs.micropython.org/en/latest/esp8266/reference/glossary.html#term-micropython-unix-port) and make sure it is in your PATH and you can execute `micropython`.
3. Change to port’s directory (e.g. `ports/esp8266/` for ESP8266).
4. Run `make clean-frozen`. This step cleans up any previous modules which were installed for freezing (consequently, you need to skip this step to add additional modules, instead of starting from scratch).
5. Run `micropython -m upip install -p modules <packages>...` to install packages you want to freeze.
6. Run `make clean`.
7. Run `make`.

After this, you should have the executable/firmware with modules as the bytecode inside, which you can deploy the usual way.

大体的意思是这样的：运行py文件不能有效的使用内存资源，因为代码执行时需要被编译成bytecode代码，该编译需要RAM，生成的字节码也存在RAM中，这就降低了可用内存。并且存储py文件需要文件系统，而一些嵌入设备本身不存在文件系统。

嗯，神一样的保护功能，不但降低了内存占用，还把字节码藏到了固件中，代价就是需要自己编译micropython固件。
