#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据导入工具 — 将 Excel/CSV 文件导入 MySQL 数据库
支持：文件预览、字段映射、自动去重
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import csv
import os
import sys
import threading
import traceback
from datetime import datetime, date

# ─── 可选依赖检测 ───────────────────────────────────────────
try:
    import pymysql
    HAS_PYMYSQL = True
except ImportError:
    HAS_PYMYSQL = False

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    import xlrd
    HAS_XLRD = True
except ImportError:
    HAS_XLRD = False

# ─── 默认数据库配置 ─────────────────────────────────────────
# 【唯一数据源】所有数据入口统一指向「内网自建 MySQL」：192.168.2.10:3306
# 真源是 backend/config.py 的 DB_CONFIG，这里优先复用它，保证导入工具与后端读同一个库。
# ⚠️ 禁止改回 127.0.0.1:3307（那是 SSH 隧道到腾讯云服务器的库），会造成
#    「导入成功但网页看不到」的两库分裂问题。
# ⚠️ 禁止指向腾讯云 119.45.187.154。
_FALLBACK_DB = {
    'host': '192.168.2.10',   # 内网自建 MySQL
    'port': 3306,
    'user': 'root',
    'password': os.environ.get('DB_PASSWORD', '123456'),
    'database': '数据',
    'charset': 'utf8mb4',
}


def _load_default_db():
    """优先复用 backend/config.py 的 DB_CONFIG；取不到时用内置的自建库默认值。"""
    try:
        _backend_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend')
        )
        if _backend_dir not in sys.path:
            sys.path.insert(0, _backend_dir)
        from config import DB_CONFIG  # noqa: E402
        cfg = dict(_FALLBACK_DB)
        for k in ('host', 'port', 'user', 'password', 'database', 'charset'):
            if k in DB_CONFIG:
                cfg[k] = DB_CONFIG[k]
        return cfg, 'backend/config.py'
    except Exception:
        return dict(_FALLBACK_DB), '内置默认值'


DEFAULT_DB, DB_SOURCE = _load_default_db()


