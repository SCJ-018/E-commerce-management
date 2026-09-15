# -*- coding: utf-8 -*-
"""
人事数据中心 — 5 张人事表的通用 CRUD 接口（Flask Blueprint）

设计说明：
- 技术框架与 app.py 完全一致（Flask + PyMySQL + 同一套 db_execute 连接池），不引入第二套框架。
- 依赖通过 init_hr_api(...) 注入，避免与 app.py 循环导入。
- 表名/列名全部来自本文件静态配置（白名单），不接受前端传入的标识符，杜绝 SQL 注入。
- 5 张表均为复合主键（无单一自增 id），故更新/删除用主键组合定位行。

接口：
    GET    /api/hr/meta              获取 5 张表的元数据（列定义/主键/权限点）
    GET    /api/hr/<key>/rows        查询某表全部数据
    POST   /api/hr/<key>/rows        新增一行
    PUT    /api/hr/<key>/rows        按主键更新一行（body 中 _pk 为主键原值）
    DELETE /api/hr/<key>/rows        按主键删除一行（body 中 _pk 为主键值）
"""
from datetime import date, datetime, time as dtime
from decimal import Decimal

from flask import Blueprint, request, jsonify

hr_bp = Blueprint('hr', __name__, url_prefix='/api/hr')

# 由 app.py 在启动时注入
_db_execute = None
_db_execute_insert = None
_success = None
_fail = None


def init_hr_api(db_execute, db_execute_insert, success, fail):
    """注入 app.py 的数据库连接函数与统一响应函数（避免循环导入）"""
    global _db_execute, _db_execute_insert, _success, _fail
    _db_execute = db_execute
    _db_execute_insert = db_execute_insert
    _success = success
    _fail = fail


