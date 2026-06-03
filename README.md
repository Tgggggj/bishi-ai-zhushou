# Practice Desktop Assistant Local

本目录是本地版程序。

## 特点

- 不接入公网同步服务器。
- 邮箱注册、登录和同步数据只保存在本机 `sync_store.json`。
- 保留本机桌面端、网页版和移动端复盘页。
- 点击窗口关闭按钮会隐藏到托盘，托盘菜单可退出程序。

## 运行

直接打开：

```text
PracticeDesktopAssistant_Local.exe
```

本机网页：

```text
http://127.0.0.1:8786/
```

移动端复盘页：

```text
http://127.0.0.1:8786/mobile
```

## 构建

```powershell
.\build_desktop_exe.ps1
```

产物：

```text
dist\PracticeDesktopAssistant_Local.exe
```

## 数据文件

以下文件为运行时生成，不提交 Git：

- `desktop_settings.json`
- `latest_result.json`
- `sync_store.json`
