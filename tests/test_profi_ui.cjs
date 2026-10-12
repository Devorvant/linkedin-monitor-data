// Executes the actual page script with synthetic data; no browser or API requests.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'fixtures/profi_monitor_ui.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
const stamp='2026-10-12T12:00:00Z',capture='2026-10-12T10:00:00+03:00';
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
 const defaults={view:'current',fit:'all',remote:'all',priced:'all',sort:'fit',search:'',adviceSearch:'',adviceAction:'shortlist',adviceRemote:'all'};
 function el(id){if(!elements.has(id))elements.set(id,{id,value:defaults[id]||'',textContent:'',innerHTML:'',hidden:false,disabled:false,classList:{toggle(){}},setAttribute(){},addEventListener(){}});return elements.get(id)}
 const files={'profi_current.json':{generated_at:stamp,captured_at:capture,orders,new_ids:[]},
 'profi_history.json':{generated_at:stamp,orders},'profi_pipeline_status.json':{generated_at:stamp,captured_at:capture,summary:{orders:3},daily:[],runs:[]},
 'profi_recommendations.json':recommendations==null?null:JSON.parse(JSON.stringify(recommendations))};
 const ctx=vm.createContext({Intl,Date,Math,String,Number,Set,Map,Object,Array,RegExp,console,setInterval(){},
 navigator:{clipboard:{writeText:async t=>copied.push(t)}},
 document:{getElementById:el,querySelectorAll(selector){return selector==='.copy-draft'?[button]:[]}},
 fetch:async url=>{const file=url.split('/main/')[1].split('?')[0];return {ok:files[file]!=null,status:404,json:async()=>files[file]}}});
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
 const preliminary=JSON.parse(JSON.stringify(recs));preliminary.status='RULES_ONLY';preliminary.summary.ai_orders=0;preliminary.summary.preliminary_orders=3;
 preliminary.items.forEach(r=>{r.analysis_source='rules';r.technical_fit=null;r.priority_score=null;r.draft=''});
 ({ctx,el}=await context(preliminary));assert.equal(el('analysisStatus').textContent,'Предварительный отбор');
 assert(el('recommendationCards').innerHTML.includes('Предварительно · по словам'));
 assert(!el('recommendationCards').innerHTML.includes('Приоритет:'));
 assert.equal(vm.runInContext("safeOrderUrl({order_id:'javascript:alert(1)'})",ctx),null);
 console.log('Recommendation UI passed: filter, provenance, freshness, escaping, safe URLs, manual copy, missing API/data and tabs.');
})().catch(e=>{console.error(e);process.exitCode=1});
