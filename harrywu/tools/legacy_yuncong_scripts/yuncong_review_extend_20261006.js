function(sid,date,end,sections,names=[],pending=[],terms=[]){
 const dir="D:\\YuncongVideoAnalysis\\ledger_2026_v0.1\\text_priority_batch_20261005\\"+date+"_"+sid;
 const payload={end,sections,names,pending,terms};
 const py=`import json,sys;sys.stdout.reconfigure(encoding="utf-8");from pathlib import Path;p=Path(r"${dir}\\review.json");r=json.loads(p.read_text(encoding="utf-8"));a=json.loads(${JSON.stringify(JSON.stringify(payload).replace(/[^\x20-\x7E]/g,c=>"\\u"+c.charCodeAt(0).toString(16).padStart(4,"0")))});r["sections"]=[s for s in r["sections"] if s["type"]!="待完成审核"]+a["sections"];r["read_coverage_seconds"]=[0,a["end"]];r["names"]+=a["names"];r["pending"]+=a["pending"];r["terms"]+=a["terms"];r["sections"].append({"end":"END","keep":True,"type":"待完成审核","label":"尚未阅读的后续内容"});p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8");print(json.dumps({"sid":"${sid}","read_to":a["end"],"sections":len(r["sections"]),"names":len(r["names"])},ensure_ascii=False))`;
 return tools.exec_command({cmd:"py -3.12 -c '"+py.replace(/'/g,"''")+"'",max_output_tokens:500});
}
