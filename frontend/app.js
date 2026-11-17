const $ = (selector, root = document) => root.querySelector(selector);
const labels = {image:'图文图片',video:'视频',cover:'封面'};
const state = {base:'',key:'',connected:false,busy:false,parseId:null,posts:[],cards:new Map(),downloadBusy:false,stop:false,controller:null,fileLimit:157286400,batchLimit:419430400};
const terminal = new Set(['completed','partial','failed','cancelled']);
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
const bytes = n => n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(2)} MB`;
function el(tag, className, text) {const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function message(text,error=false){const node=$('#notice');node.textContent=text;node.hidden=!text;node.classList.toggle('error',error);}
function updateButtons(){ $('#parseButton').disabled=!state.connected || state.busy || state.downloadBusy || !$('#shareInput').value.trim();$('#batchDownload').disabled=!state.connected || !state.posts.length || state.busy || state.downloadBusy;$('#clearButton').disabled=state.busy || state.downloadBusy; }
function connection(online){state.connected=online;$('#connectionDot').classList.toggle('online',online);$('#connectionText').textContent=online?'后端已连接':'未连接后端';$('#connectionHint').textContent=online?'支持批量解析，直链与文件以源站实际提供为准。':'先连接你的 Python 后端，即可开始解析。';updateButtons();}
function validBase(value){const u=new URL(value);if(u.username || u.password || u.search || u.hash || u.pathname!=='/')throw Error('请填写不含账号、路径或参数的后端根地址');if(u.protocol!=='https:' && !(u.protocol==='http:' && ['localhost','127.0.0.1','[::1]'].includes(u.hostname)))throw Error('公网后端必须使用 HTTPS');return u.origin;}
async function api(path,options={},timeout=20000){
  if(!state.base)throw Error('尚未配置后端地址');
  const targetBase=state.base;
  const headers={...options.headers};if(state.key)headers.Authorization=`Bearer ${state.key}`;
  if(options.body)headers['Content-Type']='application/json';
  let response;
  try{response=await fetch(targetBase+path,{...options,headers,signal:AbortSignal.timeout(timeout),cache:'no-store',referrerPolicy:'no-referrer'});}
  catch{
    if(state.base===targetBase)connection(false);
    throw Error('无法连接后端。请检查地址、HTTPS、网络和 CORS；免费实例唤醒可能需要约一分钟。');
  }
  let data;try{data=await response.json();}catch{throw Error('后端未返回有效 API 数据，可能尚在唤醒或地址不正确。');}
  if(!response.ok){
    const err = Error(typeof data.detail==='string'?data.detail:`请求失败（HTTP ${response.status}）`);
    err.status = response.status;
    err.retryAfter = Number(response.headers.get('retry-after')) || 0;
    throw err;
  }
  return data;
}
function safeSource(url){try{const u=new URL(url);return u.protocol==='https:' && !u.username && !u.password?u.href:null;}catch{return null;}}
async function copy(text){if(!text)throw Error('页面没有提供可复制的内容');try{await navigator.clipboard.writeText(text);message('已复制到剪贴板。');}catch{throw Error('浏览器未允许剪贴板访问，请手动选择并复制文案或直链。');}}
function action(text,handler,className='text-button'){const button=el('button',className,text);button.type='button';button.addEventListener('click',async()=>{button.disabled=true;try{await handler();}catch(error){message(error.message,true);}finally{button.disabled=false;}});return button;}
function sourceLink(url,text){const a=el('a','',text);const safe=safeSource(url);if(safe)a.href=safe;a.target='_blank';a.rel='noopener noreferrer';a.referrerPolicy='no-referrer';return a;}
function saveBlob(data,name,type){const url=URL.createObjectURL(new Blob([data],{type}));const a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);message('已交给浏览器保存，请在下载管理器中确认文件。');}
function selected(post){const card=state.cards.get(post.id);return post.assets.filter(asset=>$(`input[data-asset="${asset.id}"]`,card)?.checked).map(asset=>({post_id:post.id,asset_id:asset.id}));}
function addPost(post){
  if(state.cards.has(post.id))return;
  state.posts.push(post);const card=el('article','post');state.cards.set(post.id,card);card.dataset.postId=post.id;
  const head=el('div','post-head'),author=el('div','post-author');
  author.append(el('span','avatar',(post.author||'作').slice(0,1)),el('span','',post.author||'页面未提供作者'),el('span','type-label',post.assets.some(a=>a.kind==='image')?'图文':'视频'));
  head.append(author,el('h3','',post.title||post.description?.split('\n')[0].slice(0,85)||'页面未提供标题'));
  const canonical=sourceLink(post.source_url,`作品 ${post.id} ↗`);canonical.className='source-link';head.append(canonical);card.append(head);
  const preview=el('div','preview'),video=post.assets.find(a=>a.kind==='video'),cover=post.assets.find(a=>a.kind==='cover');
  const previewError=()=>{if(!$('.preview-error',preview))preview.append(el('p','preview-error','源站暂不允许此预览。可重新解析，或尝试下方流式保存。'));};
  if(video){const player=el('video');player.controls=true;player.preload='none';player.playsInline=true;player.src=safeSource(video.url)||'';if(cover)player.poster=safeSource(cover.url)||'';player.addEventListener('error',previewError);preview.append(player);}
  const pictures=post.assets.filter(a=>a.kind==='image');
  if(pictures.length){const gallery=el('div','gallery');const addImage=asset=>{const a=sourceLink(asset.url,'');const image=el('img');image.src=safeSource(asset.url)||'';image.alt=`${post.author||'作品'} · ${asset.label}`;image.loading='lazy';image.referrerPolicy='no-referrer';image.addEventListener('error',previewError,{once:true});a.append(image);gallery.append(a);};pictures.slice(0,6).forEach(addImage);preview.append(gallery);if(pictures.length>6){const more=action(`展开全部 ${pictures.length} 张图片 ↓`,()=>{pictures.slice(6).forEach(addImage);more.remove();});more.classList.add('gallery-all');preview.append(more);}}
  card.append(preview,el('p','caption',post.description||'页面未提供文案'));
  const groups=el('div','post-options');
  for(const kind of ['image','video','cover']){const assets=post.assets.filter(a=>a.kind===kind);if(!assets.length)continue;const label=el('label'),input=el('input');input.type='checkbox';input.checked=$(`[data-kind="${kind}"]`).checked;input.dataset.group=kind;label.append(input,document.createTextNode(`${labels[kind]}${kind==='image'?` · ${assets.length} 张`:''}`));input.addEventListener('change',()=>assets.forEach(a=>$(`input[data-asset="${a.id}"]`,card).checked=input.checked));groups.append(label);}card.append(groups);
  const details=el('details','asset-list');details.append(el('summary','',`源直链与单文件保存 · ${post.assets.length} 个资源`));
  for(const asset of post.assets){const row=el('div','asset-row'),label=el('label'),input=el('input');input.type='checkbox';input.dataset.asset=asset.id;input.dataset.kind=asset.kind;input.checked=$(`[data-kind="${asset.kind}"]`).checked;label.append(input,document.createTextNode(asset.kind==='image'?`图片 ${Number(asset.label.split('_')[1])}`:labels[asset.kind]));row.append(label,sourceLink(asset.url,'打开源文件 ↗'),action('复制直链',()=>copy(asset.url)),action('保存到本机',()=>download([post],[{post_id:post.id,asset_id:asset.id}],[]),'text-button save-asset'));details.append(row);}card.append(details);
  const footer=el('div','post-footer');footer.append(action('复制文案',()=>copy([...new Set([post.title,post.description].filter(Boolean))].join('\n'))),action('保存元数据',async()=>{const data=await api(`/api/jobs/${encodeURIComponent(state.parseId)}/posts/${encodeURIComponent(post.id)}/metadata`);saveBlob(JSON.stringify(data,null,2),`${post.id}-metadata.json`,'application/json');}),action(pictures.length?'保存本篇图片 ↓':'保存视频 ↓',()=>download([post],selected(post),$('#metadataOption').checked?[post.id]:[]),'primary'));
  card.append(footer,el('p','warning',post.warning));$('#results').append(card);$('#emptyState').hidden=true;$('#resultCount').textContent=String(state.posts.length);updateButtons();
}
async function download(posts,assets,metadataIds=[]){
  if(!state.connected)throw Error('后端未连接，不能开始文件下载');
  if(state.downloadBusy)throw Error('正在保存上一批文件，请等待或停止当前批次');
  if(!assets.length && !metadataIds.length)throw Error('请先选择要保存的内容');
  if(assets.length>200)throw Error('每批最多保存 200 个文件，请分批选择');
  state.downloadBusy=true;state.stop=false;updateButtons();
  $('#transfersSection').hidden=false;$('#stopDownload').hidden=false;
  let total=0,handedOff=0,errors=0;
  const parseId=state.parseId;
  message('每个文件将分别保存。若浏览器询问是否允许下载多个文件，请按需要允许；最终位置请查看下载管理器。');
  try{
    for(const selection of assets){
      if(state.stop)break;
      const post=posts.find(p=>p.id===selection.post_id),asset=post.assets.find(a=>a.id===selection.asset_id);
      const row=el('div','transfer'),head=el('div','transfer-head'),status=el('strong','','正在连接源文件'),bar=el('progress'),meta=el('p','transfer-meta');
      head.append(el('span','',`${post.author||post.id} · ${asset.kind==='image'?asset.label:labels[asset.kind]}`),status);
      bar.max=1;bar.removeAttribute('value');bar.setAttribute('aria-label','单文件下载进度');row.append(head,bar,meta);$('#transfers').prepend(row);

      // 原图优先尝试浏览器直链直连，规避服务端带宽消耗（实测图片支持 CORS，视频跨域拒绝则走兼容中继）
      let handledDirectly = false;
      if (asset.kind === 'image' && safeSource(asset.url)) {
        status.textContent = '正在直连源站获取…';
        const fileController = new AbortController();
        state.controller = fileController;
        let directTimedOut = false;
        const directTimer = setTimeout(() => { directTimedOut = true; try { fileController.abort(); } catch (_) {} }, 20000);
        try {
          const directResp = await fetch(asset.url, {
            mode: 'cors',
            credentials: 'omit',
            referrerPolicy: 'no-referrer',
            signal: fileController.signal
          });
          if (!directResp.ok) throw Error(`源站返回 HTTP ${directResp.status}`);

          const expected = Number(directResp.headers.get('content-length')) || 0;
          if (expected > state.fileLimit || total + expected > state.batchLimit) {
            try { fileController.abort(); } catch (_) {}
            throw Error('文件或本批总量超过安全限制，请减小选择范围');
          }

          const reader = directResp.body.getReader(), chunks = [];
          let received = 0;
          while (true) {
            if (state.stop) { try { fileController.abort(); } catch (_) {} break; }
            const {done, value} = await reader.read();
            if (done) break;
            received += value.byteLength;
            total += value.byteLength;
            if (received > state.fileLimit || total > state.batchLimit) {
              try { fileController.abort(); } catch (_) {}
              throw Error('文件或本批总量超过安全限制');
            }
            chunks.push(value);
            meta.textContent = `本文件 ${bytes(received)}${expected ? ` / ${bytes(expected)}` : ''} · 本批已接收 ${bytes(total)}`;
            if (expected) bar.value = Math.min(received / expected, 1);
          }

          if (state.stop) throw Error('已停止下载');
          if (expected && received !== expected) throw Error('源文件传输不完整');

          const firstChunk = chunks[0] || new Uint8Array(0);
          const isWebp = firstChunk.length >= 12 && firstChunk[0]===0x52 && firstChunk[1]===0x49 && firstChunk[2]===0x46 && firstChunk[3]===0x46 && firstChunk[8]===0x57 && firstChunk[9]===0x45 && firstChunk[10]===0x42 && firstChunk[11]===0x50;
          const isJpg = firstChunk.length >= 3 && firstChunk[0]===0xff && firstChunk[1]===0xd8 && firstChunk[2]===0xff;
          const isPng = firstChunk.length >= 8 && firstChunk[0]===0x89 && firstChunk[1]===0x50 && firstChunk[2]===0x4e && firstChunk[3]===0x47;
          if (!isWebp && !isJpg && !isPng) {
            throw Error('源站未返回受支持的图片格式，可能需要验证或页面已变更');
          }

          const ext = isWebp ? 'webp' : isJpg ? 'jpg' : 'png';
          const mime = isWebp ? 'image/webp' : isJpg ? 'image/jpeg' : 'image/png';
          const filename = `${post.id}_${asset.label}.${ext}`;

          const url = URL.createObjectURL(new Blob(chunks, {type: mime}));
          const a = el('a'); a.href = url; a.download = filename;
          document.body.append(a); a.click(); a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 5000);

          handedOff++; bar.value = 1;
          status.textContent = '已交给浏览器保存（源站直连）';
          handledDirectly = true;
          for (let w = 0; w < 10; w++) {
            if (state.stop) break;
            await sleep(100);
          }
        } catch (e) {
          if (directTimedOut) {
            errors++; row.classList.add('error'); status.textContent = '下载失败';
            meta.textContent = '直连下载超时（超过 20 秒），已中止。'; bar.value = 0;
            break;
          }
          if (state.stop || e.message?.includes('安全限制') || e.message?.includes('已停止')) {
            errors++; row.classList.add('error'); status.textContent = state.stop ? '已停止' : '下载失败';
            meta.textContent = state.stop ? '本文件已取消，后续文件未开始。' : e.message; bar.value = 0;
            break;
          }
          // 仅在 CORS 限制或网络直连受阻时，平滑降级（fallback）为兼容流式中继保存
          handledDirectly = false;
        } finally {
          clearTimeout(directTimer);
          try { fileController.abort(); } catch (_) {}
          if (state.controller === fileController) state.controller = null;
        }
      }
      if (handledDirectly) continue;
      if (state.stop) break;

      let jobId=null;
      try{
        let task = null;
        for (let attempt = 0; attempt < 4; attempt++) {
          if (state.stop) break;
          try {
            task = await api('/api/downloads', {
              method: 'POST',
              body: JSON.stringify({parse_job_id: parseId, post_id: post.id, asset_id: asset.id})
            });
            break;
          } catch (err) {
            if (err.status === 429 && attempt < 3 && !state.stop) {
              const waitSec = Math.max(1, Math.min(err.retryAfter || 2, 60));
              status.textContent = `达到速率限制，等待重试 (${waitSec}秒)…`;
              for (let s = 0; s < waitSec * 10; s++) {
                if (state.stop) break;
                await sleep(100);
              }
              if (state.stop) break;
              continue;
            }
            throw err;
          }
        }
        if (state.stop) {
          if (task && task.id) {
            try { await api(`/api/jobs/${encodeURIComponent(task.id)}`, {method: 'DELETE'}); } catch {}
          }
          break;
        }
        if (!task) throw Error('创建下载任务失败');

        jobId=task.id;state.controller=new AbortController();
        const response=await fetch(state.base+task.download_path,{signal:state.controller.signal,cache:'no-store',referrerPolicy:'no-referrer'});
        if(!response.ok){let reason;try{reason=(await response.json()).detail;}catch{}throw Error(reason||`源文件请求失败（HTTP ${response.status}）`);}
        const expected=Number(response.headers.get('content-length'))||0;
        if(expected>state.fileLimit || total+expected>state.batchLimit){await response.body.cancel();throw Error('文件或本批总量超过安全限制，请减小选择范围');}
        const disposition=response.headers.get('content-disposition')||'';
        const filename=/filename="([^"]+)"/.exec(disposition)?.[1]||`${post.id}_${asset.label}`;
        const reader=response.body.getReader(),chunks=[];let received=0;
        status.textContent='正在接收原始文件';
        while(true){
          if(state.stop) { state.controller.abort(); break; }
          const {done,value}=await reader.read();if(done)break;
          received+=value.byteLength;total+=value.byteLength;
          if(received>state.fileLimit || total>state.batchLimit){state.controller.abort();throw Error('文件或本批总量超过安全限制');}
          chunks.push(value);meta.textContent=`本文件 ${bytes(received)}${expected?` / ${bytes(expected)}`:''} · 本批已接收 ${bytes(total)}`;
          if(expected)bar.value=Math.min(received/expected,1);
        }
        if(state.stop) throw Error('已停止下载');
        if(expected && received!==expected)throw Error('源文件传输不完整，请重试');
        const progress=await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        if(progress.status!=='completed')throw Error(progress.error||'传输尚未完成，文件未交给浏览器保存');
        const url=URL.createObjectURL(new Blob(chunks,{type:response.headers.get('content-type')||'application/octet-stream'}));
        const a=el('a');a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
        handedOff++;bar.value=1;status.textContent='已交给浏览器保存';
        // The browser owns final placement; this does not confirm a filesystem write.
        for (let w = 0; w < 10; w++) {
          if (state.stop) break;
          await sleep(100);
        }
      }catch(error){errors++;row.classList.add('error');status.textContent=state.stop?'已停止':'下载失败';meta.textContent=state.stop?'本文件已取消，后续文件未开始。':error.message;bar.value=0;}
      finally{state.controller=null;if(jobId)try{await api(`/api/jobs/${encodeURIComponent(jobId)}`,{method:'DELETE'});}catch{}}
    }
    if(!state.stop){
      for(const post of posts.filter(p=>metadataIds.includes(p.id))){
        if(state.stop) break;
        const data=await api(`/api/jobs/${encodeURIComponent(parseId)}/posts/${encodeURIComponent(post.id)}/metadata`);
        saveBlob(JSON.stringify(data,null,2),`${post.id}-metadata.json`,'application/json');
      }
    }
    message(`${state.stop?'本批已停止':'本批处理结束'}：${handedOff} 个媒体文件已交给浏览器，${errors} 个未完成${state.stop?`，${Math.max(0,assets.length-handedOff-errors)} 个尚未开始`:''}。请在下载管理器确认保存结果。`,errors>0);
  }finally{state.downloadBusy=false;state.controller=null;$('#stopDownload').hidden=true;updateButtons();}
}

let currentParseToken = 0;
async function parse(){
  state.busy=true;state.parseId=null;state.cancelRequested=false;
  const parseToken = ++currentParseToken;
  updateButtons();message('');$('#parseErrors').replaceChildren();state.posts=[];state.cards.clear();$('#results').replaceChildren();$('#resultCount').textContent='0';$('#emptyState').hidden=true;$('#parseStatus').hidden=false;$('#cancelParse').disabled=false;
  $('#parseState').textContent='正在提交解析…';
  try{
    const task=await api('/api/parse',{method:'POST',body:JSON.stringify({text:$('#shareInput').value})});
    if(parseToken!==currentParseToken) return;
    state.parseId=task.id;
    if(state.cancelRequested){
      try{
        await api(`/api/jobs/${encodeURIComponent(task.id)}`,{method:'DELETE'});
        $('#parseState').textContent='解析已取消';message('解析已取消。');
      }catch(err){
        $('#parseState').textContent='已停止等待';message('已停止等待解析，但后端任务取消未能确认，请稍后检查。',true);
      }
      return;
    }
    for(let i=0;i<1800;i++){
      if(parseToken!==currentParseToken || state.cancelRequested) return;
      const job=await api(`/api/jobs/${encodeURIComponent(task.id)}`);
      if(parseToken!==currentParseToken || state.cancelRequested) return;
      $('#parseState').textContent={queued:'等待解析',running:'正在解析',completed:'解析完成',partial:'部分解析成功',failed:'解析失败',cancelled:'解析已取消'}[job.status]||job.status;
      $('#parseCount').textContent=`${job.completed} / ${job.total}`;$('#parseProgress').max=job.total;$('#parseProgress').value=job.completed;
      for(const post of job.items||[])addPost(post);
      if(terminal.has(job.status)){
        for(const error of job.errors||[])$('#parseErrors').append(el('p','',`第 ${error.index} 条：${error.reason}`));
        $('#resultsSubtitle').textContent=`已取得 ${state.posts.length} 个作品。直链可能过期；预览失败时可尝试流式保存。`;
        if(job.status==='failed')message('没有作品解析成功，请查看下方错误原因。',true);
        if(job.status==='partial')message('部分作品未能解析，已保留成功结果。',true);
        if(job.status==='cancelled')message('解析已取消。');
        return;
      }
      await sleep(1000);
    }
    throw Error('任务监测超时，无法确认解析结果。请稍后重试。');
  }catch(error){if(!state.cancelRequested && parseToken===currentParseToken){message(error.message,true);$('#parseState').textContent='无法完成解析';}}
  finally{if(parseToken===currentParseToken){state.busy=false;$('#cancelParse').disabled=true;$('#emptyState').hidden=!!state.posts.length;updateButtons();}}
}
$('#shareInput').addEventListener('input',()=>{$('#charCount').textContent=`${$('#shareInput').value.length} / 20000`;updateButtons();});
$('#parseButton').addEventListener('click',parse);
$('#pasteButton').addEventListener('click',async()=>{try{$('#shareInput').value=(await navigator.clipboard.readText()).slice(0,20000);$('#shareInput').dispatchEvent(new Event('input'));}catch{message('未获得剪贴板权限，请直接粘贴分享内容到输入框。',true);}});
$('#cancelParse').addEventListener('click',async()=>{
  state.cancelRequested=true;
  $('#cancelParse').disabled=true;
  $('#parseState').textContent='正在取消解析…';
  const targetId=state.parseId;
  if(targetId){
    try{
      await api(`/api/jobs/${encodeURIComponent(targetId)}`,{method:'DELETE'});
      $('#parseState').textContent='解析已取消';
      message('解析已取消。');
    }catch(error){
      $('#parseState').textContent='已停止等待';
      message('已停止等待解析，但后端任务取消未能确认，请稍后检查。',true);
    }
  }
});
$('#clearButton').addEventListener('click',()=>{state.posts=[];state.cards.clear();$('#results').replaceChildren();$('#parseErrors').replaceChildren();$('#emptyState').hidden=false;$('#resultCount').textContent='0';$('#parseStatus').hidden=true;message('');updateButtons();});
$('#batchDownload').addEventListener('click',async()=>{try{await download(state.posts,state.posts.flatMap(selected),$('#metadataOption').checked?state.posts.map(p=>p.id):[]);}catch(error){message(error.message,true);}});
for(const input of document.querySelectorAll('.global-options input[data-kind]'))input.addEventListener('change',()=>{for(const card of state.cards.values()){for(const asset of card.querySelectorAll(`input[data-kind="${input.dataset.kind}"]`))asset.checked=input.checked;const group=$(`input[data-group="${input.dataset.kind}"]`,card);if(group)group.checked=input.checked;}});
let backendInitToken = 0;
$('#settingsButton').addEventListener('click',()=>{$('#apiUrl').value=state.base;$('#apiKey').value=state.key;$('#settingsDialog').showModal();});$('#closeSettings').addEventListener('click',()=>$('#settingsDialog').close());
$('#settingsForm').addEventListener('submit',async event=>{
  event.preventDefault();
  backendInitToken++;
  const button=$('#connectButton');button.disabled=true;button.textContent='正在连接…';$('#connectionError').textContent='';
  try{
    state.base=validBase($('#apiUrl').value.trim());state.key=$('#apiKey').value;
    const result=await api('/api/health',{},65000);
    if(result.service!=='douyin-media')throw Error('这个地址不是兼容的抖音媒体后端');
    state.fileLimit=result.limits.file_bytes;state.batchLimit=result.limits.total_bytes;
    if(result.requires_api_key){
      await api('/api/jobs/connection-check',{},10000).catch(error=>{if(!error.message.includes('任务不存在'))throw error;});
    }
    localStorage.setItem('shiying-api-url',state.base);
    connection(true);
    $('#settingsDialog').close();
    message(`后端已连接。每批最多 ${result.limits.batch} 条；媒体直接保存到本机。`);
  }catch(error){
    connection(false);
    $('#connectionError').textContent=error.message;
  }finally{
    button.disabled=false;button.textContent='测试并连接 →';
  }
});
async function initBackend(){
  const currentToken = ++backendInitToken;
  let userSaved='';
  try{userSaved=localStorage.getItem('shiying-api-url')||'';}catch{}

  let defaultUrl='';
  try{
    const configUrl=new URL('./config.json',import.meta.url).href;
    const resp=await fetch(configUrl,{cache:'no-store'});
    if(resp.ok){
      const cfg=await resp.json();
      if(cfg && typeof cfg.apiBaseUrl==='string' && cfg.apiBaseUrl.trim()){
        defaultUrl=validBase(cfg.apiBaseUrl.trim());
      }
    }
  }catch{}

  const activeUrl=userSaved||defaultUrl;
  if(backendInitToken!==currentToken) return;
  if(activeUrl){
    state.base=activeUrl;
    try{
      const result=await api('/api/health',{},5000);
      if(backendInitToken!==currentToken || state.base!==activeUrl) return;
      if(result && result.service==='douyin-media'){
        state.fileLimit=result.limits?.file_bytes||state.fileLimit;
        state.batchLimit=result.limits?.total_bytes||state.batchLimit;
        if(result.requires_api_key && !state.key){
          connection(false);
          message('后端已就绪但需要访问密钥，请在设置中输入密钥后连接。');
          return;
        }
        connection(true);
        message(`后端已连接。每批最多 ${result.limits?.batch||10} 条；媒体直接保存到本机。`);
        return;
      }
    }catch{
      if(backendInitToken===currentToken && state.base===activeUrl) connection(false);
      return;
    }
    return;
  }

  // 配置为空时保持原版行为
  if(backendInitToken!==currentToken) return;
  try{state.base=(['localhost','127.0.0.1'].includes(location.hostname)?'http://127.0.0.1:8000':'');}catch{}
  connection(false);
}

initBackend();

$('#stopDownload').addEventListener('click',()=>{state.stop=true;state.controller?.abort();});
