---
title: "Python64+win10_64+cython+msys2(ming64)踩坑记"
date: 2019-08-26T15:56:00+08:00
categories: ['归档']
tags: [Cython, MSYS2, Python, Windows, 编译]
original: "https://www.cnblogs.com/yafengabc/p/11399939.html"
draft: false
---

一直在linux下用python，一直妥妥的，从没想过在windows下编译cython模块，直到昨天……

过程是曲折的，解决方法是简单的，时间不多，长话短说，直接先来个传送门：

<https://www.jianshu.com/p/50105307dea5>

这里的步骤是可以的，因为我用的msys2，所以过程有一点点曲折，下边补充说明一下：

msys2算是个windows下的linux系统，里边一共包含了3套工具链，

比如安装GCC，可以直接安装gcc这个包，也可以安装mingw-w64-i686-gcc或者mingw-w64-x86\_64-gcc

这三个包不同之处在于，gcc是属于msys2系统本身的工具链，如果编译软件，会连接到/usr/lib下，也就是虽然生成的是exe，但是是没法离开msys2运行的

而mingw-w64-i686-gcc与mingw-w64-x86\_64-gcc实际上是个交叉工具链，编译出来的exe是只依赖宿主系统的库的，所以可以直接在windows系统下运行的

甚至只要吧msys2下的mingw64/bin目录加到系统系统变量，是可以随便调用的。

python配合msys2的使用方式：

python也可以有3种安装方式，

1，直接从python官方下载安装包安装，这是最普遍的做法，跟msys2完全没关系，可以直接通过系统环境变量调用相应的mingw32或者mingw64编译器，编译自己的包

2，在msys2直接安装python，配合msys2的gcc，这样类似直接在linux安装gcc python，最方便，编译连接时也不需要指定路径，类似linux下的体验，缺点是编译生成的exe文件无法离开msys2运行

3，安装mingw-w64-x86\_64-gcc mingw-w64-x86\_64-python这种工具链下的包，这个其实跟1类似，可以编译直接在系统下运行的exe，也可以直接在msys2下使用。

因为我系统下已经安装了python3 64bit，并且安装了大量的包，所以装完msys2以后，直接安装mingw-w64-x86\_64-gcc就可以在pyhon3中使用这个编译器了。

```
pacman -S mingw-w64-x86_64-gcc
```

然后把mingw64加到系统环境变量比如我msys2安装在E:\linux（没错我伪装成linux了哈）下，系统环境变量就加一条E:\linux\mingw64\bin。

根据传送门的教程：在python安装目录`C:\Program Files\Python\Lib\distutils`中新建`distutils.cfg`文件，内容如下：

```
[build]
compiler=mingw32

[build_ext]
compiler=mingw32
```

注意，这地方是没错的，不要因为是mingw64就写mingw64，写mingw32是对的。

这时候，编译一个cython程序，会报VC版本1900 1906之类，根据教程这么改：

三、修改*cygwinccompiler.py*文件

进入python安装目录`C:\Program Files\Python\Lib\distutils`中，修改`cygwinccompiler.py`文件，添加以下内容到`get_msvcr()`函数（用于解决`ValueError: Unknown MS Compiler version 1900错误`）。

```
elif msc_ver == '1900':
    # Visual Studio 2015 / Visual C++ 14.0
    # "msvcr140.dll no longer exists"
    return ['vcruntime140']
```

因为我这报的是1906，所以就改成了

```python
elif msc_ver == '1906':
    # Visual Studio 2015 / Visual C++ 14.0
    # "msvcr140.dll no longer exists"
    return ['vcruntime140']
```

这一步一定要根据实际版本改。

