'use strict';
const state = {user:null, csrf:'', schema:null, page:'reports', reportPage:1, filters:{}, report:null, systems:[], users:[]};
const main = document.querySelector('#main');
const el = (tag, attrs={}, children=[]) => {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else if (value !== false && value != null) node.setAttribute(key,value === true ? '' : value);
  }
  for (const child of [].concat(children)) if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
  return node;
};
const button = (text, action, secondary=false) => el('button',{type:'button',text,class:secondary?'secondary':'',onclick:action});
const option = (value,text=value) => el('option',{value,text});
const title = (text,subtitle,actions=[]) => el('div',{class:'heading'},[el('div',{},[el('h1',{text}),el('p',{class:'intro',text:subtitle})]),el('div',{class:'actions'},actions)]);
function flash(message){const n=document.querySelector('#notice');n.textContent=message;n.hidden=false;clearTimeout(flash.timer);flash.timer=setTimeout(()=>n.hidden=true,5000);}
function errorView(error,target=main){
  const old=target.querySelector('.error');if(old)old.remove();
  const box=el('div',{class:'error',role:'alert',tabindex:'-1'},[el('strong',{text:error.message || 'The request could not be completed.'})]);
  if(error.fields)box.append(el('pre',{text:JSON.stringify(error.fields,null,2)}));
  if(error.code==='stale_version'||error.status===409)box.append(el('p',{text:'This record changed in another session. Reload it before editing again.'}),button('Reload record',()=>state.report?viewReport(state.report.id):navigate(state.page),true));
  target.prepend(box);box.focus();
}
async function api(path,method='GET',data){
  const headers={};if(data!==undefined)headers['Content-Type']='application/json';if(method!=='GET')headers['X-CSRF-Token']=state.csrf;
  const response=await fetch(path,{method,headers,credentials:'same-origin',body:data===undefined?undefined:JSON.stringify(data)});
  if(!response.ok){let payload;try{payload=await response.json();}catch{payload={error:{message:`Request failed (${response.status}).`}};}const err=new Error(payload.error?.message || 'Request failed.');Object.assign(err,payload.error,{status:response.status});if(response.status===401 && state.user){state.user=null;state.schema=null;state.csrf='';renderSession();await initialize();}throw err;}
  return response.json();
}
async function guarded(action,target=main){try{return await action();}catch(error){errorView(error,target);}}
async function busy(form,action){const controls=[...form.querySelectorAll('button')];controls.forEach(b=>b.disabled=true);try{await action();}catch(error){errorView(error,form);}finally{controls.forEach(b=>b.disabled=false);}}
function renderSession(){
  const account=document.querySelector('#account');account.replaceChildren();const nav=document.querySelector('#navigation');nav.replaceChildren();nav.hidden=!state.user;if(!state.user)return;
  account.append(el('span',{text:`${state.user.username} · ${state.user.role}`}),button('Sign out',()=>guarded(async()=>{await api('/api/logout','POST',{});state.user=null;state.schema=null;await initialize();}),true));
  const pages=[['reports','Demo reports'],['systems','Equipment']];if(state.user.role==='admin')pages.push(['imports','Excel import'],['users','Users'],['audit','Audit log']);
  for(const [id,label] of pages)nav.append(button(label,()=>navigate(id)));
  [...nav.children].forEach((b,i)=>{if(pages[i][0]===state.page)b.setAttribute('aria-current','page');});
}
async function navigate(page){state.page=page;state.report=null;renderSession();main.replaceChildren(el('p',{role:'status',text:'Loading…'}));await guarded(async()=>{if(page==='reports')await reportsView();if(page==='systems')await systemsView();if(page==='imports')await importsView();if(page==='users')await usersView();if(page==='audit')await auditView();});}
function labeled(label,input,help){const id=input.id || `input-${++labeled.n}`;input.id=id;return el('div',{class:'field'},[el('label',{for:id,text:label}),input,help?el('small',{text:help}):null]);}labeled.n=0;
function loginView(){
  const form=el('form',{class:'card login'},[el('h1',{text:'Sign in to your workspace'}),el('p',{class:'intro',text:'Use an account created by your workspace administrator.'})]);
  const username=el('input',{name:'username',autocomplete:'username',required:true,maxlength:250});const password=el('input',{name:'password',type:'password',autocomplete:'current-password',required:true});
  form.append(labeled('Username',username),labeled('Password',password),el('button',{type:'submit',text:'Sign in'}));
  form.addEventListener('submit',event=>{event.preventDefault();busy(form,async()=>{const data=await api('/api/login','POST',{username:username.value,password:password.value});password.value='';state.user=data.user;state.csrf=data.csrf_token;state.schema=await api('/api/schema');await navigate('reports');});});main.replaceChildren(form);
}
function filterQuery(extra={}){const params=new URLSearchParams({...state.filters,...extra});for(const [k,v] of [...params])if(!v)params.delete(k);return params.toString();}
function fieldValue(report,id){const c=report.fields?.[id];return c?.state==='value'?c.value:'';}
function table(headers,rows){return el('div',{class:'table-wrap'},el('table',{},[el('thead',{},el('tr',{},headers.map(h=>el('th',{scope:'col',text:h})))),el('tbody',{},rows.length?rows.map(cells=>el('tr',{},cells.map(c=>el('td',{},c?.nodeType?c:String(c??''))))):el('tr',{},el('td',{colspan:headers.length,class:'empty',text:'No records found.'})))]));}
async function exportCsv(){await guarded(async()=>{const response=await fetch(`/api/reports/export.csv?${filterQuery()}`,{credentials:'same-origin'});if(!response.ok){const data=await response.json();throw new Error(data.error?.message || 'Export failed.');}const url=URL.createObjectURL(await response.blob());const a=el('a',{href:url,download:'demo-reports.csv'});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);flash('CSV export downloaded.');});}
async function reportsView(){
  const data=await api(`/api/reports?${filterQuery({page:state.reportPage,page_size:20})}`);
  const actions=[button('Export CSV',exportCsv,true)];if(state.user.role!=='reviewer')actions.push(button('New demo report',()=>reportForm()));
  main.replaceChildren(title('Demo reports','Record deployment, surgery outcomes, navigation use and field feedback.',actions));
  const filters=el('form',{class:'card filters'});
  const definitions=[['q','Search hospital, city, surgeon or system','text'],['date_from','Surgery from','date'],['date_to','Surgery to','date'],['status','Status',['draft','submitted']],['surgery_type','Surgery type',(state.schema.versions.V3.find(f=>f.id==='surgery_type')?.options || [])],['system_identity','System identity','text'],['system_base','System base',['Optical','EM']]];
  if(state.user.role!=='engineer')definitions.push(['owner_id','Owner ID','text']);
  for(const [key,label,type] of definitions){const input=Array.isArray(type)?el('select',{name:key},[option('','All'),...type.map(v=>option(v))]):el('input',{name:key,type});input.value=state.filters[key]||'';filters.append(labeled(label,input));}
  filters.append(el('div',{class:'actions'},[el('button',{type:'submit',text:'Apply filters'}),button('Clear',()=>{state.filters={};state.reportPage=1;navigate('reports');},true)]));
  filters.addEventListener('submit',e=>{e.preventDefault();state.filters=Object.fromEntries(new FormData(filters));state.reportPage=1;navigate('reports');});main.append(filters);
  main.append(table(['Hospital / city','Surgery date','Surgeon','System','Version','Status','Open'],data.items.map(r=>[el('div',{},[el('strong',{text:fieldValue(r,'hospital_name')||'Untitled draft'}),el('div',{class:'muted',text:fieldValue(r,'city')})]),fieldValue(r,'surgery_date'),fieldValue(r,'surgeon_name'),fieldValue(r,'system_identity'),r.form_version,el('span',{class:`badge ${r.status}`,text:r.status}),button('View',()=>guarded(()=>viewReport(r.id)),true)])));
  main.append(el('div',{class:'pagination'},[el('span',{text:`${data.total} reports · Page ${data.page}`}),el('div',{class:'actions'},[el('button',{type:'button',class:'secondary',text:'Previous',disabled:data.page<=1,onclick:()=>{state.reportPage--;navigate('reports');}}),el('button',{type:'button',class:'secondary',text:'Next',disabled:data.page*data.page_size>=data.total,onclick:()=>{state.reportPage++;navigate('reports');}})])]));
}
function groupFields(fields){const groups=new Map();for(const f of fields){const section=f.section || 'Report details';if(!groups.has(section))groups.set(section,[]);groups.get(section).push(f);}return groups;}
function showCell(cell,field){
  if(!cell || cell.state!=='value')return el('span',{class:'muted',text:(cell?.state || 'not_recorded').replaceAll('_',' ')});
  if(field.type==='links'){const div=el('div',{class:'detail-value'});for(const link of Array.isArray(cell.value)?cell.value:[]){try{if(new URL(link).protocol==='https:')div.append(el('a',{href:link,target:'_blank',rel:'noopener noreferrer',text:`External link: ${link}`}));}catch{/* Invalid imported URLs remain inert. */}}return div;}
  return el('div',{class:'detail-value',text:typeof cell.value==='boolean'?(cell.value?'Yes':'No'):String(cell.value)});
}
async function viewReport(id){
  const {report,comments}=await api(`/api/reports/${encodeURIComponent(id)}`);state.report=report;
  const actions=[button('Back to reports',()=>navigate('reports'),true)];if(state.user.role==='admin'||(state.user.role==='engineer'&&report.owner_id===state.user.id))actions.push(button('Edit report',()=>reportForm(report)));
  main.replaceChildren(title(fieldValue(report,'hospital_name')||'Demo report',`${report.form_version} · ${report.status} · Revision ${report.version}`,actions));
  if(report.source)main.append(el('div',{class:'card'},[el('h2',{text:'Historical source'}),el('p',{text:`${report.source.filename || report.source.workbook_filename || 'Excel workbook'} · ${report.source.sheet || ''} · Row ${report.source.row || ''}`}),el('p',{class:'muted',text:'Historical values retain explicit unknown and unavailable states. Source dates may not have a known timezone.'})]));
  for(const [section,fields] of groupFields(state.schema.versions[report.form_version])){const card=el('section',{class:'card'},[el('h2',{text:section}),el('dl',{class:'grid'})]);const dl=card.querySelector('dl');for(const f of fields){const cell=report.fields[f.id];const block=el('div',{class:'field'},[el('dt',{},el('strong',{text:f.label})),el('dd',{},showCell(cell,f))]);if(cell?.raw!==undefined)block.append(el('div',{class:'raw-note',text:`Original imported value: ${String(cell.raw)}`}));dl.append(block);}main.append(card);}
  const commentCard=el('section',{class:'card'},el('h2',{text:'Reviewer comments'}));for(const c of comments)commentCard.append(el('article',{class:'comment'},[el('strong',{text:c.author_username || c.author_id}),el('span',{class:'muted',text:` · ${c.created_at}`}),el('p',{text:c.body})]));if(!comments.length)commentCard.append(el('p',{class:'muted',text:'No reviewer comments yet.'}));
  if(state.user.role!=='engineer'){const form=el('form');const body=el('textarea',{required:true,maxlength:4000,name:'body'});form.append(labeled('Add reviewer comment',body),el('button',{type:'submit',text:'Add comment'}));form.addEventListener('submit',e=>{e.preventDefault();busy(form,async()=>{await api(`/api/reports/${id}/comments`,'POST',{body:body.value});await viewReport(id);flash('Comment added.');});});commentCard.append(form);}main.append(commentCard);
}
function reportCell(control){
  const {field,input,stateControl,original,initialValue,initialState}=control;
  // Preserve exact imported precision, timezone, raw text and states on unrelated edits.
  if(input.value===initialValue&&stateControl.value===initialState)return {...original};
  let cellState=stateControl.value;
  let value=null;
  if(cellState==='value'){
    value=input.value;
    if(value==='')cellState='not_recorded';
    else if(field.type==='boolean')value=value==='true';
    else if(['integer','decimal'].includes(field.type)){
      value=Number(value);
      if(!Number.isFinite(value)||value<0||(field.type==='integer'&&!Number.isInteger(value)))throw new Error(`${field.label}: enter a nonnegative ${field.type==='integer'?'whole number':'number'}.`);
    }else if(field.type==='links'){
      value=value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
      if(value.some(x=>{try{return new URL(x).protocol!=='https:';}catch{return true;}}))throw new Error(`${field.label}: only valid HTTPS links are accepted.`);
    }else if(field.type==='datetime'){
      if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)||Number.isNaN(Date.parse(value)))throw new Error(`${field.label}: use an ISO timestamp with seconds and optional fractional seconds; include Z or a UTC offset if known.`);
    }
  }
  const cell={state:cellState,value:cellState==='value'?value:null};
  if(original.raw!==undefined)cell.raw=original.raw;
  return cell;
}
function resourceValue(input){
  if(input.type==='number')return input.value===''?null:Number(input.value);
  if(input.type==='date')return input.value===''?null:input.value;
  return input.value;
}
function reportForm(report=null){
  state.report=report;const versionSelect=el('select',{name:'form_version'},Object.keys(state.schema.versions).map(v=>option(v)));versionSelect.value=report?.form_version || 'V3';versionSelect.disabled=!!report;
  main.replaceChildren(title(report?'Edit demo report':'New demo report','Save incomplete information as a draft. Choose an explicit state when a measurement or answer is unavailable.',[button('Cancel',()=>report?guarded(()=>viewReport(report.id)):navigate('reports'),true)]));
  const form=el('form',{novalidate:true});form.append(labeled('Form version',versionSelect));const fieldsContainer=el('div');form.append(fieldsContainer);const controls=new Map();
  const build=()=>{controls.clear();fieldsContainer.replaceChildren();for(const [section,fields] of groupFields(state.schema.versions[versionSelect.value])){const group=el('fieldset',{},[el('legend',{text:section}),el('div',{class:'grid'})]);const grid=group.querySelector('.grid');for(const field of fields){const cell=report?.fields[field.id] || {state:'not_recorded',value:null};const stateControl=el('select',{class:'field-state','aria-label':`${field.label}: information state`},[option('value','Recorded value'),option('unknown','Unknown'),option('not_applicable','Not applicable'),option('not_recorded','Not recorded')]);stateControl.value=cell.state;
      let input;if(field.type==='enum')input=el('select',{},[option('','Choose…'),...(field.options || []).map(v=>option(v))]);else if(field.type==='boolean')input=el('select',{},[option('','Choose…'),option('true','Yes'),option('false','No')]);else if(field.type==='text'||field.type==='links')input=el('textarea',{maxlength:field.type==='links'?20500:10000});else input=el('input',{type:field.type==='date'?'date':field.type==='datetime'?'text':['integer','decimal'].includes(field.type)?'number':'text',maxlength:250});
      if(['integer','decimal'].includes(field.type)){input.min='0';input.step=field.type==='integer'?'1':'any';}input.classList.add('value-control');input.id=`field-${field.id}`;input.value=cell.state==='value'?(Array.isArray(cell.value)?cell.value.join('\n'):cell.value===null?'':String(cell.value)):'';if(field.type==='datetime')input.placeholder='2026-10-11T14:30:45.123+05:30';
      const block=el('div',{class:`field ${['text','links'].includes(field.type)?'wide':''}`},[el('label',{for:input.id},[field.label,field.required?el('span',{class:'required',text:' *'}):null]),el('div',{class:'state-row'},[stateControl,input])]);if(field.type==='links')block.append(el('small',{text:'HTTPS links only, one per line. Links are not uploaded or fetched.'}));if(field.type==='datetime')block.append(el('small',{text:'ISO timestamp with seconds and optional fractions. Include Z or +05:30 when known; omit timezone only when unknown.'}));if(cell.raw!==undefined)block.append(el('div',{class:'raw-note',text:`Original imported value: ${String(cell.raw)}`}));
      const sync=()=>{input.disabled=stateControl.value!=='value';input.setAttribute('aria-required',String(field.required&&stateControl.value==='value'));};stateControl.addEventListener('change',sync);input.addEventListener('input',()=>{if(input.value!==''){stateControl.value='value';sync();}});sync();controls.set(field.id,{field,input,stateControl,original:cell,initialValue:input.value,initialState:stateControl.value});grid.append(block);}fieldsContainer.append(group);}};
  versionSelect.addEventListener('change',build);build();
  const save=async(status)=>{const fields={};for(const [id,control] of controls)fields[id]=reportCell(control);
    const data=report?await api(`/api/reports/${report.id}`,'PUT',{fields,status,version:report.version}):await api('/api/reports','POST',{fields,status,form_version:versionSelect.value});await viewReport(data.report.id);flash(status==='submitted'?'Report submitted.':'Draft saved.');};
  form.append(el('div',{class:'sticky-actions'},[el('span',{class:'muted',text:'* Required for native V3 submission; conditional details may also be required.'}),el('div',{class:'actions'},[button('Save draft',()=>busy(form,()=>save('draft')),true),button('Submit report',()=>busy(form,()=>save('submitted')))])]));form.addEventListener('submit',e=>e.preventDefault());main.append(form);
}
const systemFields=[['identity','System identity'],['name','Display name'],['city','City'],['hospital','Hospital'],['application_experts','Application experts'],['issues','Current issues','textarea'],['readiness','Readiness',['ready','not_ready','unknown']]];
const itemFields=()=>[['category','Category',state.schema.inventory.categories],['name','Item name'],['serial_number','Serial number'],['quantity','Quantity','number'],['availability','Availability',state.schema.inventory.availability],['remarks','Remarks','textarea']];
const movementFields=[['item_name','Item name'],['event_date','Event date','date'],['quantity','Quantity','number'],['provided_by','Provided by'],['reference_note','Reference / note','textarea'],['cost','Cost (currency unspecified)','number']];
function resourceForm(heading,definitions,record,onSave,cancel){
  const form=el('form',{class:'card'});form.append(el('h2',{text:heading}));const grid=el('div',{class:'grid'});const inputs=new Map();for(const [key,label,type] of definitions){const input=Array.isArray(type)?el('select',{},type.map(v=>option(v,v.replaceAll('_',' ')))):type==='textarea'?el('textarea',{maxlength:10000}):el('input',{type:type || 'text',maxlength:250});if(type==='number'){input.min=0;input.step=key==='cost'?'any':'1';}input.value=record?.[key]??(Array.isArray(type)?type[0]:'');inputs.set(key,input);grid.append(labeled(label,input));}form.append(grid,el('div',{class:'actions'},[el('button',{type:'submit',text:'Save'}),button('Cancel',cancel,true)]));form.addEventListener('submit',e=>{e.preventDefault();busy(form,async()=>{const payload={};for(const [key,input] of inputs)payload[key]=resourceValue(input);if(record?.version)payload.version=record.version;await onSave(payload);});});main.append(form);form.scrollIntoView({block:'start'});return form;
}
async function systemsView(){
  const data=await api('/api/systems');state.systems=data.items;
  main.replaceChildren(title('Equipment','Track systems, readiness, component availability and extra-instrument movements.',state.user.role==='admin'?[button('Add system',()=>resourceForm('Add system',systemFields,null,async payload=>{await api('/api/systems','POST',payload);await systemsView();flash('System added.');},()=>systemsView()))]:[]));
  main.append(table(['System','Location','Hospital','Readiness','Updated','Actions'],data.items.map(s=>[el('div',{},[el('strong',{text:s.identity}),el('div',{class:'muted',text:s.name})]),s.city,s.hospital,el('span',{class:'badge',text:s.readiness.replaceAll('_',' ')}),s.updated_at,el('div',{class:'actions'},[button('Inventory',()=>guarded(()=>inventoryView(s.id)),true),state.user.role==='admin'?button('Edit',()=>resourceForm('Edit system',systemFields,s,async payload=>{await api(`/api/systems/${s.id}`,'PUT',payload);await systemsView();flash('System updated.');},()=>systemsView()),true):null])])));
}
async function inventoryView(id){
  const system=state.systems.find(s=>s.id===id);const data=await api(`/api/systems/${id}/inventory`);const admin=state.user.role==='admin';
  const itemForm=(item=null)=>resourceForm(item?'Edit item':'Add inventory item',itemFields(),item,async payload=>{await api(item?`/api/inventory/${item.id}`:`/api/systems/${id}/inventory`,item?'PUT':'POST',payload);await inventoryView(id);flash('Inventory saved.');},()=>inventoryView(id));
  main.replaceChildren(title(system?.identity || 'System inventory','Availability is separate from quantity. Movement records do not automatically reconcile stock.',[button('Back to equipment',()=>navigate('systems'),true),admin?button('Add item',()=>itemForm()):null]));
  main.append(table(['Category','Item','Serial','Quantity','Availability','Remarks',...(admin?['Actions']:[])],data.items.map(i=>[i.category,i.name,i.serial_number,i.quantity??'Not recorded',i.availability,i.remarks,...(admin?[button('Edit',()=>itemForm(i),true)]:[])])));
  main.append(el('div',{class:'heading'},[el('h2',{text:'Extra-instrument movements'}),admin?button('Add movement',()=>resourceForm('Add movement',movementFields,null,async payload=>{await api(`/api/systems/${id}/movements`,'POST',payload);await inventoryView(id);flash('Movement recorded.');},()=>inventoryView(id))):null]));main.append(table(['Date','Item','Quantity','Provided by','Reference','Cost (currency unspecified)'],data.movements.map(m=>[m.event_date,m.item_name,m.quantity??'Not recorded',m.provided_by,m.reference_note,m.cost??'Not recorded'])));
}
async function usersView(){
  const data=await api('/api/users');state.users=data.items;main.replaceChildren(title('User administration','Manage local accounts and access. Role, password and activation changes invalidate existing sessions.',[button('Create user',()=>userForm())]));
  main.append(table(['Username','Role','Active','Actions'],data.items.map(u=>[u.username,u.role,u.active?'Yes':'No',button('Manage',()=>userForm(u),true)])));
}
function userForm(user=null){
  const form=el('form',{class:'card'},el('h2',{text:user?`Manage ${user.username}`:'Create user'}));const username=el('input',{required:true,maxlength:250,autocomplete:'off'});const password=el('input',{type:'password',autocomplete:'new-password',required:!user,minlength:12,maxlength:250});const role=el('select',{},['engineer','reviewer','admin'].map(v=>option(v)));role.value=user?.role || 'engineer';if(!user)form.append(labeled('Username',username));form.append(labeled(user?'New password (leave blank to keep)':'Password',password,'At least 12 characters.'),labeled('Role',role));const active=el('input',{type:'checkbox',id:'user-active'});active.checked=!!user?.active;if(user)form.append(el('label',{class:'check',for:'user-active'},[active,'Active account']));form.append(el('div',{class:'actions'},[el('button',{type:'submit',text:'Save user'}),button('Cancel',()=>usersView(),true)]));form.addEventListener('submit',e=>{e.preventDefault();busy(form,async()=>{const payload={role:role.value};if(password.value)payload.password=password.value;if(user)payload.active=active.checked;else payload.username=username.value;await api(user?`/api/users/${user.id}`:'/api/users',user?'PUT':'POST',payload);password.value='';await usersView();flash('User saved.');});});main.append(form);form.scrollIntoView({block:'start'});
}
async function importsView(){
  const users=await api('/api/users');state.users=users.items;
  main.replaceChildren(title('Historical Excel import','Preview workbook structure, warnings and duplicate records before committing. Imported reports become drafts.'));
  const form=el('form',{class:'card'},[el('h2',{text:'1. Preview workbook'}),el('p',{class:'muted',text:'Supported: original V1, V2 and V3 XLSX layouts. Maximum workbook size 16 MiB. No data is written during preview.'})]);const file=el('input',{type:'file',accept:'.xlsx',required:true});form.append(labeled('Excel workbook',file),el('button',{type:'submit',text:'Preview import'}));const result=el('section',{class:'card',hidden:true});form.addEventListener('submit',e=>{e.preventDefault();busy(form,async()=>{result.hidden=true;const selected=file.files[0];if(!selected)throw new Error('Select an XLSX workbook.');if(selected.size>16*1024*1024)throw new Error('Workbook exceeds the 16 MiB limit.');const bytes=new Uint8Array(await selected.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));const preview=await api('/api/imports/preview','POST',{filename:selected.name,content_base64:btoa(binary)});result.replaceChildren(el('h2',{text:'2. Review & commit'}),el('p',{text:`Preview expires: ${preview.expires_at}`}),el('h3',{text:'Summary'}),el('pre',{text:JSON.stringify(preview.summary,null,2)}),el('h3',{text:'Diagnostics'}));const diagnostics=preview.diagnostics || [];if(Array.isArray(diagnostics)){if(!diagnostics.length)result.append(el('p',{text:'No warnings reported.'}));else result.append(el('ul',{},diagnostics.map(d=>el('li',{text:typeof d==='string'?d:JSON.stringify(d)}))));}else result.append(el('pre',{text:JSON.stringify(diagnostics,null,2)}));const commitForm=el('form');const owner=el('select',{required:true},[option('','Select an engineer'),...state.users.filter(u=>u.active&&u.role==='engineer').map(u=>option(u.id,u.username))]);commitForm.append(labeled('Engineer who will own imported reports',owner),el('p',{class:'muted',text:'Review warnings before committing. Existing matching rows are skipped; changed historical rows become new drafts.'}),el('button',{type:'submit',text:'Confirm import'}));commitForm.addEventListener('submit',event=>{event.preventDefault();busy(commitForm,async()=>{const outcome=await api('/api/imports/commit','POST',{token:preview.token,owner_id:owner.value});result.replaceChildren(el('h2',{text:'Import complete'}),el('pre',{text:JSON.stringify(outcome,null,2)}),button('View reports',()=>navigate('reports')));flash('Historical import committed.');});});result.append(commitForm);result.hidden=false;result.scrollIntoView({block:'start'});});});main.append(form,result);
}
async function auditView(page=1){const data=await api(`/api/audit?page=${page}&page_size=50`);main.replaceChildren(title('Audit log','Append-only record of account, report, equipment and import changes. Sensitive narratives and credentials are excluded.'));main.append(table(['Time','Actor','Action','Entity','Changed fields'],data.items.map(a=>[a.created_at,a.actor_id || 'System',a.action,`${a.entity_type} · ${a.entity_id || ''}`,el('pre',{text:typeof a.changed_fields==='object'?JSON.stringify(a.changed_fields):a.changed_fields_json || a.changed_fields || ''})])));main.append(el('div',{class:'pagination'},[el('span',{text:`${data.total} events · Page ${data.page}`}),el('div',{class:'actions'},[el('button',{type:'button',class:'secondary',text:'Previous',disabled:page<=1,onclick:()=>guarded(()=>auditView(page-1))}),el('button',{type:'button',class:'secondary',text:'Next',disabled:page*data.page_size>=data.total,onclick:()=>guarded(()=>auditView(page+1))})])]));}
async function initialize(){try{const session=await api('/api/session');state.user=session.user;state.csrf=session.csrf_token;renderSession();if(state.user){state.schema=await api('/api/schema');await navigate('reports');}else loginView();}catch(error){main.replaceChildren();errorView(error);main.append(button('Try again',initialize,true));}}
initialize();
