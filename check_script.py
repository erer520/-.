#!/usr/bin/env python3
# check_script_fast.py
# 按频道分组并发检查每组内的 URL，找到第一个可用就停止该组的其它检测

import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import threading

SOURCE_FILE = 'my_source.m3u'
OUTPUT_FILE = 'valid_sub_per_channel.m3u'

# 调整这些参数以兼顾速度与可靠性
WORKERS = 50        # 全局线程池总线程数（根据机器/带宽调整）
TIMEOUT = 6          # 单个请求超时（秒）
READ_BYTES = 512     # 读取少量字节判断内容
HEAD_FIRST = True    # 先试 HEAD（若服务支持可省流量）
RETRIES = 0          # urllib3 重试次数（重试会变慢）
STOP_PER_CHANNEL = 1 # 每个频道找到多少条就停止（通常为1）

# 全局 session（连接池）
session = requests.Session()
retries = Retry(total=RETRIES, backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504))
adapter = HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS * 2, max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)
session.headers.update({'User-Agent': 'Mozilla/5.0 (iptv-checker)'})

def is_m3u8_content_type(resp):
    ctype = resp.headers.get('Content-Type', '').lower()
    return ('mpegurl' in ctype) or ('.m3u8' in ctype) or ('application/vnd.apple.mpegurl' in ctype)

def check_url(url, stop_event):
    """
    检查单个 URL 是否是 m3u8/playlist（若 stop_event 已 set 会尽早返回 None）。
    返回 url（如果有效）或 None。
    """
    if stop_event.is_set():
        return None
    try:
        if HEAD_FIRST:
            try:
                r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
                if r.status_code == 200 and is_m3u8_content_type(r):
                    r.close()
                    return url
            except Exception:
                # HEAD 可能被屏蔽，继续做 GET
                pass

        if stop_event.is_set():
            return None

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

        # 只读少量内容判断
        chunk = b''
        try:
            for data in r.iter_content(chunk_size=READ_BYTES):
                if not data:
                    break
                chunk += data
                break
        finally:
            try:
                r.close()
            except Exception:
                pass

        if b'#EXTM3U' in chunk or b'#EXTINF' in chunk or b'.m3u8' in chunk:
            return url
    except Exception:
        return None
    return None

def parse_m3u(path):
    """
    将 m3u 文件解析成 [(info_line, [url,...]), ...] 的列表。
    如果某些 URL 在 info 行之前（孤立 URL），也会被作为无名频道处理。
    """
    groups = []
    current_info = None
    current_urls = []
    if not os.path.exists(path):
        return groups

    with open(path, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#EXTINF'):
                # 保存上一个组
                if current_info is not None or current_urls:
                    groups.append((current_info or '#EXTINF', current_urls))
                current_info = line
                current_urls = []
            elif line.lower().startswith('http'):
                current_urls.append(line)
            else:
                # 其他注释/行，忽略或可扩展解析 tvg-name 等
                pass
    # 收尾
    if current_info is not None or current_urls:
        groups.append((current_info or '#EXTINF', current_urls))
    return groups

def find_first_valid_for_group(info, urls, executor):
    """
    对单个频道组，提交该组所有 URL 到全局 executor 并并发等待结果，
    一旦找到第一个有效的就提前取消其它未来任务。
    返回找到的第一个有效 URL 或 None。
    """
    stop_event = threading.Event()
    futures = {executor.submit(check_url, u, stop_event): u for u in urls}
    found = None
    try:
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception:
                res = None
            if res:
                found = res
                stop_event.set()
                # 取消尚未开始的任务
                for f in futures:
                    if not f.done():
                        f.cancel()
                break
            # 若 stop_event 被外部设置，也跳出
            if stop_event.is_set():
                break
    finally:
        # 确保取消其他未完成任务
        for f in futures:
            if not f.done():
                f.cancel()
    return found

def main():
    groups = parse_m3u(SOURCE_FILE)
    if not groups:
        print("No groups/URLs found in", SOURCE_FILE)
        return

    results = []  # list of (info_line, found_url or None)
    total_groups = len(groups)
    print(f"Parsed {total_groups} groups. Starting checks with {WORKERS} workers...")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # 顺序处理每个频道，但每个频道内部并发尝试多个 URL
        for idx, (info, urls) in enumerate(groups, 1):
            if not urls:
                results.append((info, None))
                continue
            print(f"[{idx}/{total_groups}] Checking group: {info} ({len(urls)} urls)")
            found = find_first_valid_for_group(info, urls, ex)
            if found:
                print("  -> Found:", found)
            else:
                print("  -> None valid found in this group.")
            results.append((info, found))

    # 关闭 session
    try:
        session.close()
    except Exception:
        pass

    # 写出 m3u，只写找到的第一个有效 URL（如果有）
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for info, found in results:
            # 保持原来的 info 行（或者写默认）
            if info:
                f.write(f'{info}\n')
            if found:
                f.write(f'{found}\n')

    valid_count = sum(1 for _, u in results if u)
    print("Done. Valid groups:", valid_count, "/", total_groups)

if __name__ == '__main__':
    main()
