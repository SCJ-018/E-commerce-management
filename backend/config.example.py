"""
数据库连接配置 —— 模板（复制为 config.py 即可；config.py 已被 .gitignore 排除）

【配置优先级】系统环境变量 > 项目根目录 .env > 本文件内置兜底值
敏感信息（DB 密码、DeepSeek Key）不要写在这里，放到项目根目录的 .env
（复制 .env.example 为 .env 后填入真实值；.env 已被 .gitignore 排除）。

【数据源策略】数据库端点与凭据必须通过环境变量或项目根目录 .env 显式提供。
本文件不内置任何内网地址、账号、密码或数据库名。
"""
import os


def _load_dotenv():
    """零依赖读取项目根目录的 .env。系统环境变量优先，.env 只补充缺失项。"""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if not os.path.isfile(env_path):
        return False
    try:
        with open(env_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        return True
    except Exception as e:
        print('[警告] 读取 .env 失败：%s' % e)
        return False


HAS_DOTENV = _load_dotenv()

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '').strip(),
    'port': int(os.environ.get('DB_PORT', '3306')),
    'user': os.environ.get('DB_USER', '').strip(),
    'password': os.environ.get('DB_PASSWORD', ''),
    'database': os.environ.get('DB_NAME', '').strip(),
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
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-chat'

# 选品助手智能体专用 DeepSeek Key（与全局 key 隔离，不影响其它 AI 功能）
DEEPSEEK_SELECTION_API_KEY = os.environ.get('DEEPSEEK_SELECTION_API_KEY', '')

# ---- 启动自检：数据库配置缺失时直接失败，禁止隐式回落到历史内网库 ----
_WARNINGS = []

_missing_db = [name for name, value in (
    ('DB_HOST', DB_CONFIG['host']),
    ('DB_USER', DB_CONFIG['user']),
    ('DB_PASSWORD', DB_CONFIG['password']),
    ('DB_NAME', DB_CONFIG['database']),
) if not value]
if _missing_db:
    raise RuntimeError(
        '数据库配置不完整：缺少 %s。请通过环境变量或项目根目录 .env 显式配置；'
        '程序不会回落到任何内网数据库。' % ', '.join(_missing_db)
    )
if not DEEPSEEK_API_KEY:
    _WARNINGS.append('未配置 DEEPSEEK_API_KEY —— AI 功能（每日分析 / 选品 / 种草智能体）将不可用')
if not DEEPSEEK_SELECTION_API_KEY:
    _WARNINGS.append('未配置 DEEPSEEK_SELECTION_API_KEY —— 选品助手将不可用')

if _WARNINGS:
    print('=' * 66)
    print('[配置检查] 数据源：%s:%s/%s（.env：%s）'
          % (DB_CONFIG['host'], DB_CONFIG['port'], DB_CONFIG['database'],
             '已加载' if HAS_DOTENV else '未找到'))
    for _w in _WARNINGS:
        print('  [警告] ' + _w)
    print('=' * 66)
