"""
opencli 网盘资源搜索脚本

用法:
    python pan.py -h                           # 查看帮助
    python pan.py <engine> <keyword> [--limit N] [--no-fetch] [--save] [--debug]

示例:
    python pan.py google "肖申克的救赎 夸克" --save --debug
"""

import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

SUPPORTED_DRIVES = frozenset({"quark", "aliyun"})

# share type → opencli CLI 名称
_DRIVE_CLI: dict[str, str] = {
    "quark": "quark",
    "aliyun": "alipan",
}

# driver → 搜索时追加的关键词
_DRIVER_KEYWORDS: dict[str, str] = {
    "quark": "夸克",
    "aliyun": "阿里云盘",
}

# 括号内容（中英文圆括号、方括号、中文书名号外的括号）—— 通常包裹画质/版本/年份等附加信息
_PAREN_RE = re.compile(r"\(.*?\)|（.*?）|\[.*?\]|［.*?］|【.*?】")

# IMDb 搜索噪声模式 —— 去除画质、编码、来源、平台名等非片名信息
_SEARCH_NOISE_RE = re.compile(
    r"(?<![a-zA-Z])(?:4[Kk]\d*|[28][Kk]|720[Pp]|1080[Pp]|2160[Pp]|4320[Pp]|SDR|HDR(?:10?\+?)?|UHD)\b|"
    r"\b(?:H\.?26[45]|[Xx]26[45]|HEVC|AVC|AV1|VP9)\b|"
    r"\b(?:WEB[.\- ]?DL|WEBRip|Blu[ \-]?Ray|BDRip|HD[.\- ]?Rip|HDTV|AMZN|NF)\b|"
    r"\b(?:DDP?[578]\.\d|AAC\d?|Atmos|TrueHD|DTS[ \-]?HD|FLAC|AC3|EAC3)\b|"
    r"\b(?:REPACK|PROPER|EXTENDED|IMAX|DS4K|S\d{2,})\b|"
    r"\b(?:19|20)\d{2}\b|"
    r"\d*帧|"
    r"(?:CHT|CHS|ENG|中文字幕|中字|双字|字幕|双语|三语|多语|内[封嵌]|官中|高码[率]?|高[码清]|标清|超清|"
    r"繁体|简体|禁[转传]|[有無无]删减|[无未]删|完整版?|修正版?|删减版?|"
    r"[中国英日韩法德俄沪粤]{1,3}[语双](?:[字语双])?|[国中][英日韩法德俄沪粤]|"
    r"纯[净清]版本?|无[水印广告]|免费版?|夸[克云]|阿里云盘|[网云]盘)",
    re.IGNORECASE | re.ASCII,
)

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".m4v", ".rmvb", ".iso", ".vob", ".mpeg", ".mpg",
})

_cmd_log: list[dict] = []

def _log_cmd(entry: dict):
    cmd = entry["cmd"]
    parts = [f"'{a}'" if " " in a else a for a in cmd]
    _cmd_log.append({"cmd": " ".join(parts), "success": entry["success"], "output": entry["output"]})

# 网盘分享链接识别模式
SHARE_PATTERNS: dict[str, re.Pattern] = {
    "quark": re.compile(r"https?://pan\.quark\.cn/s/[a-zA-Z0-9]+"),
    "aliyun": re.compile(r"https?://(?:www\.)?aliyundrive\.com/s/[a-zA-Z0-9]+"),
}

