# 知识包格式说明

这里定义知识包的目录结构和打包方式，方便在不同设备之间迁移，或者以后从 GitHub 下载示例包。

## 目录结构

```text
pack/
└─ example/                 # 一个知识包 = 一个目录
   ├─ manifest.json         # 可选：名称、版本、默认主题
   ├─ study/                # 每个子目录名 = 一个主题（topic）
   │  └─ sample_math.md
   └─ life/
      └─ sample_fitness.md
```

规则：

- 支持 `.md`、`.markdown`、`.txt`；
- 子目录名会作为 `topic` 写入 metadata，方便以后按主题过滤；
- `manifest.json` 可选，字段 `topic` 会作为整包的默认主题；
- 同一文件重复导入会覆盖旧片段（稳定 ID），不会重复堆积。

## 打包成 zip

在项目根目录执行：

```powershell
# Windows 自带压缩，生成 pack/example.zip
Compress-Archive -Path pack/example/* -DestinationPath pack/example.zip -Force
```

或者用仓库自带脚本（同时会校验包内容）：

```powershell
.\.venv\Scripts\python.exe scripts\pack_kb.py pack --src pack/example --out pack/example.zip
```

## 导入 zip

```powershell
.\.venv\Scripts\python.exe scripts\ingest.py pack --path pack/example.zip
```

## 导入目录

```powershell
# 导入整个示例包
.\.venv\Scripts\python.exe scripts\ingest.py ingest --path pack/example

# 导入自己的文档目录
.\.venv\Scripts\python.exe scripts\ingest.py ingest --path data/docs
```

## 公开知识与个人资料的边界

- `pack/example/`：**只放示例/通用知识**，可以提交到 GitHub；
- `data/docs/`：放**个人档案**，默认被 `.gitignore` 忽略，不会上传；
- `data/runtime/chroma/`：运行时向量库，同样被忽略，可随时从文档重建。
