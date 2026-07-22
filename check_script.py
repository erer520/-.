#!/usr/bin/env python3
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, urlunquote, unquote

# 配置（按需调整）
SOURCE_FILE = 'my_source.m3u'
OUTPUT_FILE = 'valid_sub.m3u'
WORKERS = 50           # 并发线程数
TIMEOUT = 6            # 单个请求超时（秒）
READ_BYTES = 1024      # 读取判断用的字节数
HEAD_FIRST = True      # 先尝试 HEAD 请求（可节省流量）
RETRIES = 0            # urllib3 Retry 总重试次数

# 去重策略选项：
DEDUPE_BY_FINAL_URL = True   # True: 按重定向后的最终 URL 去重（推荐）
NORMALIZE_STRIP_FRAGMENT = True  # True: 去掉 fragment (#...) 并去掉末尾斜杠（小幅规范）
# 注意：不要默认去掉 query 参数（可能包含鉴权 token），除非你确定可以合并带不同 query 的 URL
NORMALIZE_STRIP_QUERY = False

# 输出 extinf 模板（按需修改）
# Example: #EXTINF:-1 logo="" group-title="" ,milf
EXTINF_TEMPLATE = '#EXTINF:-1 logo=\"\" group-title=\"\" ,{name}\n'

def is_m3u8_content_type(resp):
    ctype = resp.headers.get('Content-Type', '').lower()
    return ('mpegurl' in ctype) or ('.m3u8' in ctype) or ('application/vnd.apple.mpegurl' in ctype)

def normalize_url_for_dedupe(url):
    """
    对用于去重的 key 做轻量规范化：
    - 可选去掉 fragment
    - 可选去掉 query
    - 去掉末尾的斜杠
    返回规范化后的字符串（小写 host 保持）
    """
    try:
        p = urlparse(url)
        scheme = p.scheme
        netloc = p.netloc.lower()
        path = p.path.rstrip('/')
        query = '' if NORMALIZE_STRIP_QUERY else p.query
        # fragment removed intentionally
        norm = urlunparse((scheme, netloc, path or '/', '', query, ''))
        return norm
    except Exception:
        return url

# 准备 Session（连接池 + 重试）
session = requests.Session()
retries = Retry(total=RETRIES, backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504))
adapter = HTTPAdapter(pool_connections=100, pool_maxsize=200, max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({'User-Agent': 'Mozilla/5.0'})

def check_url_return_final(url):
    """
    检查 URL 是否有效播放列表。如果是，返回最终 URL (跟随重定向后的 r.url)；否则返回 None。
    不修改外部状态，线程安全。
    """
    try:
        # 先发 HEAD（节省流量）
        if HEAD_FIRST:
            try:
                r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
                final = r.url if hasattr(r, 'url') else url
                if r.status_code == 200 and is_m3u8_content_type(r):
                    r.close()
                    return final
                r.close()
            except Exception:
                pass

        # GET 一小段内容判断
        r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
        final = r.url if hasattr(r, 'url') else url
        if r.status_code != 200:
            r.close()
            return None

        if is_m3u8_content_type(r):
            r.close()
            return final

        try:
            chunk = next(r.iter_content(chunk_size=READ_BYTES), b'')
        except Exception:
            chunk = b''
        finally:
            r.close()

        if b'#EXTM3U' in chunk or b'#EXTINF' in chunk or b'.m3u8' in chunk:
            return final
    except Exception:
        return None
    return None

def title_from_url(url):
    """
    优先从 URL path 中按模式提取频道名：
    - 如果 path 中包含 'stream_<name>/playlist'，则返回 <name>
    - 否则回退到原有的 basename 提取逻辑（去掉 .m3u8 并将下划线替换为空格）

    例如: '/hls/stream_LucianaMeca/playlist.m3u8' -> 'LucianaMeca'
    """
    try:
        p = urlparse(url)
        path = unquote(p.path)
        m = re.search(r'stream_([^/]+)/playlist', path)
        if m:
            return m.group(1)
        name = os.path.basename(unquote(p.path))
        if name:
            name = name.replace('.m3u8', '').replace('_', ' ')
            return name
    except Exception:
        pass
    return url

def extract_name_from_extinf(extinf_line):
    """
    从 #EXTINF 行提取逗号后面的名称（若有），否则返回 None
    例如: '#EXTINF:-1,LucianaMeca' -> 'LucianaMeca'
    """
    try:
        if not extinf_line:
            return None
        # 找到第一个逗号并提取之后的内容
        idx = extinf_line.find(',')
        if idx != -1:
            name = extinf_line[idx+1:].strip()
            # 去掉可能的换行
            name = name.rstrip('\r\n')
            if name:
                return name
    except Exception:
        pass
    return None

def sanitize_name(name):
    # 简单清理，去掉多余空白
    return ' '.join(name.split())

def main():
    if not os.path.exists(SOURCE_FILE):
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
        print("Source file not found, wrote empty playlist.")
        return

    # 解析输入文件，配对 #EXTINF 与 URL（保留原始 extinf 名称或从 URL 生成）
    entries = []  # list of (orig_url, preferred_name, original_extinf_line or None)
    with open(SOURCE_FILE, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    seen_orig = set()
    while i < len(lines):
        line = lines[i].rstrip('\n')
        stripped = line.strip()
        if stripped.startswith('#EXTINF'):
            extinf = line
            name = extract_name_from_extinf(extinf)
            j = i + 1
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines):
                url_line = lines[j].strip()
                if url_line.lower().startswith('http'):
                    url = url_line
                    if url not in seen_orig:
                        seen_orig.add(url)
                        preferred_name = sanitize_name(name) if name else title_from_url(url)
                        entries.append((url, preferred_name, extinf))
                    i = j + 1
                    continue
            i += 1
        elif stripped.lower().startswith('http'):
            url = stripped
            if url not in seen_orig:
                seen_orig.add(url)
                preferred_name = title_from_url(url)
                entries.append((url, preferred_name, None))
            i += 1
        else:
            i += 1

    if not entries:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
        print("No URLs found in source file, wrote empty playlist.")
        return

    # 并行校验所有 entries，收集最终 URL（或 None）
    results = {}  # orig_url -> final_url or None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_url_return_final, url): url for (url, _, _) in entries}
        for fut in as_completed(futures):
            orig = futures[fut]
            try:
                final = fut.result()
                results[orig] = final
                if final:
                    print("OK:", orig, "->", final)
                else:
                    print("Bad:", orig)
            except Exception as e:
                results[orig] = None
                print("Error checking:", orig, e)

    # 写输出时按最终 URL 去重（保留第一次出现的名称）
    emitted_final = set()
    emitted_names = {}  # name -> count (用于避免重复名称)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for orig_url, preferred_name, orig_extinf in entries:
            final = results.get(orig_url)
            if not final:
                continue
            key = final
            if DEDUPE_BY_FINAL_URL:
                key = normalize_url_for_dedupe(final)
            # 如果已经写过相同最终 URL，就跳过
            if key in emitted_final:
                continue
            emitted_final.add(key)

            # 确保频道名唯一（若重复则追加序号）
            name = sanitize_name(preferred_name or title_from_url(final))
            count = emitted_names.get(name, 0) + 1
            emitted_names[name] = count
            out_name = name if count == 1 else f"{name} - {count}"

            # 写入 extinf（使用模板，格式示例: #EXTINF:-1 logo=\"\" group-title=\"\" ,ChannelName）
            f.write(EXTINF_TEMPLATE.format(name=out_name))
            f.write(final + '\n')

    print("Done. Valid unique count:", len(emitted_final))

if __name__ == '__main__':
    main()
