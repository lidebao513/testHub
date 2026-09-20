"""testgen 集成 app：testhub 经 HTTP 调用 testgen sidecar，并把产物映射回流为 TestCase。

铁律（阶段 M1）：testgen 业务逻辑零改；本 app 仅新增文件，不动现有 21 app；
映射层只做字段翻译，不搬 testgen 代码。
"""
