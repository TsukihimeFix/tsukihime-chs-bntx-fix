# 月姬汉化补丁 BNTX/BC7 修复

用于修复《月姬 -A piece of blue glass moon-》汉化补丁 v3.0 在新版 Ryujinx/Yuzu 系模拟器中出现的标题、菜单和 UI 贴图方块、条带、噪点及错位问题。

本仓库只提供修复脚本，不包含游戏本体、汉化资源、固件或密钥。请自行准备合法取得的游戏与汉化补丁 v3.0。

## 问题

汉化补丁 v3.0 使用的旧版 BNTX 工具，在部分修改过的纹理包中留下了两套头部语义：

- 汉化纹理：`flags=0x07`，`layout=0x30-0x34`
- 同包内未替换纹理：`flags=0x09`，`layout=0x40-0x44`

新版模拟器会按这些高位解释纹理布局，导致 BC7 图片显示异常。图片数据本身没有损坏。

## 解决方法

脚本从经过 SHA-256 校验的汉化补丁 v3.0 原件重新构建，只修正受影响 BNTX 中的 `flags` 和 `layout`：

- `0x07 + 0x30-0x34` → `0x01 + 0x00-0x04`
- 同包内的 `0x09 + 0x40-0x44` → `0x01 + 0x00-0x04`

BC7 payload、宽高、格式、mip、swizzle 和图片数据均不解码、不重绘、不重新压缩。92 个不受影响的 BNTX 包保持压缩字节完全不变。

## 使用方法

要求 Python 3.10 或更高版本，无第三方 Python 依赖。运行前先关闭 Ryujinx。

汉化模组目录应包含：

```text
01001DC01486A000/
└─ romfs/
   ├─ CHS.hed
   ├─ CHS.mrg
   └─ CHS.nam
```

仅生成修复后的模组：

```powershell
python .\Repair-TsukihimePatchBntx.py `
  --source "C:\Path\Official_v3.0\01001DC01486A000" `
  --output "C:\Path\BNTX_Fixed\01001DC01486A000"
```

生成后同时安装到 Ryujinx：

```powershell
python .\Repair-TsukihimePatchBntx.py `
  --source "C:\Path\Official_v3.0\01001DC01486A000" `
  --output "C:\Path\BNTX_Fixed\01001DC01486A000" `
  --install-to "C:\Path\Ryujinx\portable\mods\contents\01001dc01486a000" `
  --backup-root "C:\Path\Patch_Backups"
```

示例中的盘符和目录只需替换成自己的实际路径，脚本没有写死 C 盘或 D 盘。

注意：

- `--output` 指向的目录必须尚不存在。
- 脚本只接受已验证的汉化补丁 v3.0 三件套，哈希不符会停止。
- 安装前会备份当前启用的 `CHS.mrg/CHS.hed/CHS.nam`。
- 安装或校验失败时会自动恢复备份。
- 不需要清理 shader cache。

## 验证结果

| 检查项 | 结果 |
|---|---:|
| MRG 条目 | 225 |
| BNTX 包 | 216 |
| 纹理记录 | 569 |
| 受影响 BNTX 包 | 124 |
| 修正纹理记录 | 436 |
| 图片 payload 哈希一致 | 569/569 |
| 输出 `flags=0x07` | 0 |

已验证环境为 Ryujinx `1.3.3`（产品版本 `1.3.3+e2143d4`，Windows 文件版本 `1.3.3.0`）和 Switch 固件 `21.2.0`。这只是实际验证环境，不代表其他版本一定兼容或不兼容。

详细校验数据见 [docs/VERIFICATION.md](docs/VERIFICATION.md) 和 [verification.json](verification.json)。

## 说明

提交 Issue 时请提供模拟器版本、图形后端、GPU 型号、出错场景和截图，请勿上传游戏文件、XCI、固件、密钥、存档或原汉化资源。

修复依据：[Tsukihimates 发布说明](https://tsukihimates.com/patch/)与[上游完整 flags 修复](https://github.com/Tsukihimates/Tsukihime-Translation/pull/1326)。汉化补丁随附使用教程署名为 `letdo1945 / ruje0504`。

本项目与 TYPE-MOON、Aniplex、Nintendo、Ryujinx/Ryubing 及原汉化组没有隶属或授权关系。代码与文档采用 [MIT License](LICENSE)，第三方内容说明见 [THIRD_PARTY_NOTICE.md](THIRD_PARTY_NOTICE.md)。
