# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\computer\\Desktop\\AI\\opencoder-autocoder-split\\gemini_coder_web\\default_selectors.json', 'gemini_coder_web')]
binaries = []
hiddenimports = ['gemini_coder', 'gemini_coder.ui', 'gemini_coder.ui.app', 'gemini_coder.ui.theme', 'gemini_coder.config', 'gemini_coder.task_manager', 'gemini_coder.expander', 'gemini_coder.history', 'gemini_coder.platform_utils', 'gemini_coder_web', 'gemini_coder_web.ui', 'gemini_coder_web.ui.app_web', 'gemini_coder_web.ai_profiles', 'gemini_coder_web.auto_save', 'gemini_coder_web.broadcast', 'gemini_coder_web.browser_actions', 'gemini_coder_web.browser_client', 'gemini_coder_web.cdp_client', 'gemini_coder_web.session_manager', 'gemini_coder_web.universal_client', 'gemini_coder_web.window_manager', 'customtkinter', 'pyautogui', 'pyperclip', 'pystray', 'PIL', 'psutil', 'websocket']
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['C:\\Users\\computer\\Desktop\\AI\\opencoder-autocoder-split\\_autocoder_entry.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Autocoder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Autocoder',
)