# 文件类型分类关键词
FILE_TYPE_PATTERNS: dict[str, list[str]] = {
    "电视": ["电视剧", "连续剧", "剧集", "第.*季", "episode", "s\\d{2}",
             "更新", "连载", "tv", "series"],
    "综艺": ["综艺", "真人秀", "脱口秀", "variety", "show"],
    "纪录片": ["纪录片", "documentary", "纪实", "探索", "bbc", "国家地理",
               "natgeo", "discovery"],
    "电影": ["电影", "movie", "1080p", "4k", "bluray", "蓝光", "2160p",
             "uhd", "hdr", "杜比", "dolby", "remux", "web-dl", "webdl",
             "x264", "x265", "hevc", "bdrip", "hdrip"],
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def run_opencli(*args: str, timeout: int = 60, retries: int = 2) -> dict:
    """执行 opencli 命令并返回结构化结果，失败时自动重试。"""
    cmd = ["opencli", *args]
    last_error: dict | None = None
    for attempt in range(retries + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                error_msg = result.stderr.strip() or output
                last_error = {"cmd": cmd, "success": False, "output": error_msg}
                if attempt < retries:
                    continue
                _log_cmd(last_error)
                return {
                    "success": False,
                    "error": error_msg,
                    "raw_output": output,
                }

            _log_cmd({"cmd": cmd, "success": True, "output": output})
            try:
                return {"success": True, "data": json.loads(output), "raw_output": output}
            except json.JSONDecodeError:
                return {"success": True, "data": output, "raw_output": output}

        except subprocess.TimeoutExpired:
            last_error = {"cmd": cmd, "success": False, "output": "timeout"}
            if attempt < retries:
                continue
            _log_cmd(last_error)
            return {"success": False, "error": "命令执行超时", "raw_output": ""}
        except FileNotFoundError:
            _log_cmd({"cmd": cmd, "success": False, "output": "opencli not found"})
            return {"success": False, "error": "opencli 未安装或未找到", "raw_output": ""}

    return {"success": False, "error": "未知错误", "raw_output": ""}


_BROWSER_START_TIMEOUT = 15  # 等待浏览器启动的最大秒数
_browser_launched: bool = False   # 标记是否由本脚本启动了浏览器


def _check_browser_running() -> bool:
    """检测 Google Chrome 是否正在运行。"""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to (name of processes) contains "Google Chrome"'],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() == "true"
        elif sys.platform == "win32":
            cmd = 'tasklist 2>nul | findstr /i "chrome.exe"'
            return subprocess.run(cmd, shell=True, capture_output=True, timeout=5).returncode == 0
        else:
            return subprocess.run(["pgrep", "-i", "chrome"], capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


def _launch_browser() -> bool:
    """尝试启动 Google Chrome，返回是否成功发出启动命令。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "Google Chrome", "--args", "--profile-directory=Default"], check=True, timeout=5)
        elif sys.platform == "win32":
            subprocess.run(["cmd", "/c", "start", "chrome"], check=True, timeout=5)
        else:
            subprocess.run(["google-chrome", "--no-startup-window"], check=True, timeout=5)
        return True
    except Exception:
        return False


def _ensure_browser() -> None:
    """检测浏览器是否已打开，未打开则自动启动并等待就绪。"""
    global _browser_launched
    if _check_browser_running():
        print("检测到浏览器正在运行，继续执行", file=sys.stderr)
        return

    print("未检测到浏览器，正在启动...", file=sys.stderr)
    if not _launch_browser():
        print("无法启动浏览器，继续执行", file=sys.stderr)
        return

    _browser_launched = True

    print("等待浏览器就绪...", file=sys.stderr)
    waited = 0
    while waited < _BROWSER_START_TIMEOUT:
        time.sleep(1)
        waited += 1
        if _check_browser_running():
            print(f"浏览器已就绪 ({waited}s)", file=sys.stderr)
            return
        if waited % 5 == 0:
            print(f"等待浏览器就绪... ({waited}s)", file=sys.stderr)

    print(f"浏览器启动超时 ({_BROWSER_START_TIMEOUT}s)，继续执行", file=sys.stderr)


def _close_browser() -> None:
    """关闭本脚本启动的浏览器（仅当浏览器由本脚本启动时）。"""
    global _browser_launched
    if not _browser_launched:
        return
    _browser_launched = False
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                capture_output=True, timeout=5,
            )
        elif sys.platform == "win32":
            subprocess.run(
                ["cmd", "/c", "taskkill /IM chrome.exe"],
                capture_output=True, timeout=5,
            )
        else:
            subprocess.run(["pkill", "chrome"], capture_output=True, timeout=5)
    except Exception:
        pass


def classify_file_type(title: str) -> str:
    """根据文件名/标题推断文件类型。"""
    lower = title.lower()
    for file_type, patterns in FILE_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return file_type
    return "其他"


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def _match_share_type(url: str, drivers: frozenset[str] = SUPPORTED_DRIVES) -> str | None:
    """匹配分享链接并返回网盘类型，不在 drivers 内返回 None。"""
    for drive_type, pattern in SHARE_PATTERNS.items():
        if drive_type not in drivers:
            continue
        if pattern.match(url):
            return drive_type
    return None


def extract_share_links(text: str, drivers: frozenset[str] = SUPPORTED_DRIVES) -> list[dict]:
    """从文本中提取目标网盘的分享链接（仅 drivers 指定的类型）。"""
    found: list[dict] = []
    seen: set[str] = set()

    for drive_type, pattern in SHARE_PATTERNS.items():
        if drive_type not in drivers:
            continue
        for match in pattern.finditer(text):
            url = match.group(0)
            if url not in seen:
                seen.add(url)
                found.append({"type": drive_type, "url": url})

    return found


_PAGE_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

def fetch_page_links(url: str, timeout: int = 15, drivers: frozenset[str] = SUPPORTED_DRIVES) -> tuple[list[dict], str]:
    """抓取页面并提取其中的网盘分享链接和页面标题。"""
    cmd = ["curl", "-sL", "--max-time", str(timeout), url]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout + 5,
        )
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode != 0 or not output:
            _log_cmd({"cmd": cmd, "success": False, "output": result.stderr.decode("utf-8", errors="replace").strip() or output or "(empty)"})
            return [], ""
        _log_cmd({"cmd": cmd, "success": True, "output": f"{len(output)} bytes"})
        page_title = ""
        m = _PAGE_TITLE_RE.search(output)
        if m:
            page_title = m.group(1).strip()
        return extract_share_links(output, drivers), page_title
    except (subprocess.TimeoutExpired, UnicodeError):
        _log_cmd({"cmd": cmd, "success": False, "output": "timeout or decode error"})
        return [], ""


def search(engine: str, keyword: str, limit: int = 10, no_fetch: bool = False, drivers: frozenset[str] = SUPPORTED_DRIVES) -> dict:
    """调用 opencli 搜索，提取并过滤网盘分享链接。"""
    _ensure_browser()

    # 如果指定了特定 driver，追加对应搜索词提升精准度
    search_kw = keyword
    if drivers:
        extras = " ".join(_DRIVER_KEYWORDS[d] for d in sorted(drivers) if d in _DRIVER_KEYWORDS and _DRIVER_KEYWORDS[d] not in keyword)
        if extras:
            search_kw = f"{keyword} {extras}"
    result = run_opencli(engine, "search", search_kw, "--limit", str(limit), "-f", "json", timeout=30)
    if not result["success"]:
        return {
            "success": False,
            "engine": engine,
            "keyword": keyword,
            "error": result.get("error", "搜索失败"),
            "results": [],
        }

    raw_items = result["data"]
    if not isinstance(raw_items, list):
        raw_items = [raw_items]

    share_results: list[dict] = []
    seen_shares: set[str] = set()

    # 分离直达链接和需抓取的页面
    fetch_items: list[dict] = []
    for item in raw_items:
        page_url = item.get("url", "")
        if not page_url:
            continue
        search_title = item.get("title", "")
        matched_type = _match_share_type(page_url, drivers)
        if matched_type:
            resource_title = _clean_for_search(search_title)
            if page_url not in seen_shares:
                seen_shares.add(page_url)
                share_results.append({
                    "type": matched_type, "url": page_url,
                    "source": page_url, "title": resource_title,
                })
        elif not no_fetch:
            fetch_items.append(item)

    # 并行抓取页面
    if fetch_items:

        def _fetch_one(item: dict):
            url = item.get("url", "")
            links, page_title = fetch_page_links(url, drivers=drivers)
            search_title = item.get("title", "")
            resource_title = _clean_for_search(search_title) or _clean_for_search(page_title)
            return [(link["type"], link["url"], url, resource_title) for link in links]

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_one, item): item for item in fetch_items}
            for future in as_completed(futures):
                try:
                    for drive_type, link_url, source_url, rt in future.result():
                        if link_url not in seen_shares:
                            seen_shares.add(link_url)
                            share_results.append({
                                "type": drive_type, "url": link_url,
                                "source": source_url, "title": rt,
                            })
                except Exception:
                    pass

    return {
        "success": True,
        "engine": engine,
        "keyword": keyword,
        "total": len(share_results),
        "results": share_results,
    }


# ---------------------------------------------------------------------------
# 转存
# ---------------------------------------------------------------------------

def _clean_for_search(name: str) -> str:
    """去除画质/编码噪音，提取可用于 IMDb 搜索的片名关键词。"""
    cleaned = _PAREN_RE.sub(" ", name)
    cleaned = _SEARCH_NOISE_RE.sub("", cleaned)
    cleaned = re.sub(r"[.,\-_/：:;；、丨#@~!+×&]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else name


_CHINESE_RE = re.compile(r"[一-鿿]")

def _is_chinese(text: str) -> bool:
    return bool(_CHINESE_RE.search(text))


def _search_douban(query: str) -> dict | None:
    """搜索豆瓣，返回最佳匹配 {title, year, douban_id, type} 或 None。"""
    result = run_opencli("douban", "search", query, "--type", "movie", "--limit", "1", "-f", "json", timeout=15)
    if not result["success"]:
        return None
    items = result["data"]
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    raw_title = first.get("title", "")
    year_match = re.search(r"\((\d{4})\)", raw_title)
    return {
        "title": raw_title,
        "year": year_match.group(1) if year_match else "",
        "douban_id": first.get("id", ""),
        "type": first.get("type", "movie"),
        "source": "douban",
    }


def _search_imdb(query: str) -> dict | None:
    """搜索 IMDb，返回最佳匹配 {title, year, imdb_id, type} 或 None。"""
    result = run_opencli("imdb", "search", query, "--limit", "1", "-f", "json", timeout=15)
    if not result["success"]:
        return None
    items = result["data"]
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    return {
        "title": first.get("title", ""),
        "year": first.get("year", ""),
        "imdb_id": first.get("imdb_id", ""),
        "type": first.get("type", ""),
        "source": "imdb",
    }


_SUMMARY_MAX_LEN = 80

def _search_douban_multi(query: str, limit: int = 20) -> list[dict]:
    """多结果豆瓣搜索。"""
    result = run_opencli("douban", "search", query, "--type", "movie", "--limit", str(limit), "-f", "json", timeout=15)
    if not result["success"]:
        return []
    items = result["data"]
    if not isinstance(items, list):
        return []
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        raw = d.get("title", "")
        m = re.search(r"\((\d{4})\)", raw)
        abstract = d.get("abstract", "")
        if abstract and len(abstract) > _SUMMARY_MAX_LEN:
            abstract = abstract[:_SUMMARY_MAX_LEN] + "…"
        out.append({
            "title": raw,
            "year": m.group(1) if m else "",
            "douban_id": d.get("id", ""),
            "type": d.get("type", "movie"),
            "source": "douban",
            "summary": abstract,
        })
    return out


def _search_imdb_multi(query: str, limit: int = 20) -> list[dict]:
    """多结果 IMDb 搜索。"""
    result = run_opencli("imdb", "search", query, "--limit", str(limit), "-f", "json", timeout=15)
    if not result["success"]:
        return []
    items = result["data"]
    if not isinstance(items, list):
        return []
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        stars = d.get("stars", "")
        if stars and len(stars) > _SUMMARY_MAX_LEN:
            stars = stars[:_SUMMARY_MAX_LEN] + "…"
        out.append({
            "title": d.get("title", ""),
            "year": d.get("year", ""),
            "imdb_id": d.get("imdb_id", ""),
            "type": d.get("type", ""),
            "source": "imdb",
            "summary": stars,
        })
    return out


def _search_meta_multi(query: str, limit: int = 20) -> list[dict]:
    """多结果元数据搜索。"""
    if _is_chinese(query):
        items = _search_douban_multi(query, limit)
        if items:
            return items
    return _search_imdb_multi(query, limit)


def _infer_file_type(name: str, fallback_type: str) -> str:
    """从片名推断 file_type，name 判断不了则回退到候选类型。"""
    tv_patterns = FILE_TYPE_PATTERNS.get("电视", [])
    lower = name.lower()
    for p in tv_patterns:
        if re.search(p, lower):
            return "电视剧"
    type_map = {"movie": "电影", "tv": "电视剧", "tvSeries": "电视剧"}
    return type_map.get(fallback_type, "其他")


def _candidates_to_video_infos(candidates: list[dict]) -> list[dict]:
    """将元数据候选转为 video_infos 格式，按 name 去重。"""
    seen: set[str] = set()
    result: list[dict] = []
    for c in candidates:
        name = c.get("title", "")
        if name and name not in seen:
            seen.add(name)
            result.append({
                "file_type": _infer_file_type(name, c.get("type", "")),
                "name": name,
                "summary": c.get("summary", ""),
            })
    return result


def _drive_cmd(drive: str, *args: str, **kwargs) -> dict:
    """map share type → opencli CLI name, then run"""
    cli = _DRIVE_CLI.get(drive, drive)
    return run_opencli(cli, *args, **kwargs)


def _is_video(name: str) -> bool:
    return any(name.lower().endswith(ext) for ext in VIDEO_EXTENSIONS)


def _norm_name(item: dict) -> str:
    return (item.get("name") or item.get("file_name") or item.get("title") or "").strip()


def _fetch_share_dir(drive: str, url: str, path: str = "") -> list[dict]:
    """调用 share-list 获取指定路径的内容。"""
    if path:
        result = _drive_cmd(drive, "share-list", url, "--path", path, "-f", "json", timeout=30)
    else:
        result = _drive_cmd(drive, "share-list", url, "-f", "json", timeout=30)
    if not result["success"]:
        return []
    data = result["data"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


_SEASON_RE = re.compile(r"[Ss](\d{1,2})|第\s*(\d{1,2})\s*季|[Ss]eason\s*(\d{1,2})")


def _extract_season(parts: list[str]) -> str:
    """从路径片段中提取季数，未找到返回空字符串。"""
    for p in parts:
        m = _SEASON_RE.search(p)
        if m:
            return m.group(1) or m.group(2) or m.group(3) or ""
    return ""


def _walk_share(drive: str, url: str, path: str, parents: list[str]) -> list[dict]:
    """递归列出分享内容，收集包含视频的路径。"""
    items = _fetch_share_dir(drive, url, path)
    found: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _norm_name(item)
        if not name:
            continue
        item_type = (item.get("type") or "").lower()
        if item_type in ("folder", "dir"):
            sub_path = f"{path}/{name}" if path else name
            found.extend(_walk_share(drive, url, sub_path, parents + [name]))
        elif _is_video(name):
            found.append({"path_parts": parents + [name]})
    return found


def _classify_from_parts(parts: list[str]) -> str:
    """从路径片段分类。越深优先级越高，匹配后立即返回。"""
    # 逐段从深到浅匹配
    for i in range(len(parts) - 1, -1, -1):
        lower = parts[i].lower()
        for file_type, patterns in FILE_TYPE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, lower):
                    return file_type
    # 全路径合并兜底
    lower = "/".join(parts).lower()
    for file_type, patterns in FILE_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return file_type
    return "其他"


def _extract_imdb_title(imdb: dict) -> str:
    """从 IMDb 结果中提取可用于搜索的英文标题。"""
    title = imdb.get("title", "")
    return re.sub(r"\s+", " ", re.sub(r"[:;,\-]+", " ", title)).strip()


# ---------------------------------------------------------------------------
# 命令路由 & CLI
# ---------------------------------------------------------------------------

def _parse_drivers(args: list[str]) -> frozenset[str]:
    """从参数中提取 --driver 值，允许多次重复。"""
    drivers = []
    i = 0
    while i < len(args):
        if args[i] == "--driver" and i + 1 < len(args):
            drivers.append(args[i + 1])
            i += 1
        i += 1
    if not drivers:
        return SUPPORTED_DRIVES
    return frozenset(d for d in drivers if d in SUPPORTED_DRIVES)


def _preview_result(item: dict) -> dict:
    """Walk share，收集 source_folders。"""
    drive = item["type"]
    url = item["url"]

    if drive not in SUPPORTED_DRIVES:
        return {"url": url, "type": drive, "source_folders": []}

    video_paths = _walk_share(drive, url, "", [])
    if not video_paths:
        return {"url": url, "type": drive, "source_folders": []}

    source_folders: list[str] = []
    for vp in video_paths:
        source_folders.append("/".join(vp["path_parts"]))

    return {"url": url, "type": drive, "source_folders": source_folders}


def handle_search(args: list[str]) -> dict:
    """搜索命令：python pan.py <engine> <keyword> [--limit N] [--no-fetch] [--debug] [--driver <name>]..."""
    if len(args) < 2:
        return {"success": False, "error": "用法: python pan.py <engine> <keyword> [--limit N] [--no-fetch] [--debug] [--driver <name>]..."}

    engine = args[0].lower()
    if engine not in ("google", "baidu"):
        return {"success": False, "error": f"不支持的搜索引擎: {engine}，仅支持 google/baidu"}

    keyword = args[1]
    limit = 10
    no_fetch = "--no-fetch" in args
    drivers = _parse_drivers(args)

    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            limit = int(args[idx + 1])

    print(f"搜索 '{keyword}' ...", file=sys.stderr)
    result = search(engine, keyword, limit, no_fetch, drivers=drivers)

    if not result["success"] or not result["results"]:
        return result

    raw_count = len(result["results"])
    print(f"搜索到 {raw_count} 条网盘链接，正在获取元数据...", file=sys.stderr)

    # 仅用 keyword 搜一次元数据（最多 20 条），所有分享共享
    candidates = _search_meta_multi(keyword) if keyword else []
    video_infos = _candidates_to_video_infos(candidates)
    print(f"获取到 {len(video_infos)} 条影片信息，正在分析分享链接...", file=sys.stderr)

    # 并行收集每个分享链接的 source_folders
    pool = ThreadPoolExecutor(max_workers=5)
    try:
        futures = {pool.submit(_preview_result, item): item for item in result["results"]}
        search_result = []
        done = 0
        for future in as_completed(futures):
            try:
                preview = future.result()
                done += 1
                n = len(preview.get("source_folders", []))
                print(f"  [{done}/{raw_count}] {preview['url']} -> {n} 个目录", file=sys.stderr)
                if preview["source_folders"]:
                    search_result.append(preview)
            except Exception:
                done += 1
                print(f"  [{done}/{raw_count}] 分析失败", file=sys.stderr)
    finally:
        pool.shutdown(wait=True)

    return {
        "success": True,
        "engine": engine,
        "keyword": keyword,
        "search_result": search_result,
        "video_infos": video_infos,
        "total": len(search_result),
    }


def _verify_save(drive: str, target: str) -> bool:
    """通过 list 确认目标路径下是否有视频文件。"""
    ls = _drive_cmd(drive, "list", "--path", target, "-f", "json", timeout=15)
    if ls["success"] and isinstance(ls["data"], list):
        return any(isinstance(f, dict) and _is_video(f.get("name", "") or f.get("file_name", "")) for f in ls["data"])
    return False


def _save_folder(drive: str, url: str, source_folder: str, parent_dir: str, new_name: str) -> bool:
    """保存分享中的文件夹到 parent_dir，然后 rename 为新名。成功返回 True。"""
    if source_folder:
        args = [drive, "save", url, "--to-path", parent_dir, "--source-path", source_folder, "--overwrite", "true", "-f", "json"]
    else:
        args = [drive, "save", url, "--to-path", parent_dir, "--overwrite", "true", "-f", "json"]

    result = _drive_cmd(*args, timeout=120)
    ok = False
    if result["success"]:
        data = result["data"]
        ok = not (isinstance(data, dict) and (data.get("success") is False or data.get("code") != 0))

    # 确认保存成功
    if not ok:
        ok = _verify_save(drive, parent_dir)

    if not ok:
        return False

    # rename 保存后的文件夹
    leaf = source_folder.split("/")[-1] if source_folder else ""
    if leaf and leaf != new_name:
        old_path = f"{parent_dir}/{leaf}"
        _drive_cmd(drive, "rename", "--path", old_path, "--new-name", new_name, "-f", "json", timeout=15)
    return True


def _parse_info(raw: str) -> dict[str, str]:
    """解析 --info 值：source_folder=xxx,file_type=xxx,session=xxx,episode=xxx"""
    info: dict[str, str] = {}
    for kv in raw.split(","):
        kv = kv.strip()
        if "=" in kv:
            k, v = kv.split("=", 1)
            info[k.strip()] = v.strip()
    return info


def _save_one(drive: str, url: str, name: str, info: dict[str, str]) -> dict | None:
    """转存单个 source_folder，返回 {source_folder, target_dir, dest_folder} 或 None。"""
    file_type = info.get("file_type", "")
    source_folder = info.get("source_folder", "")
    session = info.get("session", "-1")

    show_dir = f"/movie_agent/{file_type}/{name}"
    rename_to = ""
    if file_type in ("电视", "综艺") and session not in ("", "-1"):
        try:
            rename_to = f"S{int(session):02d}"
        except ValueError:
            pass

    leaf = source_folder.split("/")[-1]
    dest_folder = rename_to if rename_to else leaf

    _drive_cmd(drive, "mkdir", show_dir, "--parents", "true", timeout=15)
    if _save_folder(drive, url, source_folder, show_dir, rename_to):
        return {"source_folder": source_folder, "target_dir": show_dir, "dest_folder": dest_folder}
    return None


def handle_save(args: list[str]) -> dict:
    """独立转存命令：python pan.py save <drive> <url> --name <name> --info source_folder=xxx,file_type=xxx,session=xxx,episode=xxx"""
    drive = args[0] if args else ""
    if drive not in SUPPORTED_DRIVES:
        return {"success": False, "error": f"用法: save <drive> <url> --name <name> --info ... 不支持的网盘: {drive}"}

    i = 1
    url = ""
    name = ""
    infos: list[dict[str, str]] = []
    while i < len(args):
        a = args[i]
        if a == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 1
        elif a == "--info" and i + 1 < len(args):
            info = _parse_info(args[i + 1])
            if info:
                infos.append(info)
            i += 1
        elif not a.startswith("--") and not url:
            url = a
        i += 1

    if not url or not name:
        return {"success": False, "error": "用法: save <drive> <url> --name <name> --info source_folder=xxx,file_type=xxx,session=xxx,episode=xxx"}

    _ensure_browser()

    saved: list[dict] = []

    if infos:
        for info in infos:
            r = _save_one(drive, url, name, info)
            if r is not None:
                saved.append(r)
        return {
            "success": bool(saved),
            "driver_type": drive,
            "saved": saved,
            "error": None if saved else "所有文件夹保存失败",
        }

    # 无 --info：walk 分享链接自动发现所有视频
    video_paths = _walk_share(drive, url, "", [])
    if not video_paths:
        return {"success": False, "error": "未找到视频文件"}

    file_type = ""
    for vp in video_paths:
        file_type = _classify_from_parts(vp["path_parts"])
        if file_type != "其他":
            break

    folders: dict[str, list[list[str]]] = {}
    for vp in video_paths:
        parts = vp["path_parts"]
        sf = "/".join(parts[:-1]) if len(parts) > 1 else ""
        if sf not in folders:
            folders[sf] = []
        folders[sf].append(parts)

    show_dir = f"/movie_agent/{file_type}/{name}"

    def _save_folder_job(sf: str, f_parts: list[list[str]]) -> dict | None:
        season = ""
        if file_type in ("电视", "综艺"):
            season = _extract_season(f_parts[0])
        if season:
            try:
                season = f"S{int(season):02d}"
            except ValueError:
                pass
        rename_to = season if season else ""
        leaf = sf.split("/")[-1] if sf else ""
        dest_folder = rename_to if rename_to else leaf
        _drive_cmd(drive, "mkdir", show_dir, "--parents", "true", timeout=15)
        if _save_folder(drive, url, sf, show_dir, rename_to):
            return {"source_folder": sf, "target_dir": show_dir, "dest_folder": dest_folder}
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_save_folder_job, sf, fps): sf for sf, fps in folders.items()}
        for future in as_completed(futures):
            try:
                r = future.result()
                if r is not None:
                    saved.append(r)
            except Exception:
                pass

    return {
        "success": bool(saved),
        "driver_type": drive,
        "saved": saved,
        "error": None if saved else "所有文件夹保存失败",
    }


def print_help():
    print("""用法:
    python pan.py -h                           查看帮助
    python pan.py <engine> <keyword> [--limit N] [--no-fetch] [--debug] [--driver <name>]... [--output <file>]
    python pan.py save <drive> <url> --name <name> --info source_folder=xxx,file_type=xxx,session=xxx,episode=xxx

    --info  匹配信息，可重复。每项为逗号分隔的 key=value 对

    支持搜索引擎: google、baidu
    支持网盘: quark（夸克）、aliyun（阿里云盘）

    --debug   输出中追加 commands 字段，记录所有 opencli/curl 调用
    --driver  限定网盘类型（可重复），如 --driver quark --driver aliyun
    --output  将结果写入指定文件""")


def _pop_arg(args: list[str], flag: str) -> str | None:
    """从 args 中提取 --flag <value>，返回 value 并从列表移除。"""
    try:
        idx = args.index(flag)
        val = args[idx + 1]
        del args[idx:idx + 2]
        return val
    except (ValueError, IndexError):
        return None


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print_help()
        return

    output_path = _pop_arg(args, "--output")

    if args[0] == "save":
        result = handle_save(args[1:])
    else:
        result = handle_search(args)
    if "--debug" in args:
        result["commands"] = _cmd_log

    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    print(json_str)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"结果已写入 {output_path}", file=sys.stderr)

    _close_browser()


if __name__ == "__main__":
    main()