# ======================== 表结构配置（与数据库实表一一对应） ========================
# type: text / date / time / number / textarea
# required: 前端标 *，后端强制非空校验
HR_TABLES = {
    'roster': {
        'table': '员工花名册',
        'label': '员工花名册',
        'perm': 'hr-roster',
        'icon': 'fa-solid fa-address-book',
        'color': '#4f46e5',
        'desc': '在职/试用/离职人员全量档案',
        'pk': ['工号', '姓名', '手机号'],
        'order': '`工号` ASC',
        'fields': [
            {'name': '工号', 'type': 'text', 'required': True},
            {'name': '姓名', 'type': 'text', 'required': True},
            {'name': '入职时间', 'type': 'date'},
            {'name': '一级部门', 'type': 'text'},
            {'name': '二级部门', 'type': 'text'},
            {'name': '直带人', 'type': 'text'},
            {'name': '岗级', 'type': 'text'},
            {'name': '手机号', 'type': 'text', 'required': True},
            {'name': '身份证号', 'type': 'text'},
            {'name': '紧急联系人电话', 'type': 'text'},
            {'name': '状态', 'type': 'text', 'options': ['在职', '试用期', '离职', '待入职']},
            {'name': '薪资待遇', 'type': 'text'},
            {'name': '转正日期', 'type': 'date'},
            {'name': '银行卡号', 'type': 'text'},
            {'name': '开户行', 'type': 'text'},
            {'name': '主人事', 'type': 'text'},
        ],
    },
    'interview': {
        'table': '面试信息登记表',
        'label': '面试信息登记表',
        'perm': 'hr-interview',
        'icon': 'fa-solid fa-comments',
        'color': '#0891b2',
        'desc': '面试邀约、到场与结果记录',
        'pk': ['姓名', '年龄', '日期'],
        'order': '`日期` DESC, `时间` DESC',
        'fields': [
            {'name': '姓名', 'type': 'text', 'required': True},
            {'name': '性别', 'type': 'text', 'options': ['男', '女']},
            {'name': '年龄', 'type': 'text', 'required': True},
            {'name': '岗位', 'type': 'text'},
            {'name': '日期', 'type': 'date', 'required': True},
            {'name': '时间', 'type': 'time', 'required': True},
            {'name': '邀约人', 'type': 'text'},
            {'name': '结果', 'type': 'text', 'options': ['通过', '未通过', '待定', '已入职', '放弃']},
            {'name': '备注', 'type': 'textarea'},
        ],
    },
    'onboarding': {
        'table': '入职人员信息统计表',
        'label': '入职人员信息统计表',
        'perm': 'hr-onboarding',
        'icon': 'fa-solid fa-user-plus',
        'color': '#059669',
        'desc': '面试到入职转化与 1/3/7 天留存',
        'pk': ['姓名', '年龄', '面试日期', '入职日期'],
        'order': '`入职日期` DESC, `面试日期` DESC',
        'fields': [
            {'name': '姓名', 'type': 'text', 'required': True},
            {'name': '性别', 'type': 'text', 'options': ['男', '女']},
            {'name': '年龄', 'type': 'text', 'required': True},
            {'name': '岗位', 'type': 'text'},
            {'name': '面试日期', 'type': 'date', 'required': True},
            {'name': '入职日期', 'type': 'date', 'required': True},
            {'name': '入职情况', 'type': 'text', 'options': ['已入职', '未入职', '放弃', '待定']},
            {'name': '定岗', 'type': 'text'},
            {'name': '第一天', 'type': 'text', 'options': ['正常', '异常', '离职']},
            {'name': '第三天', 'type': 'text', 'options': ['正常', '异常', '离职']},
            {'name': '第七天', 'type': 'text', 'options': ['正常', '异常', '离职']},
            {'name': '邀约人', 'type': 'text'},
            {'name': '一级部门', 'type': 'text'},
            {'name': '二级部门', 'type': 'text'},
            {'name': '直带人', 'type': 'text'},
            {'name': '备注', 'type': 'textarea'},
        ],
    },
    'salary-a': {
        'table': '人员薪资标准（表 a）',
        'label': '人员薪资标准（表 a）',
        'perm': 'hr-salary-a',
        'icon': 'fa-solid fa-money-bill-wave',
        'color': '#d97706',
        'desc': '人员档案与转正、银行卡信息',
        'pk': ['部门', '姓名', '身份证号'],
        'order': '`部门` ASC, `姓名` ASC',
        'fields': [
            {'name': '部门', 'type': 'text', 'required': True},
            {'name': '直带人', 'type': 'text'},
            {'name': '姓名', 'type': 'text', 'required': True},
            {'name': '性别', 'type': 'text', 'options': ['男', '女']},
            {'name': '驾驶证', 'type': 'text', 'options': ['有', '无']},
            {'name': '门禁', 'type': 'text', 'options': ['已开通', '未开通']},
            {'name': '联系方式', 'type': 'text'},
            {'name': '身份证号', 'type': 'text', 'required': True},
            {'name': '年龄', 'type': 'text'},
            {'name': '入职时间', 'type': 'date'},
            {'name': '人员类型', 'type': 'text', 'options': ['正式', '试用', '实习', '兼职', '外包']},
            {'name': '转正月份实际', 'type': 'date'},
            {'name': '开户行', 'type': 'text'},
            {'name': '银行卡号', 'type': 'text'},
            {'name': '主人事', 'type': 'text'},
            {'name': '备注', 'type': 'textarea'},
        ],
    },
    'salary-b': {
        'table': '人员薪资标准（表 b）',
        'label': '人员薪资标准（表 b）',
        'perm': 'hr-salary-b',
        'icon': 'fa-solid fa-sack-dollar',
        'color': '#e11d48',
        'desc': '基本/绩效/全勤/综合薪资标准',
        'pk': ['部门', '姓名', '身份证号'],
        'order': '`部门` ASC, `姓名` ASC',
        'fields': [
            {'name': '部门', 'type': 'text', 'required': True},
            {'name': '姓名', 'type': 'text', 'required': True},
            {'name': '性别', 'type': 'text', 'options': ['男', '女']},
            {'name': '驾驶证', 'type': 'text', 'options': ['有', '无']},
            {'name': '门禁', 'type': 'text', 'options': ['已开通', '未开通']},
            {'name': '联系方式', 'type': 'text'},
            {'name': '身份证号', 'type': 'text', 'required': True},
            {'name': '年龄', 'type': 'text'},
            {'name': '职级', 'type': 'text'},
            {'name': '入职时间', 'type': 'text'},
            {'name': '基本薪资', 'type': 'number'},
            {'name': '绩效', 'type': 'number'},
            {'name': '全勤', 'type': 'number'},
            {'name': '综合薪资', 'type': 'number'},
            {'name': '试用期', 'type': 'text', 'options': ['是', '否']},
            {'name': '试用薪资', 'type': 'number'},
            {'name': '人员类型', 'type': 'text', 'options': ['正式', '试用', '实习', '兼职', '外包']},
            {'name': '转正月份实际', 'type': 'text'},
            {'name': '开户行', 'type': 'text'},
            {'name': '银行卡号', 'type': 'text'},
            {'name': '主人事', 'type': 'text'},
            {'name': '备注', 'type': 'textarea'},
        ],
    },
}

