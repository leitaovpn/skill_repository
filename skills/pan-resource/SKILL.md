---
name: pan-resource
description: 搜索网盘分享链接，预览资源详情并匹配豆瓣/IMDb 元数据，支持独立转存
---

## 依赖安装

```bash
git clone --branch release/6.0.0 https://github.com/leitaovpn/opencli.git
cd opencli && pip install -e .
```

## 可用资源

- `scripts/pan.py` — 网盘资源搜索、预览与转存脚本
- `prompts/match_system.txt` — LLM 匹配 system prompt


### 用法

```bash
# 搜索 + 预览
python pan.py <engine> <keyword> [--limit N] [--no-fetch] [--debug] [--driver <name>]... [--output <file>]

# 独立转存
python pan.py save <drive> <url> --name <name> --info source_folder=xxx,file_type=xxx,session=xxx,episode=xxx
```

> **注意**: 含空格或特殊字符的关键词必须用双引号包裹。
> `--info` 可重复，指定多个 source_folder 批量转存；不传 `--info` 时自动 walk 分享链接。

| 参数 | 说明 |
|------|------|
| `engine` | `google` 或 `baidu` |
| `keyword` | 搜索关键词 |
| `--limit N` | 返回结果数量，默认 10 |
| `--no-fetch` | 不抓取页面提取分享链接 |
| `--debug` | 输出追加 `commands` 字段，记录所有 opencli/curl 调用 |
| `--driver` | 限定网盘类型（可重复），如 `--driver quark` |
| `--output <file>` | 将结果写入指定文件 |

### save 子命令

| 参数 | 说明 |
|------|------|
| `drive` | `quark` 或 `aliyun` |
| `url` | 分享链接 |
| `--name` | 影视名（必填），续集用系列名 |
| `--info` | 匹配信息（可重复），格式: `source_folder=xxx,file_type=xxx,session=xxx,episode=xxx` |

**存放规则：**
- 电影：`/movie_agent/电影/{name}/file.mkv`
- 剧集：`/movie_agent/电视/{name}/S{season}/file.mkv`
- 直接保存视频文件，不包裹中间文件夹

---

### 支持的网盘

| driver | 分享链接域名 | opencli CLI | 搜索补全词 |
|--------|-------------|------------|-----------|
| `quark` | `pan.quark.cn/s/...` | `quark` | 夸克 |
| `aliyun` | `aliyundrive.com/s/...` | `alipan` | 阿里云盘 |

`--driver` 指定时自动补全网盘搜索词：`python pan.py google "超凡蜘蛛侠" --driver quark` → 实际搜索 `"超凡蜘蛛侠 夸克"`。

---

### 搜索流程

1. **搜索** → `opencli google/baidu search` → 提取分享链接
2. **获取元数据** → 用 keyword 搜索豆瓣（中文）或 IMDb（英文），转为 `video_infos`
3. **遍历分享** → 并行 walk 每个分享链接，收集所有视频文件的完整路径 → `source_folders`
4. **LLM 匹配** → 启动子 agent 执行匹配：
   - 以 `prompts/match_system.txt` 内容作为 system prompt
   - 将 `search_result` + `video_infos` 作为 user prompt
   - 子 agent 返回匹配结果 JSON 数组（含 `match_rate`、`session`、`episode`）
5. **转存** → 根据匹配结果调用 `pan.py save` 逐条转存

### 返回值结构（搜索）

```json
{
  "success": true,
  "engine": "google",
  "keyword": "黑袍纠察队",
  "search_result": [
    {
      "url": "https://pan.quark.cn/s/xxx",
      "type": "quark",
      "source_folders": [
        "黒.H.狍.P【1-4季】/第 01 季 (2019) 全8集/file.mkv",
        "黒.H.狍.P【1-4季】/第 02 季 (2020) 全8集/file.mkv"
      ]
    }
  ],
  "video_infos": [
    {
      "file_type": "电视剧",
      "name": "黑袍纠察队 第一季 The Boys Season 1 (2019)",
      "summary": "美国 / 动作 / 科幻 / 犯罪 / 60分钟"
    }
  ],
  "total": 1
}
```

### 返回值结构（save）

```json
{
  "success": true,
  "driver_type": "quark",
  "saved": [
    {
      "source_folder": "超人前传 1-10季/S01",
      "target_dir": "/movie_agent/电视/超人前传",
      "dest_folder": "S01"
    },
    {
      "source_folder": "超人前传 1-10季/S02",
      "target_dir": "/movie_agent/电视/超人前传",
      "dest_folder": "S02"
    }
  ],
  "error": null
}
```

---

### LLM 匹配输出格式

LLM 接收到 `search_result` + `video_infos` 后，输出 JSON 数组：

```json
[
  {
    "url": "https://pan.quark.cn/s/xxx",
    "type": "quark",
    "source_folder": "黒.H.狍.P/第 01 季 (2019)/file.mkv",
    "name": "黑袍纠察队 第一季 The Boys Season 1 (2019)",
    "file_type": "电视剧",
    "session": "1",
    "episode": "-1",
    "match_rate": 90
  }
]
```

- 每个 source_folder 只匹配一条，match_rate >= 70 才输出
- session/episode 从路径提取，提取不到填 -1
- 具体规则见 `prompts/match_system.txt`

---

### file_type 推断

`video_infos` 中的 `file_type` 优先从 name 推断（`第X季`/`Season` 关键词 → 电视剧），推断不出则回退到豆瓣/IMDb 返回的 type 字段。

分类关键词（`FILE_TYPE_PATTERNS`，用于路径分类）：

| 文件类型 | 典型关键词 |
|---------|-----------|
| 电视 | 电视剧、第X季、S\d{2}、series |
| 综艺 | 综艺、真人秀、variety |
| 纪录片 | 纪录片、BBC、discovery |
| 电影 | 4K、1080p、蓝光、BluRay（兜底） |

---

### 浏览器管理

- 搜索前自动检测 Google Chrome 是否运行（macOS: osascript，Windows: tasklist，Linux: pgrep）
- 未打开时自动启动 Chrome（macOS 指定 `--profile-directory=Default` 跳过用户选择）
- 轮询等待浏览器就绪，最多 15s，每秒检测一次
- 全部执行结束后，若浏览器由本脚本启动则自动退出 Chrome

---

### 并行化

| 阶段 | 并发数 | 内容 |
|------|--------|------|
| 搜索 | 5 | 多页面 curl 抓取分享链接 |
| 预览 | 5 | 多分享 walk 收集 source_folders |
| 转存 | 3 | 多文件 save |

---

### 容错与重试

- 所有 `opencli` 调用失败/超时自动重试 2 次（共 3 次）
- save API 返回失败后，通过 `list` 确认目标目录是否有视频文件
- `--overwrite true`：同名文件直接覆盖
- `_DRIVE_CLI` 映射：`aliyun` → opencli CLI 名 `alipan`

### 搜索技巧

- 中文资源配 `--driver quark` 省略网盘名：`python pan.py google "超凡蜘蛛侠" --driver quark`
- 用 `--no-fetch` 跳过页面抓取快速查看原始搜索结果
- `--debug` 查看所有命令调用链
- `--output /tmp/result.json` 将结果持久化到文件
