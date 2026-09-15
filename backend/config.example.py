"""
数据库连接配置 —— 模板（复制为 config.py 即可；config.py 已被 .gitignore 排除）

【配置优先级】系统环境变量 > 项目根目录 .env > 本文件内置兜底值
敏感信息（DB 密码、DeepSeek Key）不要写在这里，放到项目根目录的 .env
（复制 .env.example 为 .env 后填入真实值；.env 已被 .gitignore 排除）。

【数据源策略】默认指向「内网自建 MySQL」192.168.2.10:3306，库名「数据」。
禁止指向线上腾讯云数据库（119.45.187.154 / julangkeji.site），不要用 SSH 隧道端口 3307。
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
    'host': os.environ.get('DB_HOST', '192.168.2.10'),
    'port': int(os.environ.get('DB_PORT', '3306')),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', ''),
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
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_API_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-chat'

# 选品助手智能体专用 DeepSeek Key（与全局 key 隔离，不影响其它 AI 功能）
DEEPSEEK_SELECTION_API_KEY = os.environ.get('DEEPSEEK_SELECTION_API_KEY', '')

# ---- 启动自检：敏感项缺失 / 数据源误配，都在这里提示（正常配置时静默）----
# 线上站点（julangkeji.site）使用 .deploy/stage/backend/config.py，与本文件互不影响。
_FORBIDDEN_HOSTS = {'119.45.187.154', 'julangkeji.site', 'www.julangkeji.site'}
_WARNINGS = []

if DB_CONFIG['host'] in _FORBIDDEN_HOSTS or DB_CONFIG['port'] == 3307:
    _WARNINGS.append(
        '数据库配置指向了线上腾讯云地址 / SSH 隧道端口：%s:%s\n'
        '         数据入口必须接入自建 MySQL：192.168.2.10:3306（库名「数据」）'
        % (DB_CONFIG['host'], DB_CONFIG['port']))
if not DB_CONFIG['password']:
    _WARNINGS.append('未配置 DB_PASSWORD —— 请在项目根目录 .env 中设置（可从 .env.example 复制）')
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
