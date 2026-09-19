---
title: "web编程速度大比拼（nodejs go python）（非专业对比）"
date: 2016-04-25T17:17:00+08:00
categories: ['归档']
tags: [Node.js, Go, Python, Web性能]
original: "https://www.cnblogs.com/yafengabc/p/5431695.html"
draft: false
---

C10K问题的解决，涌现出一大批新框架，或者新语言，那么问题来了:到底谁最快呢？非专业程序猿来个非专业对比。

比较程序：输出Hello World！

测试程序：siege –c 100 –r 100 –b

例子包括：

1.go用http模块实现的helloworld

2.go用martini微框架实现的Helloworld

3.python3 python2 pypy分别用gevent server  tornado实现的Hello world

4.python3 python2 pypy分别用微框架bottle+gevent实现的Hello world

5.NodeJS纯JS实现的Helloworld

6.NodeJS用express框架实现的Helloworld

测试平台：

公司老旧的奔腾平台 Pentium(R) Dual-Core  CPU      E6700  @ 3.20GHz

内存2GB（够弱了吧）

先来宇宙最快的GO的测试：

```go
package main

import (
        "fmt"
        "net/http"
)

func sayhelloName(w http.ResponseWriter, r *http.Request){
        fmt.Fprintf(w, "hello world!")
}

func main() {
        http.HandleFunc("/", sayhelloName)
        http.ListenAndServe(":9090", nil)
}
```

连续测试5次，成绩大体如下：

```
Transactions:                  10000 hits
Availability:                 100.00 %
Elapsed time:                   4.11 secs
Data transferred:               0.11 MB
Response time:                  0.03 secs
Transaction rate:            2433.09 trans/sec
Throughput:                     0.03 MB/sec
Concurrency:                   79.76
Successful transactions:       10000
Failed transactions:               0
Longest transaction:            0.20
Shortest transaction:           0.00
```

4.11秒，不错的成绩

再看NodeJS的例子：

```javascript
var http = require("http");
http.createServer(function(request, response) {
    response.writeHead(200, {"Content-Type": "text/plain"});
    response.write("Hello World!");
    response.end();
}).listen(8000);
console.log("nodejs start listen 8888 port!");

```

测试结果如下：

```
Transactions:                  10000 hits
Availability:                 100.00 %
Elapsed time:                   5.00 secs
Data transferred:               0.11 MB
Response time:                  0.04 secs
Transaction rate:            2000.00 trans/sec
Throughput:                     0.02 MB/sec
Concurrency:                   86.84
Successful transactions:       10000
Failed transactions:               0
Longest transaction:            0.17
Shortest transaction:           0.00
```

5秒，比Go稍微慢一点

接下来是Python，由于python自带的wsgiref服务器，只是一个参考实现，性能很差，所以这里选用了两个性能不错的WSGI服务器gevent、tornado

gevent代码如下：

```python
#!/usr/bin/python
from gevent import pywsgi

def hello_world(env, start_response):
    if env['PATH_INFO'] == '/':
        start_response('200 OK', [('Content-Type', 'text/html')])
        return ["hello world"]

print 'Serving on https://127.0.0.1:8000'
server = pywsgi.WSGIServer(('0.0.0.0', 8000), hello_world )
server.serve_forever()

```

tornado的代码如下：

```python
from tornado import httpserver
from tornado import ioloop
def handle_request(request):
    if request.uri=='/':
        message = b"Hello World!"
        request.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (
                 len(message), message))
        request.finish()

http_server = httpserver.HTTPServer(handle_request)
http_server.bind(8888)
http_server.start()
ioloop.IOLoop.instance().start()

```

由于python的例子要分别在python2 python3 pypy下跑，结果太多，我这里只给结果：

gevent：

python2：5.8秒，python3：7.5秒，pypy：4.8秒

有意思的是：pypy第一次跑用了6.8秒，第二次以后全是4.8秒（感觉原因是第一次由于jit浪费了一点时间）贴出某次pypy的成绩：

```
Transactions:                  10000 hits
Availability:                 100.00 %
Elapsed time:                   4.77 secs
Data transferred:               0.10 MB
Response time:                  0.04 secs
Transaction rate:            2096.44 trans/sec
Throughput:                     0.02 MB/sec
Concurrency:                   90.38
Successful transactions:       10000
Failed transactions:               0
Longest transaction:            0.13
Shortest transaction:           0.00

```

接下来是tornado：

python2：9.05秒，python3：8.6秒，pypy：5.95秒

同样：pypy第一次执行的时间为9.45秒，以后的每次只执行时间为5.9秒多一些

可以看出，pypy 与go nodejs性能相当，其中go最快为4.11秒，pypy+gevent与nodejs性能差不多，pypy稍好一点，pypy+tornado则稍慢于nodejs。

2。框架篇：

从上边例子可以看到，纯代码写起来还是比较麻烦的，一般我们都是用框架来写web，我选了几个轻量级的框架来输出helloworld：

go+martini

```go
package main

import "github.com/codegangsta/martini"

func main() {
  m := martini.Classic()
  m.Get("/", func() string {
    return "Hello world!"
  })
  m.Run()
}

```

运行时间为：

```
Transactions:                  10000 hits
Availability:                 100.00 %
Elapsed time:                   4.69 secs
Data transferred:               0.11 MB
Response time:                  0.04 secs
Transaction rate:            2132.20 trans/sec
Throughput:                     0.02 MB/sec
Concurrency:                   90.23
Successful transactions:       10000
Failed transactions:               0
Longest transaction:            0.17
Shortest transaction:           0.00

```

nodejs+express：

```javascript
var express = require('express')
var app = express()
 
app.get('/', function (req, res) {
  res.send('Hello World')
})
 
app.listen(3000)
```

用时：

```
Transactions:                  10000 hits
Availability:                 100.00 %
Elapsed time:                   5.90 secs
Data transferred:               0.10 MB
Response time:                  0.06 secs
Transaction rate:            1694.92 trans/sec
Throughput:                     0.02 MB/sec
Concurrency:                   96.44
Successful transactions:       10000
Failed transactions:               0
Longest transaction:            0.13
Shortest transaction:           0.01

```

python gevent+bottle：

```python
from gevent import monkey
monkey.patch_all()
from bottle import run,get

@get("/")
def index():
    return "Hello world!"

run(server='gevent')

```

用时：python2 10.05秒，python3：12.95秒，pypy：5.85秒

python tornado：

```python
import tornado.httpserver
import tornado.ioloop
import tornado.web

class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.write('Hello World!')

if __name__ == "__main__":
    app = tornado.web.Application(handlers=[(r"/",IndexHandler)])
    http_server = tornado.httpserver.HTTPServer(app)
    http_server.listen(8000)
    tornado.ioloop.IOLoop.instance().start()

```

用时：python2 11.85秒，python3：11.79秒，pypy：6.65秒

总结：可以看到，python在开启jit技术的pypy上web响应速度已经略优于nodejs，跟golang还有一定差距，但在同一数量级，标准python就稍微慢一些。

