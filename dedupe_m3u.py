#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
name=dedupe_m3u.py
A small utility to deduplicate an M3U playlist.
- Default input: valid_sub_clean.m3u
- Default output: valid_sub_clean.dedup.m3u
- Deduplication fingerprint: normalized final URL (optionally following redirects)
- Preserves the first occurrence of a title+url pair and retains headers/comments.

Usage:
  python3 dedupe_m3u.py --in valid_sub_clean.m3u --out valid_sub_clean.dedup.m3u
  python3 dedupe_m3u.py --in valid_sub_clean.m3u --out valid_sub_clean.m3u --follow-redirects

Requires: requests (only if --follow-redirects is used). The script works without requests when not following redirects.
"""

from __future__ import annotations
import argparse
import hashlib
import os
import re
import sys
import tempfile
from urllib.parse import urlparse, urlunparse, unquote

try:
    import requests
except Exception:
    requests = None


def normalize_url(url: str) -> str:
    """Normalize a URL for fingerprinting: remove query/fragment, trailing slash, lowercase host/path."""
    if not url:
        return ""
    url = url.strip()
    # decode percent-encoding for consistency
    try:
        url = unquote(url)
    except Exception:
        pass
    p = urlparse(url)
    scheme = p.scheme.lower() if p.scheme else "http"
    netloc = p.netloc.lower()
    path = p.path.rstrip('/')
    clean = urlunparse((scheme, netloc, path, '', '', ''))
    return clean


def normalize_title(title: str) -> str:
    if not title:
        return ''
    t = title.strip().lower()
    # remove non-alphanumeric (but keep spaces)
    t = re.sub(r'[^0-9a-z\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def follow_final_url(url: str, timeout: float = 5.0) -> str:
    """Return the final URL after following redirects (uses requests). If requests not available, return original."""
    if not requests:
        return url
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        # some servers don't respond to HEAD; fall back to GET if status >=400
        if resp.status_code >= 400:
            resp = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
            resp.close()
        return resp.url or url
    except Exception:
        return url


def sha1_of(s: str) -> str:
    return hashlib.sha1(s.encode('utf-8')).hexdigest()


def dedupe(in_path: str, out_path: str, follow_redirects: bool = False, seen_file: str | None = None):
    if follow_redirects and not requests:
        print('Warning: requests library not available; --follow-redirects ignored', file=sys.stderr)
        follow_redirects = False

    seen_hashes: set[str] = set()
    if seen_file and os.path.exists(seen_file):
        with open(seen_file, 'r', encoding='utf-8') as sf:
            for line in sf:
                seen_hashes.add(line.strip())

    if not os.path.exists(in_path):
        raise SystemExit(f'Input file not found: {in_path}')

    out_lines: list[str] = []
    added_hashes: list[str] = []

    with open(in_path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = [ln.rstrip('\n') for ln in f]

    i = 0
    # preserve header/comments until first #EXTINF
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith('#EXTINF:'):
            break
        out_lines.append(line)
        i += 1

    # process entries (assumes pairs: #EXTINF then URL)
    while i < len(lines):
        line = lines[i]
        if not line.strip().startswith('#EXTINF:'):
            # preserve stray lines
            out_lines.append(line)
            i += 1
            continue
        title = ''
        if ',' in line:
            title = line.split(',', 1)[1].strip()
        url = ''
        if i + 1 < len(lines):
            url = lines[i+1].strip()
        else:
            i += 1
            continue

        final_url = url
        if follow_redirects and url:
            final_url = follow_final_url(url)

        norm_url = normalize_url(final_url)
        norm_title = normalize_title(title)
        fingerprint = sha1_of(norm_url)

        if fingerprint in seen_hashes:
            # duplicate detected: skip
            i += 2
            continue

        # keep this entry
        out_lines.append(line)
        out_lines.append(url)
        seen_hashes.add(fingerprint)
        added_hashes.append(fingerprint)

        i += 2

    # atomic write
    d = os.path.dirname(out_path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='tmp-dedupe-', dir=d)
    os.close(fd)
    with open(tmp_path, 'w', encoding='utf-8') as out_f:
        out_f.write('\n'.join(out_lines).rstrip('\n') + '\n')
    os.replace(tmp_path, out_path)

    if seen_file and added_hashes:
        with open(seen_file, 'a', encoding='utf-8') as sf:
            for h in added_hashes:
                sf.write(h + '\n')

    print(f'Wrote {out_path} ({len(added_hashes)} new entries)')


def main():
    p = argparse.ArgumentParser(description='Deduplicate M3U playlists (preserve first occurrence)')
    p.add_argument('--in', dest='in_path', default='valid_sub_clean.m3u', help='Input M3U file')
    p.add_argument('--out', dest='out_path', default='valid_sub_clean.dedup.m3u', help='Output (deduplicated) M3U file')
    p.add_argument('--follow-redirects', action='store_true', help='Follow redirects to canonicalize final URL (requires requests)')
    p.add_argument('--seen-file', dest='seen_file', default=None, help='Optional file to persist seen entry fingerprints between runs')
    args = p.parse_args()

    try:
        dedupe(args.in_path, args.out_path, follow_redirects=args.follow_redirects, seen_file=args.seen_file)
    except Exception as e:
        print('Error:', e, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
