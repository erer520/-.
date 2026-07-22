#!/usr/bin/env python3
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, urlunparse, unquote, unquote_plus

# 配置（按需调整）
SOURCE_FILE = 'my_source.m3u'
OUTPUT_FILE = 'valid_sub.m3u'
WORKERS = 50           # 并发线程数（单个组内部最大线程数）
TIMEOUT = 6            # 单个请求超时（秒)
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
        # 使用 urlunparse 重新构造
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

    # 解析输入文件，按照组（group）收集：每个 #EXTINF 开始一个组，后续的若干 URL 被视为同组的备用地址。
    # 没有 #EXTINF 的单独 URL 也作为单独组处理。
    groups = []  # list of {'name':..., 'extinf':..., 'urls':[...]} 保持原始出现顺序
    seen_orig = set()

    with open(SOURCE_FILE, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    current_group = None
    while i < len(lines):
        line = lines[i].rstrip('\n')
        stripped = line.strip()
        if stripped.startswith('#EXTINF'):
            # 开始新组
            extinf = line
            name = extract_name_from_extinf(extinf)
            preferred_name = sanitize_name(name) if name else None
            current_group = {'name': preferred_name, 'extinf': extinf, 'urls': []}
            i += 1
            # 收集紧跟其后的 URL 行，直到遇到下一个 #EXTINF 或空的注释行
            while i < len(lines):
                nxt = lines[i].strip()
                if nxt.startswith('#EXTINF'):
                    break
                if nxt.lower().startswith('http'):
                    url = nxt
                    if url not in seen_orig:
                        seen_orig.add(url)
                        current_group['urls'].append(url)
                    i += 1
                    # 继续收集，允许多条 URL 属于同一组
                    continue
                # 如果是空行或其他注释，跳过并继续
                i += 1
            # 如果这个组没有 URL（非常规），则忽略
            if current_group['urls']:
                groups.append(current_group)
            current_group = None
            continue
        elif stripped.lower().startswith('http'):
            # 单独 URL，作为单独组
            url = stripped
            if url not in seen_orig:
                seen_orig.add(url)
                groups.append({'name': None, 'extinf': None, 'urls': [url]})
            i += 1
        else:
            i += 1

    if not groups:
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
        print("No URLs found in source file, wrote empty playlist.")
        return

    # 针对每个组并行检测其内部的 URL，但每组一旦检测到第一个有效 URL，立即切换到下一组（停止处理该组剩余 URL）
    group_results = []  # list of (final_url, preferred_name, orig_extinf)

    for gidx, group in enumerate(groups, start=1):
        urls = group['urls']
        preferred_name = group['name']
        orig_extinf = group['extinf']

        print(f"Checking group {gidx}/{len(groups)} with {len(urls)} url(s)")

        final_for_group = None
        final_checked_url = None

        # 使用一个临时线程池检测组内 URL（上限为 WORKERS 或 url 数量）
        with ThreadPoolExecutor(max_workers=min(WORKERS, max(1, len(urls)))) as ex:
            futures = {ex.submit(check_url_return_final, url): url for url in urls}
            try:
                for fut in as_completed(futures):
                    orig = futures[fut]
                    try:
                        final = fut.result()
                        if final:
                            final_for_group = final
                            final_checked_url = orig
                            print("Group OK:", orig, "->", final)
                            # 找到一个有效后，尝试取消还未开始的 futures
                            for other_fut in futures:
                                if other_fut is not fut:
                                    try:
                                        other_fut.cancel()
                                    except Exception:
                                        pass
                            break
                        else:
                            print("Bad:", orig)
                    except Exception as e:
                        print("Error checking:", orig, e)
                # 退出时，未被处理或被取消的 futures 会被忽略
            except Exception:
                pass

        if final_for_group:
            group_results.append((final_for_group, preferred_name, orig_extinf))
        else:
            print(f"Group {gidx} has no valid url, skipping.")

    # 写输出时按最终 URL 去重（保留第一次出现的名称）
    emitted_final = set()
    emitted_names = {}  # name -> count (用于避免重复名称)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for final_url, preferred_name, orig_extinf in group_results:
            key = final_url
            if DEDUPE_BY_FINAL_URL:
                key = normalize_url_for_dedupe(final_url)
            if key in emitted_final:
                continue
            emitted_final.add(key)

            name = sanitize_name(preferred_name or title_from_url(final_url))
            count = emitted_names.get(name, 0) + 1
            emitted_names[name] = count
            out_name = name if count == 1 else f"{name} - {count}"

            f.write(EXTINF_TEMPLATE.format(name=out_name))
            f.write(final_url + '\n')

    print("Done. Valid unique count:", len(emitted_final))

if __name__ == '__main__':
    main()
