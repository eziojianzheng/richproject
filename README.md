# 淘股吧数据服务

## 项目概述

本项目是一个综合性的股票数据服务平台，提供：
1. **图片下载**：从淘股吧自动下载"湖南人涨停复盘"系列文章图片
2. **OCR识别**：识别图片内容，提取股票市场数据
3. **热门股追踪**：统计板块热度，追踪个股表现

## 快速启动

```bash
# 双击运行
start_server.bat

# 或命令行运行
python api_server.py
```

服务启动后访问：
- **首页**：http://127.0.0.1:5000
- **热门股追踪**：http://127.0.0.1:5000/hot

## 核心功能

### 1. 图片下载服务

| 接口 | 方法 | 功能描述 |
|------|------|----------|
| `/` | GET | API首页，显示所有可用接口 |
| `/hot` | GET | 热门股追踪页面 |
| `/api/articles` | GET | 获取博客文章列表，支持按日期过滤 |
| `/api/download` | POST | 异步下载图片（按日期范围） |
| `/api/download/sync` | POST | 同步下载图片（阻塞式） |
| `/api/status/<task_id>` | GET | 查询下载任务状态 |
| `/api/files` | GET | 查看已下载的所有文件 |
| `/api/files/<date>` | GET | 查看指定日期的文件 |
| `/api/ocr/title` | GET | OCR查找标题图片 |
| `/api/ocr/recognize` | POST | OCR识别指定图片文字 |

### 2. 热门股追踪

| 接口 | 方法 | 功能描述 |
|------|------|----------|
| `/hot` | GET | 热门股追踪页面（可视化界面） |
| `/api/hot/track` | GET | 获取追踪数据（参数: start, end, sort, price） |
| `/api/hot/dates` | GET | 获取已有数据的日期列表 |

**功能特点**：
- 按板块统计热门股出现次数
- 显示个股连板状态和涨跌幅
- 支持区间查询和排序
- 可视化图表展示

### 3. OCR识别模块 (`utils.py`)

基于RapidOCR的文字识别功能：

- **标题图片识别**：查找包含"湖南人涨停复盘"关键字的图片
- **OCR缓存机制**：识别结果自动缓存到 `.ocr.json` 文件，避免重复识别
- **智能查找策略**：优先检查第04张图片，未找到则遍历所有图片

### 4. 依赖管理 (`bootstrap.py`)

启动时自动检测并安装缺失的Python依赖包。

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Flask API Server                        │
│                      (api_server.py)                         │
├─────────────────────────────────────────────────────────────┤
│  下载模块          │  OCR模块              │  任务管理        │
│  - 网络请求重试     │  - RapidOCR识别       │  - 异步任务      │
│  - Cookie认证      │  - 缓存机制           │  - 进度追踪      │
│  - 指数退避策略     │  - 关键字匹配         │  - 状态查询      │
├─────────────────────────────────────────────────────────────┤
│                    数据存储层                                │
│  - dataresource/YYYYMMDD/  图片存储目录                     │
│  - .ocr.json               OCR识别缓存                      │
│  - _order.txt              图片顺序文件                      │
└─────────────────────────────────────────────────────────────┘
```

## 目录结构

```
project/
├── api_server.py          # Flask API主服务
├── bootstrap.py           # 依赖自动安装模块
├── utils.py               # OCR工具函数
├── config.yml             # 配置文件（AI模型、OCR参数）
├── config.yml.example     # 配置文件示例
├── dataresource/          # 图片存储目录
│   ├── 20260514/          # 按日期分组
│   │   ├── xxx.png        # 下载的图片
│   │   ├── xxx.png.ocr.json  # OCR缓存
│   │   └── _order.txt     # 图片顺序
│   ├── 20260515/
│   └── ...
└── .price_cache.json      # 价格缓存
```

## 配置说明 (`config.yml`)

### AI模型配置

```yaml
ai:
  zhipu:
    api_key: "YOUR_API_KEY"
    text_model: "GLM-4-Flash-250414"    # 文本模型
    vision_model: "GLM-4.5V"             # 视觉模型（OCR效果更好）
    base_url: "https://open.bigmodel.cn/api/paas/v4/"
  params:
    temperature: 0.1
    top_p: 0.1
    max_tokens: 4096
```

### OCR提取配置

```yaml
ocr:
  extract:
    image_03:  # 涨跌家数
      - "上涨家数"
      - "下跌家数"
      - "总成交额"
    image_04:  # 涨停数据
      - "涨停"
      - "炸板"
      - "跌停"
