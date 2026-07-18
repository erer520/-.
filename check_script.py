import requests
import os

source_file = 'my_source.m3u'
output_file = 'valid_sub.m3u'

if not os.path.exists(source_file):
    with open(output_file, 'w') as f:
        f.write('#EXTM3U\n')
    exit(0)

valid_urls = []
with open(source_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

for line in lines:
    line = line.strip()
    if line.startswith('http'):
        try:
            r = requests.get(line, timeout=5, headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
            if r.status_code == 200:
                chunk = next(r.iter_content(50))
                if b'#EXTM3U' in chunk or b'#EXTINF' in chunk:
                    valid_urls.append(line)
        except:
            pass

with open(output_file, 'w', encoding='utf-8') as f:
    f.write('#EXTM3U\n')
    for url in valid_urls:
        f.write(f'#EXTINF:-1,Channel\n{url}\n')
