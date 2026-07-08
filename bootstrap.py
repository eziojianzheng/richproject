#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
依赖引导模块
在服务启动时自动检测并安装缺失的依赖
"""

import importlib
import subprocess
import sys
import os

# 包的导入名 -> pip 安装名 的映射
# (有些包导入名和安装名不一致)
REQUIRED_PACKAGES = {
    'flask': 'flask>=3.0.0',
    'flask_socketio': 'flask-socketio>=5.3.0',
    'requests': 'requests>=2.31.0',
    'bs4': 'beautifulsoup4>=4.12.0',
    'yaml': 'PyYAML>=6.0',
    'openpyxl': 'openpyxl>=3.1.0',
    'rapidocr_onnxruntime': 'rapidocr_onnxruntime>=1.3.0',
    'mootdx': 'mootdx>=0.11.0',
}


def _is_installed(import_name):
    """检查某个包是否已安装"""
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        return False


def ensure_dependencies(packages=None, quiet=False):
    """
    检测并自动安装缺失的依赖

    参数:
        packages: 需要的包字典 {导入名: pip安装名}，默认使用 REQUIRED_PACKAGES
        quiet: 是否静默安装

    返回:
        (success: bool, missing_installed: list) 安装是否成功，以及安装了哪些包
    """
    if packages is None:
        packages = REQUIRED_PACKAGES

    missing = {imp: pip_name for imp, pip_name in packages.items()
               if not _is_installed(imp)}

    if not missing:
        if not quiet:
            print("[bootstrap] 所有依赖已就绪")
        return True, []

    print("[bootstrap] 检测到缺失依赖: " + ", ".join(missing.keys()))
    print("[bootstrap] 开始自动安装...")

    installed = []
    for imp_name, pip_name in missing.items():
        print(f"[bootstrap] 安装 {pip_name} ...")
        cmd = [sys.executable, '-m', 'pip', 'install', pip_name]
        if quiet:
            cmd.append('-q')
        try:
            subprocess.check_call(cmd)
            installed.append(pip_name)
        except subprocess.CalledProcessError as e:
            print(f"[bootstrap] 安装失败: {pip_name} ({e})")
            print(f"[bootstrap] 请手动执行: {sys.executable} -m pip install -r requirements.txt")
            return False, installed

    # 安装后清理 import 缓存，确保可立即导入
    importlib.invalidate_caches()
    print(f"[bootstrap] 依赖安装完成，共安装 {len(installed)} 个包")
    return True, installed


def install_from_requirements(req_file='requirements.txt'):
    """从 requirements.txt 安装全部依赖"""
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), req_file)
    if not os.path.exists(req_path):
        print(f"[bootstrap] 未找到 {req_file}")
        return False
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', req_path])
        importlib.invalidate_caches()
        return True
    except subprocess.CalledProcessError as e:
        print(f"[bootstrap] 安装失败: {e}")
        return False


if __name__ == '__main__':
    ensure_dependencies()
