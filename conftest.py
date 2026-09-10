import os

# 必须在导入 app 之前设置，保证测试使用内存数据库
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
