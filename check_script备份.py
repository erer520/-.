#!/usr/bin/env python3
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

    urls = []
    with open(SOURCE_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.strip()
            if s.lower().startswith('http'):
                urls.append(s)

    valid = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_url, u): u for u in urls}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                res = fut.result()
                if res:
                    valid.append(res)
                    print("OK:", res)
            except Exception:
                pass

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for url in valid:
            f.write(f'#EXTINF:-1,Channel\n{url}\n')

    print("Done. Valid count:", len(valid))

if __name__ == '__main__':
    main()
