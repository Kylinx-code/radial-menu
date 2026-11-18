# 打包说明

需要 Windows + Python 3.9+：

```bash
pip install -r ../requirements.txt
pip install pyinstaller
```

以下命令都在 `packaging/` 目录内执行。

## 1. 免安装版

```bash
pyinstaller --noconfirm --distpath ../dist --workpath ../build_work RadialMenu.spec
```

产物：`../dist/RadialMenu.exe`。改名为 `免安装版-右键轮盘菜单.exe` 即可分发。

## 2. 安装包

安装包会把上一步的 exe 内嵌进去，**必须先完成第 1 步**：

```bash
pyinstaller --noconfirm --distpath ../dist_setup --workpath ../build_work_setup Setup.spec
```

产物：`../dist_setup/安装右键轮盘菜单.exe`。

安装器把 `RadialMenu.exe` 装到 `%LOCALAPPDATA%\RadialMenu`，创建桌面与开始菜单快捷方式，询问是否开机自启；再次运行加 `--uninstall` 参数可卸载。

## 说明

- 两个 spec 都用 PyInstaller 内置的 `SPECPATH` 推导仓库根目录，不含绝对路径。
- `RadialMenu.spec` 只把 `app.ico` 打进 exe，不打包 `folders.json`：配置由程序首次运行时按 `DEFAULT_CONFIG` 自动生成，避免把个人路径带进分发包。
- `Setup.spec` 从 `../dist/RadialMenu.exe` 读取待内嵌的程序，路径不对会打包失败。
- `dist/`、`build_work/`、`*.exe` 已在 `.gitignore` 中排除，二进制产物统一走 GitHub Releases。