```
转换python36.dll文件
用于解决C:\Program Files\Python\libs/libpython36.a: error adding symbols: File format not recognized错误。

复制C:\Program Files\Python\python36.dll到桌面，执行以下命令：

gendef python36.dll
dlltool -D python36.dll -d python36.def -l libpython36.a

备份C:\Program Files\Python\libs/libpython36.a文件，将上步生成的libpython36.a复制到C:\Program Files\Python\libs目录下。

转换vcruntime140.dll文件
用于解决C:\Program Files\Python / vcruntime140.dll: file not recognized: File format not recognized错误。

复制C:\Program Files\Python\vcruntime140.dll到桌面，执行以下命令：

gendef vcruntime140.dll
dlltool -D vcruntime140.dll -d vcruntime140.def -l libvcruntime140.a

2.将生成的libvcruntime140.a复制到C:\ProgramFiles\Python\libs目录即可。
```

这几步没什么问题，我是python3.7.x，所以上边的python36.dll改成python37.dll，当然，我是python装到了D盘，所以对应的C:\Program Files\xxxxx要改成D:

另外，gendef需要安装mingw-w64-x86\_64-tools-git

```
pacman -S mingw-w64-x86_64-tools-git
```

否则没有这个命令。

另外，在我这编译cython时会出现一些编译错误，经过网上搜索，加了-DMS\_WIN64选项，问题全部解决

```
        self.set_executables(compiler='gcc -O -Wall -DMS_WIN64',
                             compiler_so='gcc -mdll -O -Wall -DMS_WIN64',
                             compiler_cxx='g++ -O -Wall',
                             linker_exe='gcc',
                             linker_so='%s %s %s'
                                        % (self.linker_dll, shared_option,
                                           entry_point))
        # Maybe we should also append -mthreads, but then the finished
        # dlls need another dll (mingwm10.dll see Mingw32 docs)
        # (-mthreads: Support thread-safe exception handling on `Mingw32')

        # no additional libraries needed
```

以上内容在

```
gcc -v
Using built-in specs.
COLLECT_GCC=E:\linux\mingw64\bin\gcc.exe
COLLECT_LTO_WRAPPER=E:/linux/mingw64/bin/../lib/gcc/x86_64-w64-mingw32/9.2.0/lto-wrapper.exe
Target: x86_64-w64-mingw32
Configured with: ../gcc-9.2.0/configure --prefix=/mingw64 --with-local-prefix=/mingw64/local --build=x86_64-w64-mingw32 --host=x86_64-w64-mingw32 --target=x86_64-w64-mingw32 --with-native-system-header-dir=/mingw64/x86_64-w64-mingw32/include --libexecdir=/mingw64/lib --enable-bootstrap --with-arch=x86-64 --with-tune=generic --enable-languages=c,lto,c++,fortran,ada,objc,obj-c++ --enable-shared --enable-static --enable-libatomic --enable-threads=posix --enable-graphite --enable-fully-dynamic-string --enable-libstdcxx-filesystem-ts=yes --enable-libstdcxx-time=yes --disable-libstdcxx-pch --disable-libstdcxx-debug --disable-isl-version-check --enable-lto --enable-libgomp --disable-multilib --enable-checking=release --disable-rpath --disable-win32-registry --disable-nls --disable-werror --disable-symvers --enable-plugin --with-libiconv --with-system-zlib --with-gmp=/mingw64 --with-mpfr=/mingw64 --with-mpc=/mingw64 --with-isl=/mingw64 --with-pkgversion='Rev1, Built by MSYS2 project' --with-bugurl=https://sourceforge.net/projects/msys2 --with-gnu-as --with-gnu-ld
Thread model: posix
gcc version 9.2.0 (Rev1, Built by MSYS2 project)
```

python 3.7.3/3.7.4环境下通过

另外，cython还支持把python文件编译成一个独立的exe文件， 也需要加两个特殊的选项：

```
cython --embed fibonacci.py
gcc fibonacci.c "-ID:\\Program Files\\Python37\\include\\" "-LD:\\Program Files\\Python37\\libs" -lpython37 -municode -DMS_WIN64 -o fib.exe
```

比平时多加了-municode -DMS\_WIN64两个选项，其中第一个选项的作用是使用wmain这个函数作为main函数。
