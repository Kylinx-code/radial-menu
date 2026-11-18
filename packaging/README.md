# 打包说明

需要 Windows + Python 3.9+：

```bash
pip install -r ../requirements.txt
pip install pyinstaller==6.22.3
```

以下命令都在 `packaging/` 目录内执行。仓库已配置 [CI](../.github/workflows/build.yml) 自动完成同样两步，本地打包主要用于调试。

## 1. 免安装版

```bash
pyinstaller --noconfirm --distpath ../dist --workpath ../build_work RadialMenu.spec
```

产物：`../dist/RadialMenu.exe`。分发时改名为 `RadialMenu-portable.exe`（或任意中文名）。

## 2. 安装包

安装包会把上一步的 exe 内嵌进去，**必须先完成第 1 步**：

```bash
pyinstaller --noconfirm --distpath ../dist_setup --workpath ../build_work_setup Setup.spec
```

产物：`../dist_setup/RadialMenuSetup.exe`。分发时改名为 `RadialMenu-setup.exe`。

安装器把 `RadialMenu.exe` 装到 `%LOCALAPPDATA%\RadialMenu`，创建桌面与开始菜单快捷方式，询问是否开机自启；再次运行加 `--uninstall` 参数可卸载。

## 说明

- 两个 spec 用 PyInstaller 注入的 `SPECPATH` 推导仓库根目录（未注入时回退到当前工作目录），不含绝对路径。
- `RadialMenu.spec` 只把 `app.ico` 打进 exe，不打包 `folders.json`：配置由程序首次运行时按 `DEFAULT_CONFIG` 自动生成，避免把个人路径带进分发包。
- `Setup.spec` 从 `../dist/RadialMenu.exe` 读取待内嵌的程序，路径不对会打包失败。为规避 CI 上的中文路径编码问题，spec 的产物名用 ASCII `RadialMenuSetup`。
- GitHub Release 的资产名只能用 ASCII：上传接口会剥掉 `name` 参数里的非 ASCII 字符。
- `dist*/`、`build_work*/`、`dist_release/`、`*.exe` 已在 `.gitignore` 中排除，二进制产物统一走 GitHub Releases。
