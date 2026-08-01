import urllib.request
import re
import json

req = urllib.request.Request('https://mypikpak.com/s/VOyoCZ2osf3R-5Zz-WdRahmoo2', headers={'User-Agent': 'Mozilla/5.0'})
try:
    html = urllib.request.urlopen(req).read().decode('utf-8')
    print("Length of HTML:", len(html))
    urls = re.findall(r'https?://[^\s\"\'<>]+', html)
    print("Found URLs:", [u for u in urls if 'mp4' in u or 'video' in u or 'file' in u])
except Exception as e:
    print(e)
