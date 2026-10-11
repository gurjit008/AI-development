// Dependency-free tests exercise the actual serialization used by report/resource forms.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const context={document:{querySelector:()=>({})},URL,console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(__dirname+'/app.js','utf8').replace(/initialize\(\);\s*$/,''),context);
const plain=value=>JSON.parse(JSON.stringify(value));
function control(original,type='datetime'){
  const value=original.state==='value'?String(original.value):'';
  return {field:{type,label:'Timestamp'},input:{value},stateControl:{value:original.state},original,initialValue:value,initialState:original.state};
}
test('unrelated edits preserve timestamp seconds, fractions, timezone and raw source',()=>{
  for(const value of ['2026-10-11T15:22:43.123456+05:30','2026-10-11T09:52:43Z','2026-10-11T09:52:43.999999']){
    const original={state:'value',value,raw:'Original Excel cell'};
    assert.deepEqual(plain(context.reportCell(control(original))),original);
  }
});
test('explicit timestamp edits retain full precision and stated offset',()=>{
  const c=control({state:'value',value:'2026-10-11T12:00:00Z',raw:'old raw'});
  c.input.value='2026-10-12T15:22:43.123456+05:30';
  assert.equal(context.reportCell(c).value,c.input.value);
  c.input.value='2026-10-12T15:22';
  assert.throws(()=>context.reportCell(c),/ISO timestamp/);
});
test('changing information state clears value while retaining imported raw',()=>{
  const c=control({state:'value',value:'2026-10-11T12:00:00Z',raw:'source'});
  c.stateControl.value='unknown';
  assert.deepEqual(plain(context.reportCell(c)),{state:'unknown',value:null,raw:'source'});
});
test('optional undated movements send null; entered date remains unchanged',()=>{
  assert.equal(context.resourceValue({type:'date',value:''}),null);
  assert.equal(context.resourceValue({type:'date',value:'2026-10-11'}),'2026-10-11');
  assert.equal(context.resourceValue({type:'number',value:''}),null);
  assert.equal(context.resourceValue({type:'number',value:'0'}),0);
  assert.equal(context.resourceValue({type:'text',value:''}),'');
});
test('expired session fetches fresh pre-auth CSRF before showing login',async()=>{
  const calls=[];
  context.fetch=async(path)=>{
    calls.push(path);
    if(path==='/api/session')return {ok:true,json:async()=>({user:null,csrf_token:'fresh-csrf'})};
    return {ok:false,status:401,json:async()=>({error:{message:'Expired'}})};
  };
  context.loginTokens=[];
  vm.runInContext("renderSession=()=>{}; loginView=()=>loginTokens.push(state.csrf); state.user={id:'old'};state.csrf='stale-csrf';",context);
  await assert.rejects(context.api('/api/reports'),/Expired/);
  assert.deepEqual(calls,['/api/reports','/api/session']);
  assert.deepEqual(context.loginTokens,['fresh-csrf']);
  assert.equal(vm.runInContext('state.user',context),null);
});
