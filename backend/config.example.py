"""
数据库连接配置
根据实际情况修改以下参数
（复制本文件为 config.py 并填入真实值；config.py 已被 .gitignore 排除，不会被提交）
敏感字段（密码等）支持从环境变量读取，未设置时回退到这里的默认值
"""
import os

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '192.168.2.10'),
    'port': int(os.environ.get('DB_PORT', '3306')),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', '请填写密码'),
    'database': os.environ.get('DB_NAME', '数据'),
    'charset': 'utf8mb4',
    'autocommit': True,
    # 连接超时 & 自动重连
    'connect_timeout': 5,
    'read_timeout': 10,
    'write_timeout': 10,
}

# 连接池大小
DB_POOL_SIZE = 5

# 是否在每次查询前 ping 检测连接存活（推荐开启，防止 "MySQL server has gone away"）
DB_PING_BEFORE_QUERY = True

# DeepSeek API 配置
DEEPSEEK_API_KEY = '请填写 API 密钥'
DEEPSEEK_API_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-chat'

# 选品助手智能体专用 DeepSeek Key（与全局 key 隔离，不影响其它 AI 功能）
DEEPSEEK_SELECTION_API_KEY = '请填写选品助手专用 API 密钥'