```

## API使用示例

### 1. 启动服务

```bash
python api_server.py
# 服务运行在 http://127.0.0.1:5000
```

### 2. 下载图片

```bash
# 异步下载（推荐）
curl -X POST http://127.0.0.1:5000/api/download \
  -H "Content-Type: application/json" \
  -d '{"start_date":"2026-05-14","end_date":"2026-05-20"}'

# 返回: {"task_id": "abc123", "status_url": "/api/status/abc123"}

# 查询进度
curl http://127.0.0.1:5000/api/status/abc123
```

### 3. OCR识别

```bash
# 查找标题图片
curl "http://127.0.0.1:5000/api/ocr/title?date=20260514"

# 识别指定图片
curl -X POST http://127.0.0.1:5000/api/ocr/recognize \
  -H "Content-Type: application/json" \
  -d '{"image_path":"dataresource/20260514/xxx.png"}'
```

## 核心算法

### 网络请求重试机制

```python
# 指数退避重试：1s -> 2s -> 4s
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0

# 仅对网络异常重试，HTTP状态码错误不重试
_RETRYABLE_EXC = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)
```

### OCR缓存策略

```
图片文件: xxx.png
缓存文件: xxx.png.ocr.json

缓存命中条件：
1. 缓存文件存在
2. 缓存修改时间 >= 图片修改时间
```

### 标题图片查找策略

```
1. 优先检查第04张图片（经验规律）
2. 未找到则遍历所有图片
3. 使用OCR识别文字内容
4. 匹配关键字"湖南人涨停复盘"
```

## 数据流程

```
1. 用户请求下载
   ↓
2. 获取博客文章列表 (get_article_list)
   ↓
3. 按日期过滤文章
   ↓
4. 解析文章获取图片URL (get_article_images)
   ↓
5. 下载图片到本地 (download_image)
   ↓
6. 创建异步任务追踪进度
   ↓
7. 支持OCR识别提取文字
```

## 依赖列表

| 包名 | 版本 | 用途 |
|------|------|------|
| flask | >=3.0.0 | Web框架 |
| requests | >=2.31.0 | HTTP请求 |
| beautifulsoup4 | >=4.12.0 | HTML解析 |
| PyYAML | >=6.0 | 配置文件解析 |
| openpyxl | >=3.1.0 | Excel操作 |
| rapidocr_onnxruntime | >=1.3.0 | OCR识别 |

## 关键代码说明

### Cookie认证

系统使用硬编码的Cookie进行淘股吧网站认证，位于 `api_server.py` 中的 `COOKIES` 字典。Cookie包含：
- `tgbuser`: 用户ID
- `tgbpwd`: 加密密码
- `JSESSIONID`: 会话ID

### 图片URL解析

```python
# 支持多种图片URL格式
img_url = img.get('data-original') or img.get('src2') or img.get('src')

# URL补全
if img_url.startswith('//'):
    img_url = 'https:' + img_url
elif img_url.startswith('/'):
    img_url = 'https://www.tgb.cn' + img_url
```

### 跳过已下载检测

```python
def check_folder_has_images(folder_path):
    """检查文件夹是否已有图片，避免重复下载"""
    for f in os.listdir(folder_path):
        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            return True
    return False
```

## 注意事项

1. **Cookie有效期**：硬编码的Cookie可能会过期，需要定期更新
2. **网络请求频率**：下载间隔0.2秒，避免触发反爬机制
3. **OCR缓存**：删除图片时记得同时删除对应的 `.ocr.json` 缓存
4. **并发限制**：异步下载使用单线程，避免并发过高被封禁

## 扩展开发指南

### 添加新的API接口

在 `api_server.py` 中添加新的路由：

```python
@app.route('/api/new_endpoint', methods=['GET'])
def new_function():
    # 实现逻辑
    return jsonify({'success': True})
```

### 添加新的OCR识别功能

在 `utils.py` 中添加新的识别函数：

```python
def extract_specific_data(image_path, keywords):
    """提取特定数据"""
    texts = ocr_image(image_path)
    # 解析逻辑
    return result
```

### 配置新的AI模型

修改 `config.yml` 添加新的模型配置：

```yaml
ai:
  new_provider:
    api_key: "YOUR_API_KEY"
    model: "model-name"
    base_url: "https://api.example.com/"
```

---

**版本**: 1.1.0  
**最后更新**: 2026年7月
