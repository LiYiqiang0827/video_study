"""Build a self-contained local course interpretation and source-evidence reader."""
from pathlib import Path
import json, html, math

ROOT = Path(r'C:\Project Rocket\output\feiyun_double_breakout')
def read(p): return json.loads(p.read_text(encoding='utf-8'))
def stamp(s):
    s = int(s)
    return f'{s//3600:02}:{s%3600//60:02}:{s%60:02}' if s >= 3600 else f'{s//60:02}:{s%60:02}'
def esc(s): return html.escape(str(s))

def main():
    courses = []
    for key in ['03','04','08','09','buypoints']:
        notes = read(ROOT / f'notes_{key}.json')
        matches = [p for p in (ROOT/'platform_large_v3_v1').iterdir() if p.is_dir() and read(p/'manifest.json')['source'].endswith(notes['video'])]
        assert len(matches) == 1, notes['video']
        extraction = matches[0]
        manifest = read(extraction/'manifest.json')
        assert manifest['steps']['audio'] == manifest['steps']['evidence'] == 'complete'
        visual = ROOT/'review_frames_v1'/Path(notes['video']).stem
        frames = [{**r,'path':(visual/r['frame']).relative_to(ROOT).as_posix()} for r in read(visual/'frame_manifest.json')['frames']]
        for supplemental in (ROOT/'targeted_frames').glob('*') if (ROOT/'targeted_frames').exists() else []:
            fm = read(supplemental/'frame_manifest.json')
            if fm['params']['source_sha256'] == manifest['source_sha256']:
                frames += [{**r,'path':(supplemental/r['frame']).relative_to(ROOT).as_posix()} for r in fm['frames']]
        courses.append({**notes,'key':key,'duration':manifest['duration'], 'source':Path(manifest['source']).as_uri(),'source_path':manifest['source'],
            'extraction':extraction.relative_to(ROOT).as_posix(), 'frames':sorted(frames,key=lambda r:r['time']),
            'speech':read(extraction/'speech.json')})
    synthesis=read(ROOT/'understanding.json')
    validation=read(ROOT/'acceptance.json')
    assert validation['passed'], 'Evidence validation must pass first'
    styles='''
    :root{--ink:#15283b;--blue:#145da0;--muted:#526477;--line:#dbe3ea;--paper:#fff;--bg:#f3f6f9;--gold:#8b5d0a}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--bg);font:16px/1.85 "Microsoft YaHei",system-ui,sans-serif}
    main{max-width:1120px;margin:auto;padding:36px 24px 80px}header{padding:30px 0 12px}h1{font-size:clamp(29px,4vw,43px);line-height:1.35;letter-spacing:-.8px;margin:12px 0}h2{font-size:26px;margin:0 0 18px}h3{font-size:20px;margin:0 0 10px}p{margin:10px 0}a{color:var(--blue)}.eyebrow{font-size:13px;letter-spacing:2px;color:var(--blue);font-weight:700}.lead{font-size:20px;max-width:940px}.muted,small{color:var(--muted)}
    nav{display:flex;flex-wrap:wrap;gap:9px;margin:24px 0}nav a,.pill{border:1px solid var(--line);background:white;padding:5px 13px;border-radius:100px;text-decoration:none;font-size:14px}.stats{display:flex;gap:15px;flex-wrap:wrap;margin:20px 0}.stats strong{font-size:24px;display:block}.stats div{min-width:150px;padding:15px 20px;background:white;border-radius:12px;border:1px solid var(--line)}
    section{margin:38px 0}.card{padding:25px;background:var(--paper);border:1px solid var(--line);border-radius:14px;margin:16px 0}.accent{border-left:4px solid var(--blue)}.caution{border-left:4px solid var(--gold)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.grid .card{margin:0}.flow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.flow div{padding:16px;border:1px solid var(--line);border-radius:10px;background:white}.flow b{display:block;color:var(--blue)}
    button{font:inherit;cursor:pointer;border:1px solid var(--line);border-radius:6px;background:#f2f7fc;color:var(--blue);padding:3px 9px}button:hover{background:#dcecfb}.refs{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.refs button{font-size:13px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;padding:15px;border:1px solid var(--line);vertical-align:top}th{background:#eaf1f7}td:first-child{font-weight:700;min-width:110px}td{min-width:170px;font-size:14px}li{margin:7px 0}details{background:white;border:1px solid var(--line);border-radius:10px;margin:12px 0;padding:16px 20px}summary{cursor:pointer;font-size:18px;font-weight:700}.section-row{border-top:1px solid var(--line);padding:20px 0}.section-row:first-of-type{margin-top:16px}figure{margin:18px 0}figure img{width:100%;border-radius:8px;border:1px solid var(--line)}figcaption{font-size:13px;color:var(--muted)}
    dialog{max-width:1220px;width:94vw;max-height:92vh;border:1px solid var(--line);border-radius:14px;padding:24px;box-shadow:0 10px 90px #0004}dialog::backdrop{background:#15283b99}.dialog-head{display:flex;justify-content:space-between;gap:15px;align-items:center;position:sticky;top:-24px;background:white;padding:12px 0;z-index:1}.evidence-grid{display:grid;grid-template-columns:1.15fr 1fr;gap:22px}.evidence-grid img{width:100%}.transcript{max-height:64vh;overflow:auto}.utterance{padding:7px;border-bottom:1px solid var(--line)}.utterance time{font-size:12px;color:var(--blue);display:block}.match{background:#fff3ce}input,select{font:inherit;padding:10px;border:1px solid var(--line);border-radius:7px;max-width:100%}input{width:100%}.search-result{padding:12px;border-bottom:1px solid var(--line)}.search-result button{font-size:13px}footer{border-top:1px solid var(--line);padding-top:20px;font-size:13px;color:var(--muted)}
    @media(max-width:760px){main{padding:20px 16px 50px}.grid,.flow,.evidence-grid{grid-template-columns:1fr}.card{padding:18px}.transcript{max-height:45vh}dialog{padding:15px}h2{font-size:23px}}@media print{nav,button,#search,dialog{display:none}body{background:white}details{break-inside:avoid}main{padding:0}.card{break-inside:avoid}a{color:inherit}}
    '''
    def refs(items):
        return '<div class="refs">'+''.join(f'<button data-key="{esc(r[0])}" data-t="{r[1]}" data-end="{r[2]}">{esc(r[3])} · {stamp(r[1])}–{stamp(r[2])}</button>' for r in items)+'</div>'
    out=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>云聪视频理解报告</title><style>'+styles+'</style></head><body><main>']
    out.append('<header><div class="eyebrow">飞云双突破 · 五段课程交叉阅读</div><h1>云聪究竟在教什么</h1><p class="lead">'+esc(synthesis['thesis'])+'</p><p class="muted">'+esc(synthesis['scope'])+'</p>')
    total=sum(c['duration'] for c in courses)
    out.append(f'<div class="stats"><div><strong>5 段</strong>完整音轨处理与逐段阅读</div><div><strong>{stamp(total)}</strong>原始课程总时长</div><div><strong>{sum(len(c["frames"]) for c in courses)} 张</strong>已核对的抽样与定点画面</div><div><strong>13 类</strong>逐项还原买入信号</div></div></header>')
    out.append('<nav>'+''.join(f'<a href="#{k}">{v}</a>' for k,v in [('framework','体系主线'),('patterns','13 类买点'),('distinctions','容易读错的地方'),('courses','逐课详解'),('unresolved','尚未明确'),('search','原转写检索'),('method','处理与验收')])+'</nav>')
    out.append('<section id="framework"><h2>一套决策顺序</h2><p>下面是把五段内容串联后的理解模型；顺序是本报告的整理，不是视频中的逐字口诀。</p><div class="flow">')
    for i,item in enumerate(synthesis['flow'],1): out.append(f'<div><b>{i:02} · {esc(item["title"])}</b>{esc(item["text"])}</div>')
    out.append('</div>')
    for item in synthesis['core']: out.append('<article class="card accent"><h3>'+esc(item['title'])+'</h3><p>'+esc(item['text'])+'</p>'+refs(item['refs'])+'</article>')
    out.append('</section><section id="patterns"><h2>13 类买点：前提、触发和边界</h2><p>'+esc(synthesis['pattern_note'])+'</p><div class="table-wrap"><table><thead><tr><th>视频编号 / 名称</th><th>出现的背景</th><th>他要求观察的触发</th><th>容易漏掉的条件</th></tr></thead><tbody>')
    for p in synthesis['patterns']:
        out.append('<tr><td>'+esc(str(p['number'])+' · '+p['name'])+refs(p['refs'])+'</td><td>'+esc(p['context'])+'</td><td>'+esc(p['trigger'])+'</td><td>'+esc(p['boundary'])+'</td></tr>')
    out.append('</tbody></table></div></section><section id="distinctions"><h2>这些区别决定是否真的理解</h2><div class="grid">')
    for item in synthesis['distinctions']: out.append('<article class="card"><h3>'+esc(item['title'])+'</h3><p>'+esc(item['text'])+'</p>'+refs(item['refs'])+'</article>')
    out.append('</div></section><section id="courses"><h2>逐课详解与图证</h2><p>展开任意课程。时间按钮打开该段自动转写和附近的实际画面；原转写保留识别错误，正文按板书和上下文作了归一。</p>')
    for c in courses:
        out.append('<details><summary>'+esc(c['video'])+' <small>'+stamp(c['duration'])+'</small></summary><p class="muted">'+esc(c['review'])+'</p>')
        for sec in c['sections']:
            out.append('<article class="section-row"><h3>'+esc(sec['title'])+'</h3><ul>'+''.join('<li>'+esc(s)+'</li>' for s in sec['points'])+'</ul>'+refs([[c['key'],sec['start'],sec['end'],Path(c['video']).stem]])+'</article>')
        for f in c.get('frame_refs',[])[:4]: out.append(f'<figure><a href="{esc(f)}"><img loading="lazy" src="{esc(f)}"></a><figcaption>原视频画面，点击放大。精确时间可在上方证据查看器核对。</figcaption></figure>')
        out.append('<h3>本课仍需保留的边界</h3><ul>'+''.join('<li>'+esc(x)+'</li>' for x in c['open_points'])+'</ul></details>')
    out.append('</section><section id="unresolved"><h2>理解已经建立，哪些细节仍不能替他补写</h2>')
    for item in synthesis['unresolved']: out.append('<article class="card caution"><h3>'+esc(item['title'])+'</h3><p>'+esc(item['text'])+'</p>'+refs(item.get('refs',[]))+'</article>')
    out.append('</section><section id="search"><h2>查原转写</h2><p class="muted">搜索五段课程的机器转写；例如“止损”“回抽”“必须”。“沽压”在原转写中也可能写成“估压”“孤鸭”。检索结果不等于已核准原话。</p><input id="q" aria-label="搜索原转写" placeholder="输入至少两个字"><div id="results"></div></section>')
    out.append('<section id="method"><h2>处理与验收</h2><div class="card"><ul>'+''.join('<li>'+esc(x)+'</li>' for x in synthesis['method'])+'</ul><p>证据验收：<a href="acceptance.json">校验记录</a> · 可继续使用的理解：<a href="understanding.json">结构化整理</a></p><p class="muted">机器识别分段置信指标不是语义准确率；抽帧覆盖也不等于逐帧观看。此报告没有对课程所称胜率或盈利能力作回测。</p></div></section>')
    out.append('<footer>2026-09-30 · 本地处理。原媒体与原始转写保留；理解层与证据层分别保存。全文为用户提供课程的解释与整理。</footer></main>')
    out.append('<dialog id="viewer"><div class="dialog-head"><strong id="vtitle"></strong><button id="close">关闭</button></div><p id="vmeta" class="muted"></p><div class="refs"><button id="prev">前 30 秒</button><button id="next">后 30 秒</button></div><details><summary>原视频文件位置</summary><p id="source" style="overflow-wrap:anywhere;font-size:13px"></p></details><div class="evidence-grid"><figure><a id="fullframe"><img id="frame"></a><figcaption id="fcaption"></figcaption></figure><div id="speech" class="transcript"></div></div></dialog>')
    payload=json.dumps(courses,ensure_ascii=False).replace('<','\\u003c')
    out.append('<script>const courses='+payload+';'+r'''
    const $=s=>document.querySelector(s), esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const time=s=>{s=Math.floor(s);return [Math.floor(s/3600),Math.floor(s%3600/60),s%60].map(x=>String(x).padStart(2,'0')).join(':')};
    let active=null;
    function view(key,t,end){
      const c=courses.find(x=>x.key===key);t=Math.max(0,Math.min(t,c.duration));end=Math.max(t+1,Math.min(end||t+45,c.duration));active={key,t,end};
      $('#vtitle').textContent=c.video+' · '+time(t)+'–'+time(end);
      $('#vmeta').textContent='右侧为机器转写候选；左侧是最接近起始时间的已提取画面，并非整个区间的视频。';
      $('#source').textContent=c.source_path+'（在播放器中定位到 '+time(t)+'）';
      const f=c.frames.reduce((a,b)=>Math.abs(a.time-t)<=Math.abs(b.time-t)?a:b);
      $('#frame').src=f.path;$('#fullframe').href=f.path;$('#fcaption').textContent='画面实际时间 '+time(f.time)+'（'+f.time.toFixed(3)+' 秒），原分辨率。';
      $('#speech').innerHTML=c.speech.filter(s=>s.end>=t-3&&s.start<=end+3).map(s=>'<div class="utterance"><time>'+time(s.start)+'–'+time(s.end)+(s.review?' · 模型标记待核对':'')+'</time>'+esc(s.text)+'</div>').join('');
      $('#speech').scrollTop=0;if(!$('#viewer').open)$('#viewer').showModal();
    }
    function bindEvidence(){document.querySelectorAll('button[data-key]').forEach(b=>b.onclick=()=>view(b.dataset.key,+b.dataset.t,+b.dataset.end))}bindEvidence();
    $('#close').onclick=()=>$('#viewer').close();$('#prev').onclick=()=>view(active.key,active.t-30,active.t+15);$('#next').onclick=()=>view(active.key,active.t+30,active.t+75);
    $('#q').oninput=e=>{const q=e.target.value.trim();if(q.length<2){$('#results').innerHTML='';return}let rows=[];for(const c of courses)for(const s of c.speech)if(s.text.includes(q))rows.push('<div class="search-result"><button data-key="'+esc(c.key)+'" data-t="'+s.start+'" data-end="'+(s.end+20)+'">'+esc(c.video)+' · '+time(s.start)+'</button><p>'+esc(s.text)+'</p></div>');$('#results').innerHTML='<p>'+rows.length+' 条匹配'+(rows.length>120?'，显示前 120 条':'')+'</p>'+rows.slice(0,120).join('');bindEvidence()};
    </script></body></html>''')
    target=ROOT/'云聪视频理解报告.html'
    target.write_text(''.join(out),encoding='utf-8')
    print(target)

if __name__=='__main__': main()
