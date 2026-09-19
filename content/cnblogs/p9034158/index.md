---
title: "MicroPython与Python速度对比"
date: 2018-05-14T08:33:00+08:00
categories: ['归档']
tags: [MicroPython, Python, 性能对比, PyPy]
original: "https://www.cnblogs.com/yafengabc/p/9034158.html"
draft: false
---

首先说明，micropython跟python是没有任何可比性的，python作为一种通用的语言，在扩展性上不是micropython能比的，比如大量的库，可以方便的用C语言加模块提升速度，有pypy这样的带JIT的解释器，micropython是适合于单片机的系统虽然可以用C写lib，但是需要重新编译整个固件，此外，micropython也缺乏加载本地代码的功能，比如加载C便宜的so库。所以不要试图用micropython代替python，这不是一个好主意，除非micropython支持的库满足你的使用了。

这篇文章主要是简单的对比这两个不同的实现的性能有何差别。

测试代码有两个，一个是一个大循环，一个是递归计算斐波那契数列，例子比较简单，代码如下：

```python
try:
    import utime as time
except:
    import time

def bigloop():
    s=0
    for i in range(1000000000):
       s+=i

def fib(n):
    if n==0:
        return 0
    if n==1:
        return 1
    return fib(n-1)+fib(n-2)

t=time.time()
bigloop()
print("bigloop time:",time.time()-t)
t=time.time()
print("The 40th fibric is:",fib(40))
print("fibn time:",time.time()-t)
```

结果如下：

```bash
[yafeng@ArchV ~]$ python micromark.py
bigloop time: 60.44254755973816
The 40th fibric is: 102334155
fibn time: 48.39746880531311
```

```bash
[yafeng@ArchV ~]$ micropython micromark.py
bigloop time: 51.92846608161926
The 40th fibric is: 102334155
fibn time: 65.70703196525574
```

```
可以看到，效率基本是一样的，循环micropython稍快一点，递归cpython稍快一点，顺便贴一下pypy pypy3的结果：
```

```bash
[yafeng@ArchV ~]$ pypy micromark.py
('bigloop time:', 1.7053859233856201)
('The 40th fibric is:', 102334155)
('fibn time:', 7.795623064041138)
[yafeng@ArchV ~]$ pypy3 micromark.py
bigloop time: 1.2033970355987549
The 40th fibric is: 102334155
fibn time: 7.820451974868774
```

可以看到，pypy速都还是很明显的，喜闻乐见的是，pypy3甚至超过了pypy。

下边在搞一下优化，micropython有@micropython.native,@micropython.viper两个可以提速的装饰器，通过翻译成机器码来提速，结果如下：

@micropython.native

```bash
[yafeng@ArchV ~]$ micropython micromark.py
bigloop time: 23.538330078125
The 40th fibric is: 102334155
fibn time: 40.44101715087891
```

@micropython.viper

[yafeng@ArchV ~]$ micropython micromark.py  
bigloop time: 5.683197021484375  
The 40th fibric is: 102334155  
fibn time: 24.39595413208008

其中fib部分由于返回值类型不固定viper失败，所以改成了如下方式：

```python
@micropython.viper
def num(x):
    return x

@micropython.viper
def fib(n:int)->object:
    if n==0:
        return num(0)
    if n==1:
        return num(1)
    return fib(n-1)+fib(n-2)
```

对性能有一定的影响，同代码的native模式为52秒，直接解释执行是87秒

也就是说，viper性能在循环方面，4倍于native 10倍于直接解释。递归方面，viper速度是native的2倍是直接执行的3倍

当然，跟pypy的1.2秒，7.8秒还是慢3倍以上。

结论：

1.micropython解释器的速度跟cpython差不多，都低于pypy

2.通过native或者viper两个装饰器，可以加速代码，能到pypy一个数量级（慢3倍）

其实个人觉得python也可以考虑加上类似的技术来加速代码，毕竟没多少代码量。
