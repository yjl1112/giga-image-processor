# giga-image-processor

GIGA 素材图片批量处理服务：上传产品素材 ZIP → 解压校验 → 统一重制（1600×1600 / RGB / JPEG 质量可调）→ 输出规范目录 + CSV 处理报告 + 打包 ZIP 下载。单进程 Flask 服务，自带 Web 操作界面，可独立部署。

## 功能

- **批量处理**：一次上传多个产品素材包（`simple` 模式，文件名即 item code），或上传 Excel 模板按表匹配（`template` 模式）。
- **断点续传上传**：分片上传（16MB/片），支持大文件与断点恢复。
- **GIGA 素材拉取**：对接 GIGA 站点素材下载（依赖本地 webBridge 通道）。
- **图片局部编辑（可选）**：AGNES API 的蒙版局部编辑 + 提示词优化。
- **公共图片外链**：处理结果可生成对外可访问的图片链接。
- **自动清理**：`retention` 定时任务按天清理历史批次（默认保留 1 天）。
- **访问门禁**：可选令牌校验（`KROWORK_ACCESS_TOKEN`），防止公网裸奔。

## 目录结构

```
giga-image-processor/
├── apps/
│   ├── material/                 # Flask 主服务
│   │   ├── main.py               # 全部路由与任务调度
│   │   ├── giga_material_download.py  # GIGA 素材下载器
│   │   ├── image_edit_service.py # AGNES 局部编辑 / 提示词优化
│   │   ├── templates/index.html  # 操作界面
│   │   └── static/stitch.css
│   └── shared/                   # 公共模块（相对导入，随仓库分发）
│       ├── auth.py               # 令牌门禁 + 登录页
│       └── path_safety.py        # 路径安全校验
├── scripts/                      # 由主服务以子进程调用
│   ├── process_materials_json.py # 处理入口（mode=simple|template）
│   └── process_giga_materials.py # 解压 / 重制 / 报告核心逻辑
├── requirements.txt
├── .env.example
└── README.md
```

> 运行期会在 `apps/material/data/` 下生成 `uploads/ outputs/ reports/ image_edits/` 等目录，已在 `.gitignore` 中排除。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # 按需填写
python apps/material/main.py
```

启动后访问 `http://127.0.0.1:5003/`。

生产环境建议用 waitress（已列入依赖，自动启用，8 工作线程）；未安装时回退 Flask 开发服务器。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `KROWORK_PORT` | `5000` | 监听端口（部署时建议 `5003`） |
| `KROWORK_HOST` | `127.0.0.1` | 监听地址 |
| `KROWORK_ACCESS_TOKEN` | 空 | 设置后所有请求需携带令牌 / 登录 |
| `KROWORK_SESSION_SECRET` | 空 | 会话签名密钥 |
| `MATERIAL_RETENTION_DAYS` | `1` | 历史批次保留天数，`0` 关闭自动清理 |
| `MATERIAL_RETENTION_INTERVAL_SECONDS` | `1800` | 清理周期 |
| `GIGA_WEB_BASE_URL` | `https://www.gigab2b.com` | GIGA 站点地址 |
| `KIMI_WEBBRIDGE_URL` | `http://127.0.0.1:10086/command` | 本地下载通道（GIGA 拉取用） |
| `AGNES_API_KEY` | 空 | 不配置则局部编辑 / 提示词优化不可用 |
| `AGNES_IMAGE_API_URL` | `https://apihub.agnes-ai.com/v1` | AGNES 图像 API |
| `MATERIAL_PUBLIC_IMAGE_BASE_URL` | 空 | 公共图片外链前缀 |
| `BUNDLE_ROOT` | 仓库根 | 子进程工作目录 |
| `BUNDLE_PYTHON` | 当前解释器 | 执行 `scripts/` 的 Python |

密钥也可写在仓库根 `config/.env`（`KROWORK_ACCESS_TOKEN=` 单行），该文件已被忽略。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload/start` `/upload/file` `/upload/chunk` `/upload/complete` | 分片上传流程 |
| POST | `/api/process/start` | 启动处理（后台线程，返回 202） |
| GET | `/api/process/status/<run_id>` | 查询处理状态 |
| GET | `/api/runs` `/api/runs/<run_id>` | 历史批次 |
| GET | `/api/download/<run_id>` | 下载处理结果 ZIP |
| POST | `/api/giga-material/start` | GIGA 素材拉取 |
| POST | `/api/image-edit/start` | 局部编辑（需 `AGNES_API_KEY`） |
| GET | `/api/stats` `/api/trends` | 统计 |
| GET | `/api/retention` / POST `/api/retention/sweep` | 清理状态 / 立即清理 |

处理参数：`{"run_id": "...", "size": 1600, "quality": 85, "workers": 4}`。

## Windows 注意事项

若在 WorkBuddy / CodeBuddy 类 Agent 沙箱环境中运行，其 `sitecustomize` 会改写 `os.unlink` 等删除原语，并在单轮删除 ≥50 个文件时直接 `SystemExit`，**会杀掉正在处理请求的工作线程**（表现为上传完成后界面永远"处理中"）。本服务已在启动时自动还原删除原语（见 `apps/material/main.py` 的 `_restore_raw_deleters()`）；也可直接以 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 启动，跳过装载该 shim。

## License

Internal use.