# 列元数据缓存：{ table: { field: {'type':..., 'nullable': bool} } }
_COLUMN_CACHE = {}


def _cfg(key):
    return HR_TABLES.get(key)


def _column_meta(table):
    """读取实表列类型/可空性（缓存，用于值的强制转换）"""
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    rows = _db_execute('SHOW FULL COLUMNS FROM `%s`' % table)
    meta = {r['Field']: {'type': (r['Type'] or '').lower(),
                         'nullable': (r.get('Null', 'NO') == 'YES')} for r in rows}
    _COLUMN_CACHE[table] = meta
    return meta


def _td_to_str(v):
    """MySQL TIME 列在 PyMySQL 中返回 timedelta，转成 HH:MM:SS"""
    total = int(v.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return '%02d:%02d:%02d' % (h, m, s)


def _serialize(rows):
    """date/datetime/time/Decimal/timedelta → JSON 兼容类型"""
    from datetime import timedelta as _td
    for r in rows:
        for k, v in r.items():
            if isinstance(v, _td):
                r[k] = _td_to_str(v)
            elif isinstance(v, (date, datetime)):
                r[k] = str(v)
            elif isinstance(v, dtime):
                r[k] = v.strftime('%H:%M:%S')
            elif isinstance(v, Decimal):
                r[k] = float(v)
            elif isinstance(v, (bytes, bytearray)):
                r[k] = v.decode('utf-8', 'ignore')
            elif v is None:
                r[k] = ''
    return rows


def _coerce(table, field, value):
    """按列类型转换空值：
    - 日期/时间列且不可为空：空值回退到 1900-01-01 / 00:00:00（严格模式下 NULL/'' 都会报错）
    - 其余列空值 → ''（varchar NOT NULL 可以存空串）
    """
    meta = _column_meta(table).get(field, {})
    col_type = meta.get('type', '')
    nullable = meta.get('nullable', True)

    v = '' if value is None else str(value).strip()

    if 'date' in col_type and 'datetime' not in col_type:
        return None if (not v and nullable) else (v or '1900-01-01')
    if 'datetime' in col_type or 'timestamp' in col_type:
        return None if (not v and nullable) else (v or '1900-01-01 00:00:00')
    if 'time' in col_type:
        return None if (not v and nullable) else (v or '00:00:00')
    return v


def _pk_filter(cfg, pk_values):
    """构造 WHERE 主键条件与参数"""
    clauses, params = [], []
    for col in cfg['pk']:
        val = pk_values.get(col)
        if val is None or str(val).strip() == '':
            raise ValueError('主键字段缺失: %s' % col)
        clauses.append('`%s` = %%s' % col)
        params.append(str(val).strip())
    return ' AND '.join(clauses), params


# ======================== 接口 ========================

@hr_bp.route('/meta', methods=['GET'])
def hr_meta():
    """5 张表的元数据（列定义、主键、权限点），供前端渲染表头/表单"""
    try:
        out = []
        for key, cfg in HR_TABLES.items():
            out.append({
                'key': key,
                'label': cfg['label'],
                'table': cfg['table'],
                'perm': cfg['perm'],
                'icon': cfg['icon'],
                'color': cfg['color'],
                'desc': cfg['desc'],
                'pk': cfg['pk'],
                'fields': cfg['fields'],
            })
        return _success(out)
    except Exception as e:
        return _fail(str(e))


@hr_bp.route('/counts', methods=['GET'])
def hr_counts():
    """各表行数（供卡片选择器显示条数）"""
    try:
        out = {}
        for key, cfg in HR_TABLES.items():
            row = _db_execute('SELECT COUNT(*) AS c FROM `%s`' % cfg['table'])
            out[key] = row[0]['c'] if row else 0
        return _success(out)
    except Exception as e:
        return _fail(str(e))


@hr_bp.route('/<key>/rows', methods=['GET'])
def hr_list(key):
    """查询某表全部数据（人事数据量小，前端本地做搜索/分页/统计）"""
    try:
        cfg = _cfg(key)
        if not cfg:
            return _fail('未知的数据表: %s' % key)
        rows = _db_execute('SELECT * FROM `%s` ORDER BY %s' % (cfg['table'], cfg['order']))
        return _success(_serialize(rows))
    except Exception as e:
        return _fail(str(e))


@hr_bp.route('/<key>/rows', methods=['POST'])
def hr_create(key):
    """新增一行（未传的列写空串/默认值）"""
    try:
        cfg = _cfg(key)
        if not cfg:
            return _fail('未知的数据表: %s' % key)
        data = request.get_json(force=True, silent=True) or {}

        for f in cfg['fields']:
            if f.get('required') and not str(data.get(f['name']) or '').strip():
                return _fail('%s 为必填项' % f['name'])

        cols = [f['name'] for f in cfg['fields']]
        values = [_coerce(cfg['table'], f['name'], data.get(f['name'])) for f in cfg['fields']]
        sql = 'INSERT INTO `%s` (`%s`) VALUES (%s)' % (
            cfg['table'], '`,`'.join(cols), ','.join(['%s'] * len(cols)))
        _db_execute_insert(sql, values)
        return _success(None, '新增成功')
    except Exception as e:
        return _fail(str(e))


@hr_bp.route('/<key>/rows', methods=['PUT'])
def hr_update(key):
    """按主键更新一行：body = { _pk: {列名: 原值}, 其余列: 新值 }"""
    try:
        cfg = _cfg(key)
        if not cfg:
            return _fail('未知的数据表: %s' % key)
        data = request.get_json(force=True, silent=True) or {}
        pk_values = data.get('_pk') or {}
        where, pk_params = _pk_filter(cfg, pk_values)

        sets, vals = [], []
        for f in cfg['fields']:
            if f['name'] in cfg['pk']:
                continue  # 主键不参与 SET（改主键等于换一行，前端不允许编辑主键）
            sets.append('`%s` = %%s' % f['name'])
            vals.append(_coerce(cfg['table'], f['name'], data.get(f['name'])))

        if not sets:
            return _fail('没有可更新的字段')
        sql = 'UPDATE `%s` SET %s WHERE %s' % (cfg['table'], ', '.join(sets), where)
        affected = _db_execute(sql, vals + pk_params, fetch=False)
        if not affected:
            return _fail('未找到对应记录，可能已被修改，请刷新后重试')
        return _success({'affected': affected}, '已保存')
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(str(e))


@hr_bp.route('/<key>/rows', methods=['DELETE'])
def hr_delete(key):
    """按主键删除一行：body = { _pk: {列名: 值} }"""
    try:
        cfg = _cfg(key)
        if not cfg:
            return _fail('未知的数据表: %s' % key)
        data = request.get_json(force=True, silent=True) or {}
        where, params = _pk_filter(cfg, data.get('_pk') or {})
        affected = _db_execute('DELETE FROM `%s` WHERE %s' % (cfg['table'], where), params, fetch=False)
        if not affected:
            return _fail('未找到对应记录，请刷新后重试')
        return _success({'affected': affected}, '已删除')
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(str(e))
