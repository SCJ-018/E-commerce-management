#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
构建数据导入工具 EXE
运行: python build_exe.py
"""
import os
import sys
import subprocess


def main():
    tool_dir = os.path.dirname(os.path.abspath(__file__))
    main_script = os.path.join(tool_dir, 'import_tool.py')
    dist_dir = os.path.join(tool_dir, 'dist')

    # 确保依赖已安装
    print('Checking dependencies...')
    deps = ['pymysql', 'openpyxl', 'xlrd']
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            print(f'  Installing {dep}...')
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', dep])

    # 检查 PyInstaller
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('  Installing pyinstaller...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyinstaller'])

    print('Building EXE...')
    print(f'  Source: {main_script}')
    print(f'  Output: {dist_dir}')

    args = [
        sys.executable, '-m', 'PyInstaller',
        '--onefile',
        '--windowed',
        '--name', '数据导入工具',
        '--distpath', dist_dir,
        '--workpath', os.path.join(tool_dir, 'build'),
        '--specpath', tool_dir,
        '--hidden-import', 'pymysql',
        '--hidden-import', 'openpyxl',
        '--hidden-import', 'openpyxl.cell',
        '--hidden-import', 'openpyxl.cell.cell',
        '--hidden-import', 'openpyxl.styles',
        '--hidden-import', 'openpyxl.utils',
        '--hidden-import', 'openpyxl.worksheet',
        '--hidden-import', 'openpyxl.reader',
        '--hidden-import', 'openpyxl.workbook',
        '--hidden-import', 'openpyxl.descriptors',
        '--hidden-import', 'openpyxl.xml',
        '--hidden-import', 'openpyxl.packaging',
        '--hidden-import', 'xlrd',
        '--clean',
        '--noconfirm',
        main_script,
    ]

    subprocess.run(args, check=True)

    exe_path = os.path.join(dist_dir, '数据导入工具.exe')
    print(f'\n========================================')
    print(f'  Build successful!')
    print(f'  EXE: {exe_path}')
    print(f'========================================')

    return exe_path


if __name__ == '__main__':
    main()
