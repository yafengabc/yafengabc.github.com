---
title: "用Cython加速Python程序以及包装C程序简单测试"
date: 2016-12-04T15:02:00+08:00
categories: ["编程杂项"]
tags: [Cython, Python, C语言, 性能优化]
original: "https://www.cnblogs.com/yafengabc/p/6130849.html"
draft: false
aliases: ["/cnblogs/p6130849/"]
---

# 用Cython加速Python程序

我没有拼错，就是Cython，C+Python=Cython!  
我们来看看Cython的威力,先运行下边的程序：

import time

def fib(n):  
if n0:  
return 0  
if n1:  
return 1  
return fib(n-1)+fib(n-2)  
t=time.time()  
print(fib(40))  
print(time.time()-t)

$ python fib.py  
102334155  
59.367255449295044  
在我的渣渣笔记本上，用时59.3秒，差不多一分钟。当然，在你那可能比我快一点，这也很正常。  
好了，我们再试试Cython：

$ cython fib.py --embed  
$ gcc -O3 fib.c -I /usr/include/python3.5m/ -lpython3.5m  
$ ./a.out  
102334155  
14.487313747406006

嗯，快了那么一点点，4倍左右;我解释一下前边的几句代码：  
首先，用cython命令把python生成c文件，也就是cython fib.py会生成一个fib.c的文件  
--embed参数就是自动生成一个main函数，以便让gcc生成可执行程序。  
接下来就是用gcc把fib.c编译成了个a.out程序，运行之，结果快了4倍（从60秒减少到15秒以内）。  
当然，这只是小试牛刀，区区4倍而已，这也太少了！  
接下来我吧这个文件复制成fib.pyx，并修改了一句代码：

import time

cdef int fib(int n):  
if n0:  
return 0  
if n1:  
return 1  
return fib(n-1)+fib(n-2)  
t=time.time()  
print(fib(40))  
print(time.time()-t)

我只改了1句，就是把 def fib(n):改成了 cdef int fib(int n):，也就是加了一个类型，下边让我们见证奇迹：

$ cython fib.pyx --embed  
$ gcc -O3 fib.c -I /usr/include/python3.5m/ -lpython3.5m  
$ ./a.out  
102334155  
0.45729994773864746

没有看错，现在只需要0.45秒！性能提升了132倍。  
这个0.45秒算是什么样的速度呢？下边，我照猫画虎，写了基本相同的一段C程序：  
#include "stdio.h"  
#include "time.h"

static int fib(int n){  
if(n0)  
return 0;  
if(n1)  
return 1;  
return fib(n-1)+fib(n-2);  
}  
int main(){  
clock\_t t=clock();  
printf("%d\n",fib(40));  
printf("%f sec\n",(clock()-t)/1000.0/1000.0);  
}  
这个跟python写的基本一模一样，只是换成了C语法，然后：

$ gcc -O3 fib.c  
$ ./a.out  
102334155  
0.452981 sec

天，只比刚才Cython的程序慢了0.005秒（我觉得这已经是误差了）  
是不是感觉Cython碉堡了？（基本用Python的语法，实现了C的速度。  
其实，这才刚刚开始。毕竟虽然Python代码写起来比C溜好多，但以前的C代码怎么办？并且，一些C实现的算法  
用Cython改写也不是特别方便，能不能直接拿来就用呢？当然能，并且也可以很6。  
比如上边的那个fib函数，我已经用C写完了，怎么整合到Python里边呢？  
首先，我先把C里边的main函数去掉，改成下边的样子：

# include "stdio.h"

# include "time.h"

static int fib(int n){  
if(n0)  
return 0;  
if(n1)  
return 1;  
return fib(n-1)+fib(n-2);  
}  
其实这时，我们已经可以用gcc编译成一个链接库，用ctypes调用了，然而在Cython看来,  
这太（调）不（用）清（麻）真（烦）,我们只需要2句代码：

cdef extern from "fib.c":  
int fib(int)

def fibf(n):  
return fib(n)

虽然是4行，其实也就是2句无疑：）  
第一句我先把fib函数从C文件里边导入，然后又定义了一个fibf的函数，把导进来的函数又调用了一下。  
cdef的作用，就是把外部函数导出为cython能调用的函数，def的作用就是定义python能调用的函数了。  
把这个文件保存成fibf.pyx,然后：

cython fibf.pyx  
gcc fibf.c -shared -fPIC -I /usr/include/python3.5m -lpython3.5m -o fibf.so -O3

把这个文件编译成了一个fibf.so文件  
然后写了下边的python代码测试：

$cat test.py  
import time  
import fibf  
t=time.time()  
print(fibf.fibf(40))  
print(time.time()-t)

python test.py  
102334155  
0.47469592094421387

也就是说，2句代码，就把一个C语言写的代码。包装成了一个python能直接import的库。是不是方便极了……  
反正个人觉得比ctypes方便。

