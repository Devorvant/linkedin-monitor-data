// Executes the actual page script with synthetic data; no browser or API requests.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'fixtures/profi_monitor_ui.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
const stamp='2026-10-12T12:00:00Z',capture='2026-10-12T10:00:00+03:00';
class ClockDate extends Date{constructor(...args){super(...(args.length?args:[stamp]))}static now(){return new Date(stamp).getTime()}}
const orders=[{order_id:'1',title:'MATLAB',remote:true,description:'Модель',profile_fit:'high',observed_dates:['2026-10-12']},
 {order_id:'2',title:'Python',remote:false,profile_fit:'medium',observed_dates:['2026-10-12']},
 {order_id:'3',title:'Другая задача',remote:true,profile_fit:'other',observed_dates:['2026-10-12']}];
function recommendation(id,action){return {order_id:id,title:orders[Number(id)-1].title,recommendation:action,
 analysis_source:'ai',remote:orders[Number(id)-1].remote,priority_score:80,technical_fit:90,
 reason:'Проверенная причина <script>alert(1)</script>',confidence:'high',budget_assessment:'Бюджет уточнить',
 format_assessment:'Формат',questions:['Вопрос?'],gaps:[],evidence:['Модель'],
 draft:action==='skip'?'':'Здравствуйте! Уточните исходные данные.'}}
const recs={source_generated_at:stamp,captured_at:capture,generated_at:stamp,status:'AI',
 summary:{orders:3,respond:1,clarify:1,skip:1,ai_orders:3,preliminary_orders:0},
 items:[recommendation('1','respond'),recommendation('2','clarify'),recommendation('3','skip')]};
