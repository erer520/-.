#!/usr/bin/env python3
# validate_m3u.py
# 并发验证 m3u8/sub 链接：requests + ThreadPoolExecutor + 连接池/重试
# 改动：支持找到第 N 条就停止（STOP_AFTER），减少超时与重试以加快整体检测

import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 输入/输出文件（保持与你原来的脚本一致）
SOURCE_FILE = 'my_source.m3u'
OUTPUT_FILE = 'valid_sub.m3u'

# 可调参数（为快速检测做了默认调整）
WORKERS = 100          # 并发线程数，根据机器和网络调整（可降到 40-100）
TIMEOUT = 6            # 单个请求超时（秒）——要快的话可以减小
READ_BYTES = 1024      # 每个响应读取的字节数用于判断
HEAD_FIRST = True      # 先尝试 HEAD 请求（节省流量）
RETRIES = 0            # 重试次数（urllib3 Retry），重试会拖慢总体速度
STOP_AFTER = 1         # 找到多少条就停止（1 表示找到第一条就停）

# 准备 Session（连接池 + 重试）
session = requests.Session()
retries = Retry(total=RETRIES, backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504))
# 增大连接池以匹配高并发
adapter = HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS * 2, max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({'User-Agent': 'Mozilla/5.0'})

def is_m3u8_content_type(resp):
    ctype = resp.headers.get('Content-Type', '').lower()
    return ('mpegurl' in ctype) or ('.m3u8' in ctype)

def check_url(url):
    try:
        if HEAD_FIRST:
            try:
                r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
                if r.status_code == 200 and is_m3u8_content_type(r):
                    r.close()
                    return url
            except Exception:
                pass

        r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code != 200:
            try:
                r.close()
            except Exception:
                pass
            return None
        if is_m3u8_content_type(r):
            r.close()
            return url

        # 只读取小段内容判断是否包含 m3u8 标记
        chunk = b''
        try:
            for data in r.iter_content(chunk_size=READ_BYTES):
                if not data:
                    break
                chunk += data
                break
        finally:
            r.close()

        if b'#EXTM3U' in chunk or b'#EXTINF' in chunk or b'.m3u8' in chunk:
            return url
    except Exception:
        return None
    return None

def main():
    if not os.path.exists(SOURCE_FILE):
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

    if not urls:
        print("No URLs found in source.")
        return

    valid = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_url, u): u for u in urls}
        try:
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    res = fut.result()
                    if res:
                        valid.append(res)
                        print("OK:", res)
                        # 达到阈值就提前退出循环
                        if len(valid) >= STOP_AFTER:
                            print(f"Reached {STOP_AFTER}, stopping early.")
                            break
                except Exception:
                    pass
        finally:
            # 取消其它还没开始的 future（注意：已经开始的线程无法被强制停止）
            for f in futures:
                if not f.done():
                    f.cancel()

    # 关闭 session（释放连接）
    try:
        session.close()
    except Exception:
        pass

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for url in valid:
            f.write(f'#EXTINF:-1,Channel\n{url}\n')

    print("Done. Valid count:", len(valid))

if __name__ == '__main__':
    main()
