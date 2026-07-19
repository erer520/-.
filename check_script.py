#!/usr/bin/env python3
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse, unquote

# 配置（按需调整）
SOURCE_FILE = 'my_source.m3u'
OUTPUT_FILE = 'valid_sub.m3u'
WORKERS = 50           # 并发线程数：根据机器/网络调整（建议 10-40 之间）
TIMEOUT = 6            # 单个请求超时（秒）
READ_BYTES = 1024      # 读取判断用的字节数
HEAD_FIRST = True      # 先尝试 HEAD 请求（可节省流量）
RETRIES = 0            # urllib3 Retry 总重试次数

def is_m3u8_content_type(resp):
    ctype = resp.headers.get('Content-Type', '').lower()
    return ('mpegurl' in ctype) or ('.m3u8' in ctype) or ('application/vnd.apple.mpegurl' in ctype)

# 简单从 URL 得到一个可读标题（用于缺失 #EXTINF 的情况）
def title_from_url(url):
    try:
        p = urlparse(url)
        name = os.path.basename(p.path)
        name = unquote(name or '')
        if name:
            name = name.replace('.m3u8', '').replace('_', ' ')
            return name
    except Exception:
        pass
    return url

# 准备 Session（连接池 + 重试）
session = requests.Session()
retries = Retry(total=RETRIES, backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504))
adapter = HTTPAdapter(pool_connections=100, pool_maxsize=200, max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({'User-Agent': 'Mozilla/5.0'})

def check_url(url):
    """
    返回 url（若判断为 m3u8/playlist）或 None。
    不修改任何全局列表，线程安全。
    """
    try:
        # 先发 HEAD（节省流量）
        if HEAD_FIRST:
            try:
                r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
                if r.status_code == 200 and is_m3u8_content_type(r):
                    r.close()
                    return url
                r.close()
            except Exception:
                pass

        # GET 一小段内容判断
        r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code != 200:
            r.close()
            return None

        if is_m3u8_content_type(r):
            r.close()
            return url

        try:
            chunk = next(r.iter_content(chunk_size=READ_BYTES), b'')
        except Exception:
            chunk = b''
        finally:
            r.close()

        if b'#EXTM3U' in chunk or b'#EXTINF' in chunk or b'.m3u8' in chunk:
            return url
    except Exception:
        return None
    return None

def main():
    if not os.path.exists(SOURCE_FILE):
        # 若没有输入文件，写空的 playlist 并退出
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
        print("Source file not found, wrote empty playlist.")
        return

    # 解析输入文件，配对 #EXTINF 与 URL，同时去重（保留第一次出现的标题）
    entries = []  # list of (url, extinf_line)
    seen_urls = set()

    with open(SOURCE_FILE, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip('\n')
        stripped = line.strip()
        if stripped.startswith('#EXTINF'):
            extinf = line
            # 查找下一个非空行作为 URL
            j = i + 1
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines):
                url_line = lines[j].strip()
                if url_line.lower().startswith('http'):
                    url = url_line
                    if url not in seen_urls:
                        seen_urls.add(url)
                        entries.append((url, extinf))
                    # 跳到 j+1
                    i = j + 1
                    continue
            # 如果没有跟随 URL 则跳过这条 extinf
            i += 1
        elif stripped.lower().startswith('http'):
            url = stripped
            if url not in seen_urls:
                seen_urls.add(url)
                # 生成一个简单标题
                title = title_from_url(url)
                extinf = f'#EXTINF:-1,{title}'
                entries.append((url, extinf))
            i += 1
        else:
            i += 1

    if not entries:
        # 没有任何 URL，写空的 playlist 并退出
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
        print("No URLs found in source file, wrote empty playlist.")
        return

    valid_pairs = []  # list of (extinf, url)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_url, url): (url, extinf) for (url, extinf) in entries}
        for fut in as_completed(futures):
            url, extinf = futures[fut]
            try:
                res = fut.result()
                if res:
                    valid_pairs.append((extinf, url))
                    print("OK:", url)
                else:
                    print("Bad:", url)
            except Exception:
                print("Error checking:", url)

    # 写入输出文件，保留标题，按发现的顺序写入
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for extinf, url in valid_pairs:
            # 确保 extinf 是一行并且以换行结尾
            if not extinf.endswith('\n'):
                extinf = extinf + '\n'
            f.write(extinf)
            f.write(url + '\n')

    print("Done. Valid count:", len(valid_pairs))

if __name__ == '__main__':
    main()