# ═══════════════════════════════════════════════════════════════
#  应用程序类
# ═══════════════════════════════════════════════════════════════
class ImportToolApp:
    def __init__(self, root):
        self.root = root
        self.root.title('数据导入工具 — MySQL Excel/CSV 导入')
        self.root.geometry('1050x780')
        self.root.minsize(900, 650)

        # 状态变量
        self.conn = None
        self.file_columns = []          # 文件列名
        self.file_data = []             # 文件数据 [{col: val}, ...]
        self.file_path = tk.StringVar()
        self.file_ext = ''              # 文件扩展名
        self.sheet_names = []           # Excel 工作表名列表
        self._wb_cache = None           # 缓存打开的 workbook（避免重复读取）
        self.table_columns = []         # 数据库表字段
        self.table_column_types = {}    # 字段类型映射
        self.pk_columns = []            # 主键列
        self._cancel_flag = threading.Event()

        # 连接配置变量
        self.var_host = tk.StringVar(value=DEFAULT_DB['host'])
        self.var_port = tk.StringVar(value=str(DEFAULT_DB['port']))
        self.var_user = tk.StringVar(value=DEFAULT_DB['user'])
        self.var_pass = tk.StringVar(value=DEFAULT_DB['password'])
        self.var_db   = tk.StringVar(value=DEFAULT_DB['database'])
        self.var_char = tk.StringVar(value=DEFAULT_DB['charset'])
        self.var_table = tk.StringVar()
        self.var_sheet = tk.StringVar()
        self.var_order_col = tk.StringVar()
        self.var_order_dir = tk.StringVar(value='DESC')

        self._build_ui()
        self._log('数据导入工具已启动')
        self._log('默认数据源：%s:%s/%s（来源：%s）'
                  % (DEFAULT_DB['host'], DEFAULT_DB['port'], DEFAULT_DB['database'], DB_SOURCE))
        self._auto_connect()

    # ── GUI 构建 ──────────────────────────────────────────
    def _build_ui(self):
        # 主容器
        main_frame = ttk.Frame(self.root, padding=8)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 使用 Notebook 分页，简化布局
        nb = ttk.Notebook(main_frame)
        nb.pack(fill=tk.BOTH, expand=True)

        # Tab 1: 导入配置
        tab_import = ttk.Frame(nb, padding=8)
        nb.add(tab_import, text='导入配置')

        # Tab 2: 日志
        tab_log = ttk.Frame(nb, padding=8)
        nb.add(tab_log, text='日志')

        self._build_db_frame(tab_import)
        self._build_file_frame(tab_import)
        self._build_table_frame(tab_import)
        self._build_dedup_frame(tab_import)
        self._build_action_frame(tab_import)
        self._build_log_frame(tab_log)

    def _build_db_frame(self, parent):
        """数据库连接配置"""
        f = ttk.LabelFrame(parent, text='数据库连接', padding=6)
        f.pack(fill=tk.X, pady=(0, 6))

        r1 = ttk.Frame(f)
        r1.pack(fill=tk.X, pady=2)
        ttk.Label(r1, text='主机:').pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_host, width=18).pack(side=tk.LEFT, padx=(4, 16))
        ttk.Label(r1, text='端口:').pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_port, width=7).pack(side=tk.LEFT, padx=(4, 16))
        ttk.Label(r1, text='用户:').pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_user, width=14).pack(side=tk.LEFT, padx=(4, 16))
        ttk.Label(r1, text='密码:').pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self.var_pass, show='*', width=14).pack(side=tk.LEFT, padx=4)

        r2 = ttk.Frame(f)
        r2.pack(fill=tk.X, pady=2)
        ttk.Label(r2, text='数据库:').pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=self.var_db, width=18).pack(side=tk.LEFT, padx=(4, 16))
        ttk.Label(r2, text='字符集:').pack(side=tk.LEFT)
        ttk.Combobox(r2, textvariable=self.var_char, values=['utf8mb4', 'utf8', 'gbk', 'gb2312'], width=12, state='readonly').pack(side=tk.LEFT, padx=(4, 16))

        self.btn_connect = ttk.Button(r2, text='连接数据库', command=self._do_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=4)
        self.btn_refresh_tables = ttk.Button(r2, text='刷新表列表', command=self._do_refresh_tables, state=tk.DISABLED)
        self.btn_refresh_tables.pack(side=tk.LEFT, padx=4)

        self.lbl_conn_status = ttk.Label(r2, text='未连接', foreground='red')
        self.lbl_conn_status.pack(side=tk.LEFT, padx=12)

        # 数据源提示：始终提醒当前指向的是自建 MySQL，避免误连线上库
        r3 = ttk.Frame(f)
        r3.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(
            r3,
            text='数据源：%s:%s/%s（内网自建 MySQL · 配置来源 %s）'
                 % (DEFAULT_DB['host'], DEFAULT_DB['port'], DEFAULT_DB['database'], DB_SOURCE),
            foreground='#0d9488',
        ).pack(side=tk.LEFT)

    def _build_file_frame(self, parent):
        """文件选择"""
        f = ttk.LabelFrame(parent, text='选择文件', padding=6)
        f.pack(fill=tk.X, pady=(0, 6))

        r1 = ttk.Frame(f)
        r1.pack(fill=tk.X)
        ttk.Entry(r1, textvariable=self.file_path, state='readonly').pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(r1, text='浏览...', command=self._do_browse, width=10).pack(side=tk.RIGHT)

        # Sheet 选择行（仅 Excel 时显示）
        self.sheet_frame = ttk.Frame(f)
        ttk.Label(self.sheet_frame, text='工作表:').pack(side=tk.LEFT)
        self.cmb_sheet = ttk.Combobox(self.sheet_frame, textvariable=self.var_sheet, state='readonly', width=28)
        self.cmb_sheet.pack(side=tk.LEFT, padx=6)
        self.cmb_sheet.bind('<<ComboboxSelected>>', self._on_sheet_selected)

        self.lbl_file_info = ttk.Label(f, text='未选择文件', foreground='gray')
        self.lbl_file_info.pack(anchor=tk.W, pady=2)

        # 预览表格
        preview_frame = ttk.Frame(f)
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=4)
        self.tree_preview = ttk.Treeview(preview_frame, height=5, show='headings')
        vsb = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL, command=self.tree_preview.yview)
        hsb = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL, command=self.tree_preview.xview)
        self.tree_preview.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_preview.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)

    def _build_table_frame(self, parent):
        """目标表选择"""
        f = ttk.LabelFrame(parent, text='目标数据库表', padding=6)
        f.pack(fill=tk.X, pady=(0, 6))

        r1 = ttk.Frame(f)
        r1.pack(fill=tk.X, pady=2)
        ttk.Label(r1, text='选择表:').pack(side=tk.LEFT)
        self.cmb_table = ttk.Combobox(r1, textvariable=self.var_table, state='readonly', width=30)
        self.cmb_table.pack(side=tk.LEFT, padx=6)
        self.cmb_table.bind('<<ComboboxSelected>>', self._on_table_selected)
        ttk.Button(r1, text='加载表结构', command=self._do_load_table_info, width=12).pack(side=tk.LEFT, padx=4)

        # 表字段显示
        self.lbl_table_cols = ttk.Label(f, text='表字段: (请先选择表)', foreground='gray')
        self.lbl_table_cols.pack(anchor=tk.W, pady=2)

        # 字段匹配状态
        self.lbl_match_status = ttk.Label(f, text='', foreground='gray')
        self.lbl_match_status.pack(anchor=tk.W)

    def _build_dedup_frame(self, parent):
        """去重设置"""
        f = ttk.LabelFrame(parent, text='去重设置（导入后自动执行）', padding=6)
        f.pack(fill=tk.X, pady=(0, 6))

        self.var_enable_dedup = tk.BooleanVar(value=True)

        r1 = ttk.Frame(f)
        r1.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(r1, text='启用去重', variable=self.var_enable_dedup).pack(side=tk.LEFT)

        ttk.Label(r1, text='  排序字段(值越大越新):').pack(side=tk.LEFT, padx=(16, 4))
        self.cmb_order_col = ttk.Combobox(r1, textvariable=self.var_order_col, state='readonly', width=18)
        self.cmb_order_col.pack(side=tk.LEFT, padx=4)
        ttk.Label(r1, text='排序:').pack(side=tk.LEFT)
        ttk.Combobox(r1, textvariable=self.var_order_dir, values=['DESC', 'ASC'], state='readonly', width=6).pack(side=tk.LEFT, padx=4)

        # 去重字段选择
        r2 = ttk.Frame(f)
        r2.pack(fill=tk.X, pady=4)
        ttk.Label(r2, text='去重依据字段（多选，这些字段值相同的视为重复）:').pack(anchor=tk.W)

        dedup_list_frame = ttk.Frame(f)
        dedup_list_frame.pack(fill=tk.BOTH, expand=True)

        self.lb_dedup = tk.Listbox(dedup_list_frame, selectmode=tk.MULTIPLE, height=4, exportselection=False)
        sb_dedup = ttk.Scrollbar(dedup_list_frame, orient=tk.VERTICAL, command=self.lb_dedup.yview)
        self.lb_dedup.configure(yscrollcommand=sb_dedup.set)
        self.lb_dedup.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_dedup.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_action_frame(self, parent):
        """操作按钮"""
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=(4, 6))

        self.btn_import = ttk.Button(f, text='开始导入', command=self._do_import, width=16)
        self.btn_import.pack(side=tk.LEFT, padx=4)
        self.btn_cancel = ttk.Button(f, text='取消', command=self._do_cancel, state=tk.DISABLED, width=8)
        self.btn_cancel.pack(side=tk.LEFT, padx=4)
        ttk.Button(f, text='清空日志', command=self._do_clear_log, width=10).pack(side=tk.RIGHT, padx=4)

        self.progress = ttk.Progressbar(f, length=300, mode='determinate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12)
        self.lbl_progress = ttk.Label(f, text='就绪', foreground='gray')
        self.lbl_progress.pack(side=tk.LEFT)

    def _build_log_frame(self, parent):
        """日志输出"""
        f = ttk.Frame(parent)
        f.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(f, wrap=tk.WORD, state=tk.DISABLED, font=('Consolas', 9))
        sb = ttk.Scrollbar(f, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── 日志 ──────────────────────────────────────────────
    def _log(self, msg, level='INFO'):
        """线程安全日志"""
        def _write():
            ts = datetime.now().strftime('%H:%M:%S')
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, f'[{ts}] {msg}\n')
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(0, _write)

    def _do_clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete('1.0', tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ── 数据库操作 ────────────────────────────────────────
    def _auto_connect(self):
        """启动时自动尝试连接"""
        self._log('正在自动连接数据库...')
        t = threading.Thread(target=self._connect_db, daemon=True)
        t.start()

    def _do_connect(self):
        t = threading.Thread(target=self._connect_db, daemon=True)
        t.start()

    def _connect_db(self):
        if not HAS_PYMYSQL:
            self.root.after(0, lambda: messagebox.showerror('缺少依赖', '请先安装 pymysql:\npip install pymysql'))
            return

        try:
            port = int(self.var_port.get())
        except ValueError:
            self._set_status('端口号无效', 'red')
            return

        try:
            conn = pymysql.connect(
                host=self.var_host.get(),
                port=port,
                user=self.var_user.get(),
                password=self.var_pass.get(),
                database=self.var_db.get(),
                charset=self.var_char.get(),
                connect_timeout=5,
                cursorclass=pymysql.cursors.DictCursor,
            )
            conn.close()
        except Exception as e:
            self._set_status(f'连接失败: {e}', 'red')
            self._log(f'数据库连接失败: {e}', 'ERROR')
            return

        self._set_status('已连接', 'green')
        self._log(f'数据库连接成功: {self.var_host.get()}:{self.var_port.get()}/{self.var_db.get()}')
        self.root.after(0, self._do_refresh_tables)

    def _set_status(self, text, color):
        def _s():
            self.lbl_conn_status.configure(text=text, foreground=color)
        self.root.after(0, _s)

    def _get_conn(self):
        """获取数据库连接"""
        port = int(self.var_port.get())
        return pymysql.connect(
            host=self.var_host.get(), port=port,
            user=self.var_user.get(), password=self.var_pass.get(),
            database=self.var_db.get(), charset=self.var_char.get(),
            connect_timeout=5, read_timeout=30, write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _do_refresh_tables(self):
        t = threading.Thread(target=self._refresh_tables, daemon=True)
        t.start()

    def _refresh_tables(self):
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute('SHOW FULL TABLES WHERE Table_type = %s', ('BASE TABLE',))
                tables = [list(row.values())[0] for row in cur.fetchall()]
            conn.close()

            def _update():
                self.cmb_table['values'] = tables
                self.btn_refresh_tables.configure(state=tk.NORMAL)
            self.root.after(0, _update)
            self._log(f'获取到 {len(tables)} 个表')
        except Exception as e:
            self._log(f'获取表列表失败: {e}', 'ERROR')

    def _on_table_selected(self, event):
        self._do_load_table_info()

    def _do_load_table_info(self):
        table = self.var_table.get().strip()
        if not table:
            return

        t = threading.Thread(target=self._load_table_info, args=(table,), daemon=True)
        t.start()

    def _load_table_info(self, table):
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                # 获取字段信息
                cur.execute(f'SHOW FULL COLUMNS FROM `{table}`')
                cols = cur.fetchall()
                self.table_columns = [c['Field'] for c in cols]
                self.table_column_types = {c['Field']: c['Type'] for c in cols}

                # 获取主键
                cur.execute(f'SHOW KEYS FROM `{table}` WHERE Key_name = %s', ('PRIMARY',))
                pk_rows = cur.fetchall()
                self.pk_columns = [r['Column_name'] for r in pk_rows]
            conn.close()

            cols_text = ', '.join(self.table_columns[:20])
            if len(self.table_columns) > 20:
                cols_text += f' ... (+{len(self.table_columns) - 20})'
            pk_text = ', '.join(self.pk_columns) if self.pk_columns else '(无主键)'

            def _update():
                self.lbl_table_cols.configure(
                    text=f'表字段 ({len(self.table_columns)}个): {cols_text}\n主键: {pk_text}')
                # 更新去重字段列表
                self.lb_dedup.delete(0, tk.END)
                for c in self.table_columns:
                    self.lb_dedup.insert(tk.END, c)
                # 更新排序字段下拉
                self.cmb_order_col['values'] = self.table_columns
                # 自动选排序字段：有日期字段就选日期，否则选主键
                date_candidates = [c for c in self.table_columns if '日期' in c or 'date' in c.lower() or '时间' in c]
                if date_candidates:
                    self.var_order_col.set(date_candidates[0])
                elif self.pk_columns:
                    self.var_order_col.set(self.pk_columns[0])
                elif self.table_columns:
                    self.var_order_col.set(self.table_columns[0])

                # 自动匹配去重字段
                self.lb_dedup.selection_clear(0, tk.END)
                if self.pk_columns:
                    for i, c in enumerate(self.table_columns):
                        if c in self.pk_columns:
                            self.lb_dedup.selection_set(i)

                # 检查字段匹配
                self._check_column_match()
            self.root.after(0, _update)
            self._log(f'表 "{table}": {len(self.table_columns)} 个字段, 主键: {pk_text}')
        except Exception as e:
            self._log(f'加载表结构失败: {e}', 'ERROR')

    # ── 文件操作 ──────────────────────────────────────────
    def _do_browse(self):
        path = filedialog.askopenfilename(
            title='选择数据文件',
            filetypes=[
                ('Excel/CSV 文件', '*.xlsx *.xls *.csv'),
                ('Excel 文件', '*.xlsx *.xls'),
                ('CSV 文件', '*.csv'),
                ('所有文件', '*.*'),
            ])
        if not path:
            return

        self.var_sheet.set('')
        self.sheet_names = []
        t = threading.Thread(target=self._load_file, args=(path, None), daemon=True)
        t.start()

    def _load_file(self, path, sheet_name=None):
        self.file_path.set(path)
        ext = os.path.splitext(path)[1].lower()
        self.file_ext = ext
        try:
            if ext == '.csv':
                data, cols = self._read_csv(path)
                self.sheet_names = []
            elif ext in ('.xlsx', '.xls'):
                data, cols, sheet_names = self._read_excel(path, sheet_name)
                self.sheet_names = sheet_names
            else:
                self._log(f'不支持的文件格式: {ext}', 'ERROR')
                return
        except Exception as e:
            self._log(f'读取文件失败: {e}', 'ERROR')
            messagebox.showerror('文件读取失败', str(e))
            return

        self.file_data = data
        self.file_columns = cols

        sheet_info = f' | 工作表: {sheet_name}' if sheet_name else ''
        self._log(f'文件读取成功: {len(data)} 行, {len(cols)} 列{sheet_info}')

        def _update():
            self.lbl_file_info.configure(
                text=f'行数: {len(data)} | 列数: {len(cols)} | 类型: {ext.upper()}{sheet_info}',
                foreground='black')

            # 更新 Sheet 下拉框
            if self.sheet_names and len(self.sheet_names) > 0:
                self.cmb_sheet['values'] = self.sheet_names
                if sheet_name:
                    self.var_sheet.set(sheet_name)
                elif not self.var_sheet.get():
                    self.var_sheet.set(self.sheet_names[0])
                self.sheet_frame.pack(fill=tk.X, pady=(2, 0), before=self.lbl_file_info)
            else:
                self.sheet_frame.pack_forget()
                self.var_sheet.set('')

            # 更新预览
            self.tree_preview.delete(*self.tree_preview.get_children())
            self.tree_preview['columns'] = cols
            self.tree_preview.heading('#0', text='')
            for c in cols:
                self.tree_preview.heading(c, text=c)
                self.tree_preview.column(c, width=max(80, min(150, len(c) * 14)), anchor=tk.CENTER)

            for row in data[:20]:
                values = [self._fmt_cell(row.get(c, '')) for c in cols]
                self.tree_preview.insert('', tk.END, values=values)

            self._check_column_match()
        self.root.after(0, _update)

    def _on_sheet_selected(self, event):
        """切换工作表时重新加载"""
        sheet = self.var_sheet.get().strip()
        path = self.file_path.get()
        if not sheet or not path:
            return
        self._log(f'切换工作表: {sheet}')
        t = threading.Thread(target=self._load_file, args=(path, sheet), daemon=True)
        t.start()

    def _fmt_cell(self, val):
        if val is None:
            return '(NULL)'
        s = str(val)
        return s[:40] + '...' if len(s) > 40 else s

    def _read_csv(self, path):
        """读取 CSV，自动检测编码"""
        encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'gb18030', 'latin-1']
        for enc in encodings:
            try:
                with open(path, 'r', encoding=enc) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    if rows:
                        cols = [c.strip() for c in (reader.fieldnames or [])]
                        self._log(f'CSV 编码: {enc}')
                        return rows, cols
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError('无法识别 CSV 编码，请另存为 UTF-8 格式')

    def _read_excel(self, path, sheet_name=None):
        """读取 Excel，返回 (rows, cols, sheet_names)"""
        rows = []
        cols = []
        sheet_names = []

        if path.lower().endswith('.xlsx'):
            if not HAS_OPENPYXL:
                raise ImportError('请安装 openpyxl: pip install openpyxl')
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheet_names = wb.sheetnames
            target = sheet_name if sheet_name else sheet_names[0]
            ws = wb[target]
            it = ws.iter_rows(values_only=True)
        else:
            if not HAS_XLRD:
                raise ImportError('请安装 xlrd: pip install xlrd')
            wb = xlrd.open_workbook(path)
            sheet_names = wb.sheet_names()
            target = sheet_name if sheet_name else sheet_names[0]
            ws = wb.sheet_by_name(target)
            it = (ws.row_values(r) for r in range(ws.nrows))

        for i, row in enumerate(it):
            row = list(row)
            if i == 0:
                cols = [str(c).strip() if c else f'Column_{j}' for j, c in enumerate(row)]
            else:
                if row and any(v is not None and str(v).strip() != '' for v in row):
                    rows.append(dict(zip(cols, row)))

        return rows, cols, sheet_names

    # ── 字段匹配检查 ─────────────────────────────────────
    def _check_column_match(self):
        """检查文件列与表字段的匹配情况"""
        if not self.file_columns or not self.table_columns:
            return

        file_set = set(self.file_columns)
        table_set = set(self.table_columns)
        matched = file_set & table_set
        missing = table_set - file_set
        extra = file_set - table_set

        text = f'匹配: {len(matched)}/{len(table_set)} 个字段'
        if missing:
            text += f'  |  表中缺少: {", ".join(sorted(missing)[:8])}'
        if extra:
            text += f'  |  文件中多余: {", ".join(sorted(extra)[:8])}'

        if missing:
            self.lbl_match_status.configure(text=text, foreground='orange')
        else:
            self.lbl_match_status.configure(text=text + ' ✓ 字段完全匹配', foreground='green')

    # ── 导入逻辑 ──────────────────────────────────────────
    def _do_import(self):
        if not self.file_data:
            messagebox.showwarning('提示', '请先选择文件')
            return
        if not self.var_table.get().strip():
            messagebox.showwarning('提示', '请先选择目标表')
            return

        # 检查字段匹配
        file_set = set(self.file_columns)
        table_set = set(self.table_columns)
        missing = table_set - file_set
        if missing:
            ok = messagebox.askokcancel(
                '字段不匹配',
                f'文件中缺少以下数据库字段:\n{", ".join(sorted(missing))}\n\n'
                f'缺少的字段将填入 NULL 值，是否继续？')
            if not ok:
                return

        # 确认导入
        dedup_enabled = self.var_enable_dedup.get()
        dedup_indices = self.lb_dedup.curselection()
        dedup_cols = [self.table_columns[i] for i in dedup_indices] if dedup_indices else []

        msg = f'即将导入 {len(self.file_data)} 行数据到 "{self.var_table.get()}" 表'
        if dedup_enabled and dedup_cols:
            msg += f'\n导入后将基于 [{", ".join(dedup_cols)}] 去重'

        if not messagebox.askokcancel('确认导入', msg):
            return

        self._cancel_flag.clear()
        self.btn_import.configure(state=tk.DISABLED)
        self.btn_cancel.configure(state=tk.NORMAL)
        self.progress['maximum'] = len(self.file_data)
        self.progress['value'] = 0

        t = threading.Thread(target=self._run_import, args=(dedup_enabled, dedup_cols), daemon=True)
        t.start()

    def _do_cancel(self):
        self._cancel_flag.set()
        self._log('用户取消操作', 'WARN')

    def _run_import(self, dedup_enabled, dedup_cols):
        conn = None
        success = 0
        failed = 0

        try:
            conn = self._get_conn()
            table = self.var_table.get().strip()

            # 确定要写入的列（文件列 ∩ 表列）
            insert_cols = [c for c in self.file_columns if c in self.table_columns]
            if not insert_cols:
                self._log('错误: 文件列与表字段无任何交集', 'ERROR')
                return

            self._log(f'开始导入: {len(self.file_data)} 行 -> {table}')
            self._log(f'写入字段: {", ".join(insert_cols)}')

            # 构建 INSERT 语句
            placeholders = ', '.join(['%s'] * len(insert_cols))
            cols_quoted = ', '.join([f'`{c}`' for c in insert_cols])
            sql = f'INSERT INTO `{table}` ({cols_quoted}) VALUES ({placeholders})'

            batch_size = 500
            batch = []

            for i, row in enumerate(self.file_data):
                if self._cancel_flag.is_set():
                    self._log(f'导入已取消 (已导入 {success} 行)', 'WARN')
                    if batch:
                        conn.rollback()
                    return

                values = []
                for c in insert_cols:
                    raw = row.get(c, None)
                    values.append(self._convert_value(raw, self.table_column_types.get(c, '')))

                batch.append(values)

                if len(batch) >= batch_size:
                    s = self._batch_insert(conn, sql, batch)
                    success += s
                    failed += len(batch) - s
                    batch = []
                    self._update_progress(i + 1, f'{i + 1}/{len(self.file_data)}')

            # 插入剩余
            if batch and not self._cancel_flag.is_set():
                s = self._batch_insert(conn, sql, batch)
                success += s
                failed += len(batch) - s

            self._update_progress(len(self.file_data), f'导入: {success} 成功, {failed} 失败')

            # 去重
            if dedup_enabled and dedup_cols and success > 0 and not self._cancel_flag.is_set():
                self._log('正在执行去重...')
                self._update_progress_text('去重中...')
                deleted = self._dedup(conn, table, dedup_cols)
                self._log(f'去重完成: 删除 {deleted} 条重复记录')

            self._log(f'=== 导入完成: 成功 {success} 行, 失败 {failed} 行 ===')

        except Exception as e:
            self._log(f'导入过程发生错误: {e}', 'ERROR')
            traceback.print_exc()
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            self.root.after(0, lambda: self.btn_import.configure(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_cancel.configure(state=tk.DISABLED))
            self.root.after(0, lambda: self.lbl_progress.configure(text='就绪', foreground='gray'))

    def _batch_insert(self, conn, sql, batch):
        """批量插入，返回成功行数"""
        try:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.executemany(sql, batch)
                conn.commit()
            return len(batch)
        except Exception as e:
            conn.rollback()
            self._log(f'批量插入失败: {e}', 'ERROR')
            # 降级为逐行插入
            ok = 0
            for vals in batch:
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, vals)
                        conn.commit()
                    ok += 1
                except Exception as row_err:
                    self._log(f'  行插入失败: {row_err} | 数据: {str(vals)[:100]}', 'WARN')
            return ok

    def _convert_value(self, value, col_type):
        """数据类型转换"""
        if value is None:
            return None

        s = str(value).strip()
        if s in ('', 'NULL', 'N/A', '-', 'null', 'None', 'nan', 'NaN'):
            return None

        tl = col_type.lower()

        # 数值类型
        if any(t in tl for t in ('int', 'decimal', 'float', 'double', 'numeric')):
            s_clean = s.replace(',', '').replace('￥', '').replace('¥', '').replace('元', '').replace('%', '').replace(' ', '')
            try:
                if '.' in s_clean:
                    return float(s_clean)
                return int(s_clean)
            except ValueError:
                self._log(f'  数值转换失败: "{s}" -> 将设为 NULL', 'WARN')
                return None

        # 日期类型
        if 'date' in tl or 'time' in tl:
            for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S',
                         '%d/%m/%Y', '%m/%d/%Y', '%Y年%m月%d日']:
                try:
                    dt = datetime.strptime(s, fmt)
                    if 'time' in tl and 'date' not in tl:
                        return dt.strftime('%H:%M:%S')
                    return dt.strftime('%Y-%m-%d')
                except ValueError:
                    continue
            # 尝试解析 Excel 日期序列号
            try:
                n = float(s)
                if 10000 < n < 100000:
                    base = datetime(1899, 12, 30)
                    dt = base + timedelta(days=int(n))
                    return dt.strftime('%Y-%m-%d')
            except ValueError:
                pass
            return s

        return s

    def _dedup(self, conn, table, dedup_cols):
        """执行去重：按 dedup_cols 分组，保留 order_col 值最大的行"""
        order_col = self.var_order_col.get().strip()
        order_dir = self.var_order_dir.get()

        if not order_col or order_col not in self.table_columns:
            self._log('排序字段无效，跳过去重', 'WARN')
            return 0

        # 获取用于标识行的列（有主键用主键，否则用全部列）
        identifier_cols = self.pk_columns if self.pk_columns else self.table_columns
        if not identifier_cols:
            return 0

        pk_list = ', '.join([f'`{c}`' for c in identifier_cols])
        dedup_partition = ', '.join([f'`{c}`' for c in dedup_cols])

        # MySQL 8.0 嵌套子查询去重法
        sql = f"""
            DELETE FROM `{table}`
            WHERE ({pk_list}) IN (
                SELECT * FROM (
                    SELECT {pk_list} FROM (
                        SELECT {pk_list},
                            ROW_NUMBER() OVER (
                                PARTITION BY {dedup_partition}
                                ORDER BY `{order_col}` {order_dir}
                            ) AS _rn
                        FROM `{table}`
                    ) AS _t1 WHERE _rn > 1
                ) AS _t2
            )
        """

        try:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.execute(sql)
                deleted = cur.rowcount
                conn.commit()
            return deleted
        except Exception as e:
            self._log(f'去重失败: {e}', 'ERROR')
            self._log(f'SQL: {sql}', 'ERROR')
            return 0

    def _update_progress(self, val, text):
        def _u():
            self.progress['value'] = val
            self.lbl_progress.configure(text=text)
        self.root.after(0, _u)

    def _update_progress_text(self, text):
        self.root.after(0, lambda: self.lbl_progress.configure(text=text))


# ═══════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════
def main():
    # 检查依赖
    missing = []
    if not HAS_PYMYSQL:
        missing.append('pymysql')
    if not HAS_OPENPYXL:
        missing.append('openpyxl')
    if missing:
        msg = '以下依赖未安装:\n' + '\n'.join(f'  pip install {m}' for m in missing)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning('缺少依赖', msg)
            root.destroy()
        except Exception:
            print(msg)
        # 仍然启动，让用户能在界面看到错误

    root = tk.Tk()
    app = ImportToolApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
