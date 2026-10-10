async function(sid,date){
 const root="D:\\YuncongVideoAnalysis\\ledger_2026_v0.1\\text_priority_batch_20261005", dir=root+"\\"+date+"_"+sid;
 const shellQuote=s=>"'"+s.replace(/'/g,"''")+"'";
 const cmd=await tools.exec_command({cmd:"py -3.12 -c "+shellQuote(`import json,sys; sys.stdout.reconfigure(encoding="utf-8"); from pathlib import Path; d=Path(r"${dir}"); print(json.dumps({"status":json.loads((d/"status.json").read_text(encoding="utf-8")),"receipt":json.loads((d/"谷歌云盘上传记录.json").read_text(encoding="utf-8")) if (d/"谷歌云盘上传记录.json").exists() else None,"sizes":{p.name:p.stat().st_size for p in d.glob("*") if p.is_file()}},ensure_ascii=False))`),max_output_tokens:2500});
 if(cmd.exit_code!==0)throw new Error(cmd.output);
 const local=JSON.parse(cmd.output);if(local.status.text!=="complete")throw new Error("Text is not complete");
 let receipt=local.receipt||{stream_id:sid,files:[],verified:false};
 async function save(name,data){
  const content=JSON.stringify(data,null,2);
  const cmd="[System.IO.File]::WriteAllText("+shellQuote(dir+"\\"+name)+", @'\n"+content+"\n'@, [System.Text.UTF8Encoding]::new($false))";
  const r=await tools.exec_command({cmd,max_output_tokens:500});if(r.exit_code!==0)throw new Error(r.output);
 }

 if(!receipt.folder){
  const f=await tools.mcp__codex_apps__google_drive_create_folder({name:date+"_"+sid,parent_folder:"1g-iftms84Z0b5t4kHDSTOG9pzcJKP-UF"});
  if(!f.structuredContent?.success)throw new Error("Folder creation unconfirmed");
  receipt.folder=f.structuredContent;await save("谷歌云盘上传记录.json",receipt);
 }
 const plan=[
 ["研究文本_切分版.txt","01_研究文本_股票名校正版.txt","text/plain"],["学员个股快评.txt","02_学员个股快评_股票名校正版.txt","text/plain"],
 ["全文_带时间_股票名校正版.txt","03_全文_带时间_股票名校正版.txt","text/plain"],["全文_带时间.txt","04_全文_带时间_原始转写.txt","text/plain"],
 ["股票名称校正记录.csv","05_股票名称校正记录.csv","text/csv"],["切分索引.csv","06_切分索引.csv","text/csv"],["试跑结果.txt","07_文本处理结果.txt","text/plain"]];
 for(const [ln,rn,mime] of plan){
  if(receipt.files.some(x=>x.local_name===ln&&x.success))continue;
  const r=await tools.mcp__codex_apps__google_drive_upload_file({file_uri:dir+"\\"+ln,file_name:rn,mime_type:mime,parent_folder_id:receipt.folder.id});
  if(!r.structuredContent?.success)throw new Error("File upload unconfirmed: "+rn);
  receipt.files.push({local_name:ln,...r.structuredContent});await save("谷歌云盘上传记录.json",receipt);
 }
 const readback=await tools.mcp__codex_apps__google_drive_list_folder({url:receipt.folder.url,top_k:20});
 const files=readback.structuredContent?.files||[];
 for(const f of receipt.files){
  const got=files.find(x=>x.id===f.id);if(!got||got.title!==f.title||Number(got.size)!==local.sizes[f.local_name])throw new Error("Readback mismatch: "+f.title);
 }
 receipt.verified=true;receipt.verified_utc=new Date().toISOString();receipt.verification="Drive readback: file IDs, names and byte sizes match local delivered files.";
 await save("谷歌云盘上传记录.json",receipt);
 await save("status.json",{...local.status,upload:"complete",upload_completed_utc:receipt.verified_utc,cloud_receipt:dir+"\\谷歌云盘上传记录.json"});
 return {sid,folder:receipt.folder.url,files:receipt.files.length,verified:receipt.verified};
}
