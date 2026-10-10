from pathlib import Path
from html.parser import HTMLParser
import json,re,subprocess
ROOT=Path(r'C:\Project Rocket\output\feiyun_double_breakout')
class Page(HTMLParser):
    def __init__(self):super().__init__();self.refs=[];self.ids=[];self.buttons=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if 'id' in a:self.ids.append(a['id'])
        for k in ['href','src']:
            if a.get(k):self.refs.append(a[k])
        if 'data-key' in a:self.buttons.append(a)
p=Page();body=(ROOT/'云聪视频理解报告.html').read_text(encoding='utf-8');p.feed(body)
assert len(p.ids)==len(set(p.ids))
for ref in p.refs:
    if ref.startswith('#'):assert ref[1:] in p.ids
    elif not ref.startswith(('http:','https:','file:')):assert (ROOT/ref).is_file(),ref
course_keys={'03','04','08','09','buypoints'}
for b in p.buttons:
    assert b['data-key'] in course_keys
    assert float(b['data-t'])<float(b['data-end'])
synthesis=json.loads((ROOT/'understanding.json').read_text(encoding='utf-8'))
assert [x['number'] for x in synthesis['patterns']]==list(range(1,14))
script=re.search(r'<script>(.*?)</script>',body,re.S).group(1)
js=Path(r'C:\Project Rocket\tmp\feiyun_report_check.js');js.write_text(script,encoding='utf-8')
r=subprocess.run([r'C:\Users\HarryWu_IOKI\AppData\Local\hermes\node\node.exe','--check',str(js)],capture_output=True,text=True,encoding='utf-8')
if r.returncode:print(r.stderr[:3000]);raise SystemExit(r.returncode)
print(json.dumps({'html_refs':len(p.refs),'evidence_buttons':len(p.buttons),'patterns':13,'script_syntax':'passed'},ensure_ascii=False))