async function context(recommendations=recs){
 const elements=new Map(),copied=[];const button={dataset:{id:'1'},nextElementSibling:{textContent:''}};
 const defaults={view:'current',fit:'all',remote:'all',priced:'all',sort:'fit',search:'',adviceSearch:'',adviceAction:'shortlist',adviceRemote:'all',adviceView:'current',adviceSnapshot:''};
 function el(id){if(!elements.has(id))elements.set(id,{id,value:defaults[id]||'',textContent:'',innerHTML:'',hidden:false,disabled:false,classList:{toggle(){}},setAttribute(){},addEventListener(){}});return elements.get(id)}
 const files={'profi_current.json':{generated_at:stamp,captured_at:capture,orders,new_ids:[]},
 'profi_history.json':{generated_at:stamp,orders},'profi_pipeline_status.json':{generated_at:stamp,captured_at:capture,summary:{orders:3},daily:[],runs:[]},
 'profi_recommendations.json':recommendations==null?null:JSON.parse(JSON.stringify(recommendations))};
 const olderPath='b'.repeat(40)+'/profi_recommendations.json',newerPath='a'.repeat(40)+'/profi_recommendations.json';
 files['profi_recommendation_history.json']={runs:[{snapshot_id:'aaaaaaaaaaaaaaaa',date:'2026-10-12',captured_at:capture,analyzed_at:stamp,ref:'a'.repeat(40),path:'profi_recommendations.json'},{snapshot_id:'bbbbbbbbbbbbbbbb',date:'2026-10-11',captured_at:'2026-10-11T10:00:00+03:00',analyzed_at:'2026-10-11T12:00:00Z',ref:'b'.repeat(40),path:'profi_recommendations.json'}]};
 files[newerPath]={captured_at:capture,generated_at:stamp,items:JSON.parse(JSON.stringify(recs.items))};
 files[olderPath]={captured_at:'2026-10-11T10:00:00+03:00',generated_at:'2026-10-11T12:00:00Z',items:[{...recs.items[0],recommendation:'skip',draft:''}]};
 const ctx=vm.createContext({Intl,Date:ClockDate,Math,String,Number,Set,Map,Object,Array,RegExp,console,setInterval(){},
 navigator:{clipboard:{writeText:async t=>copied.push(t)}},
 document:{getElementById:el,querySelectorAll(selector){return selector==='.copy-draft'?[button]:[]}},
 fetch:async url=>{const found=url.match(/\/(main|[a-f0-9]{40})\/(.+)\?/);const file=(found[1]==='main'?'':found[1]+'/')+found[2];return {ok:files[file]!=null,status:404,json:async()=>files[file]}}});
 vm.runInContext(script,ctx);await new Promise(setImmediate);await vm.runInContext('loadData()',ctx);
 return {ctx,el,copied,button};
}
(async()=>{
 let {ctx,el,copied,button}=await context();
 assert.equal(el('analysisStatus').textContent,'ИИ-анализ готов');
 assert.equal(vm.runInContext('visibleAdvice().length',ctx),2);
 assert(el('recommendationCards').innerHTML.includes('&lt;script&gt;'));
 assert(!el('recommendationCards').innerHTML.includes('<script>'));
 assert(el('recommendationCards').innerHTML.includes('Черновик отклика'));
 await button.onclick();assert.equal(copied[0],recs.items[0].draft);
 el('adviceAction').value='skip';vm.runInContext('renderRecommendations()',ctx);
 assert.equal(vm.runInContext('visibleAdvice().length',ctx),1);
 assert(!el('recommendationCards').innerHTML.includes('Черновик отклика'));
 el('adviceAction').value='all';el('adviceRemote').value='remote';assert.equal(vm.runInContext('visibleAdvice().length',ctx),2);
 el('adviceRemote').value='all';el('adviceSearch').value='Python';assert.equal(vm.runInContext('visibleAdvice().length',ctx),1);
 vm.runInContext("showPanel('recommendations')",ctx);
 assert.equal(el('recommendationsPanel').hidden,false);assert.equal(el('ordersPanel').hidden,true);assert.equal(el('historyPanel').hidden,true);
 vm.runInContext("recommendations.source_generated_at='older';renderRecommendations()",ctx);
 assert.equal(el('analysisStatus').textContent,'Ожидаем обновление');assert.equal(vm.runInContext('visibleAdvice().length',ctx),0);
 ({ctx,el}=await context(null));assert.equal(el('analysisStatus').textContent,'Ожидаем анализ');assert.equal(el('pipelineStatus').textContent,'Данные загружены');
 ({ctx,el,copied,button}=await context());
 vm.runInContext("advicePeriod='all';$('adviceView').value='history'",ctx);await vm.runInContext('loadAdviceHistory()',ctx);
 assert.equal(vm.runInContext('adviceHistoryRows.length',ctx),3);
 assert.equal(vm.runInContext("adviceHistoryRows.find(r=>r.order_id==='1')._timeline.length",ctx),2);
 assert(el('recommendationCards').innerHTML.includes('Предыдущие оценки'));
 await button.onclick();assert.equal(copied.length,0);
 el('adviceSnapshot').value='bbbbbbbbbbbbbbbb';await vm.runInContext('loadAdviceHistory()',ctx);
 assert.equal(vm.runInContext('adviceHistoryRows.length',ctx),1);
 assert.equal(vm.runInContext("adviceHistoryRows[0].recommendation",ctx),'skip');
 const preliminary=JSON.parse(JSON.stringify(recs));preliminary.status='RULES_ONLY';preliminary.summary.ai_orders=0;preliminary.summary.preliminary_orders=3;
 preliminary.items.forEach(r=>{r.analysis_source='rules';r.technical_fit=null;r.priority_score=null;r.draft=''});
 ({ctx,el}=await context(preliminary));assert.equal(el('analysisStatus').textContent,'Предварительный отбор');
 assert(el('recommendationCards').innerHTML.includes('Предварительно · по словам'));
 assert(!el('recommendationCards').innerHTML.includes('Приоритет:'));
 assert.equal(vm.runInContext("safeOrderUrl({order_id:'javascript:alert(1)'})",ctx),null);
 vm.runInContext("advicePeriod='all';$('adviceView').value='current'",ctx);
 assert.equal(vm.runInContext("adviceFresh({order_id:'1',_capture_at:'2026-10-08T10:00:00+03:00'})",ctx),false);
 assert.equal(vm.runInContext("adviceFresh({order_id:'1',_capture_at:'2026-10-12T10:00:00+03:00',_historical:true})",ctx),false);
 assert.equal(vm.runInContext("adviceFresh({order_id:'999',_capture_at:'2026-10-12T10:00:00+03:00'})",ctx),false);
 vm.runInContext("$('adviceView').value='history';adviceHistoryLoaded=true;adviceHistoryRows=[{...recommendations.items[0],_historical:true,_capture_at:'2026-10-09T10:00:00+03:00'}];renderRecommendations()",ctx);
 assert.equal(vm.runInContext('visibleAdvice().length',ctx),1);
 assert(el('recommendationCards').innerHTML.includes('Архивная оценка'));
 vm.runInContext("advicePeriod='3'",ctx);assert.equal(vm.runInContext('visibleAdvice().length',ctx),0);
 console.log('Recommendation UI passed: filter, provenance, freshness, escaping, safe URLs, manual copy, missing API/data and tabs.');
})().catch(e=>{console.error(e);process.exitCode=1});
