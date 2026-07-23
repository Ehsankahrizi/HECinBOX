#!/usr/bin/env python3
"""Stamp a content version onto the shared asset links in every page.

Browsers cache /assets/site.css and /assets/site.js by URL. With a version
query that changes whenever the file changes, a new deploy serves a new URL, so
no browser can show a stale copy. The HTML itself is revalidated on every load
(Cache-Control: max-age=0, must-revalidate), so the fresh version reaches
everyone immediately.

Run before each deploy:  python3 stamp.py
"""
import hashlib, re, glob, os

root = os.path.join(os.path.dirname(__file__), "public")

def h(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()[:8]

css_v = h(os.path.join(root, "assets/site.css"))
js_v  = h(os.path.join(root, "assets/site.js"))

pages = glob.glob(os.path.join(root, "*.html")) + glob.glob(os.path.join(root, "*/index.html"))
changed = 0
for p in pages:
    s = open(p).read()
    before = s
    s = re.sub(r'href="/assets/site\.css(?:\?v=[0-9a-f]+)?"', f'href="/assets/site.css?v={css_v}"', s)
    s = re.sub(r'src="/assets/site\.js(?:\?v=[0-9a-f]+)?"',   f'src="/assets/site.js?v={js_v}"',   s)
    if s != before:
        open(p, "w").write(s); changed += 1

print(f"  css v={css_v}  js v={js_v}  stamped {changed} page(s)")
