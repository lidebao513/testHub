"""HTTP 路由分模块。

app.py 保留为「门面/符号容器」：所有被测试 patch 的共享符号（task_store、
executor、compare_one 等）与 Pydantic 模型仍在 app.py 定义，各 router 通过
``from service import app as A`` 在运行时引用，保证 ``patch("service.app.X")``
继续生效。app.py 末尾统一 include 各 router，避免循环导入。
"""
