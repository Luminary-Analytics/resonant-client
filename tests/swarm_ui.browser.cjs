/* Real browser controls against a simulated service. No providers or user state.
 * node tests/swarm_ui.browser.cjs [absolute-path-to-playwright-module]
 * SWARM_BROWSER_EXECUTABLE optionally selects a browser binary (default: Edge).
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');
const assets = path.resolve(__dirname, '../lumi/gui/static');

const fixture = `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Team browser fixture · simulated service</title><link rel="stylesheet" href="/styles.css"><link rel="stylesheet" href="/swarm_view.css">
<style>body{padding:24px}button{padding:8px}main{max-width:680px}textarea{display:block}</style>
<main><h1>Team browser fixture</h1><p>Simulated service responses. No model calls.</p><button id="swarm-team-button">Team</button>
<label>Ordinary chat draft<textarea id="ordinary-draft">Keep this draft</textarea></label></main><script src="/swarm_view.js"></script><script>
window.fixture = {enabled:false,run:null,events:[],requests:[],held:[],hold:false,sequence:0,runCount:0};
class FixtureApp extends LumiSwarmView {
 constructor(){super();this.currentCwd='D:/fixture/project';this.currentSessionId='saved-session';this.ws={readyState:1};this.bindSwarmPanel();}
 send(request){fixture.requests.push(structuredClone(request));let response=fixture.respond(request);if(fixture.hold)fixture.held.push(response);else setTimeout(()=>this.receiveSwarmState(response),10);}
}
fixture.respond=function(request){
 if(request.action==='configure')this.enabled=request.enabled;
 if(request.action==='start'){
  const tasks=request.tasks||[];
  this.run={run:{id:++this.runCount===1?'run-fixture':'run-fixture-'+this.runCount,revision:1,state:'running',objective:request.objective,request_limit:request.request_limit,
   policy_json:JSON.stringify({max_workers:request.max_workers}),worker_limit:request.max_workers},remaining_requests:4,
   work_items:tasks.map((task,i)=>({id:'task-'+i,objective:task.objective,state:'running'})),
   attempts:tasks.map((task,i)=>({id:'attempt-'+i,work_item_id:'task-'+i,epoch:1,state:'running',process_state:'running',
    grant_json:JSON.stringify({model:{provider:'ollama',model:'explicit-fixture-model'},read_roots:task.read_roots,write_roots:[]})})),
   reservations:tasks.map(()=>({state:'reserved',amount:4,used:null})),model_requests:[]};
  if(request.plan_mode==='coordinator'){
   this.run.attempts=[{id:'coordinator-1',kind:'coordinator',epoch:1,state:'running',process_state:'running'}];
   this.run.coordinator_proposals=[{id:'proposal-1',attempt_id:'coordinator-1',state:'pending',sha256:'a'.repeat(64),payload_json:JSON.stringify({plan:{summary:'Inspect <b>quoted fields</b> safely.',use_team:false,
    work_items:[{id:'planned-1',objective:'Inspect CSV',role:'explore',read_roots:['src'],write_roots:[],dependencies:[],criteria:['owner_review']}]}})}];
  }
 }
 if(this.run&&['pause','resume','stop','recover'].includes(request.action)){
  this.run.run.revision++;this.run.run.state={pause:'paused',resume:'running',stop:'stopping',recover:'paused'}[request.action];
  if(request.action==='recover'){this.run.run.state='recovery_required';this.run.recovery={owns_lease:true,requested_epoch:1,acquired_epoch:2};}
  if(request.action==='stop')this.run.run.stop_requested=1;
 }
 if(request.action==='inspect_process')this.run.recovery_observations=[{attempt_id:request.attempt_id,observation:this.processObservation||'unknown',reason:'Fixture host observation'}];
 if(request.action==='reconcile_process'){
  this.run.run.revision++;this.run.attempts.find(row=>row.id===request.attempt_id).process_state='stopped';
  this.run.process_observations.forEach(row=>row.state='stopped');
 }
 if(request.action==='reconcile_request'){
  this.run.run.revision++;Object.assign(this.run.model_requests.find(row=>row.id===request.model_request_id),{state:request.outcome,used:request.used});
 }
 if(request.action==='reconcile_action'){
  this.run.run.revision++;this.run.action_receipts.find(row=>row.id===request.action_id).state='completed';
 }
 if(['reconcile_request','reconcile_action'].includes(request.action)&&this.run.model_requests.every(row=>row.state!=='uncertain')&&this.run.action_receipts.every(row=>row.state==='completed')){
  this.run.reservations.forEach(row=>row.state='settled');this.run.attempts[0].state='failed';this.run.work_items[0].state='failed';
 }
 if(request.action==='continue_recovered'){
  this.run.run.revision++;this.run.run.state=this.run.run.stop_requested?'cancelled':'running';this.run.recovery_needed=false;
  for(const id of request.retry_work_items)this.run.work_items.find(row=>row.id===id).state='ready';
 }
 if(request.action==='review_read_result'){
  this.run.run.revision++;this.run.work_items.find(item=>item.id===this.run.attempts.find(attempt=>attempt.id===request.attempt_id).work_item_id).state='accepted';
 }
 if(request.action==='reject_result'){
  this.run.run.revision++;const attempt=this.run.attempts.find(row=>row.id===request.attempt_id);attempt.state='failed';this.run.work_items.find(item=>item.id===attempt.work_item_id).state='failed';
 }
 if(request.action==='retry_work'){this.run.run.revision++;this.run.work_items.find(item=>item.id===request.work_item_id).state='ready';}
 if(request.action==='set_concurrency'){
  if(this.bumpBeforeConcurrency){this.run.run.revision++;this.bumpBeforeConcurrency=false;}
  if(request.expected_revision!==this.run.run.revision)return {event:'swarm_state',request_id:request.request_id,project:request.project,session_id:request.session_id,error:'Run revision changed; refresh the snapshot before deciding again.'};
  if(!Number.isInteger(request.max_workers)||request.max_workers<1||request.max_workers>JSON.parse(this.run.run.policy_json).max_workers)return {event:'swarm_state',request_id:request.request_id,project:request.project,session_id:request.session_id,error:'Worker limit exceeds the original policy.'};
  this.run.run.revision++;this.run.run.worker_limit=request.max_workers;
 }
 if(['pause_worker','resume_worker','cancel_worker','steer_worker'].includes(request.action)){
  this.run.run.revision++;const attempt=this.run.attempts.find(row=>row.id===request.attempt_id);
  this.run.workers ||= this.run.attempts.map(row=>({attempt_id:row.id,state:'running',alive:true,termination_recorded:false}));
  const worker=this.run.workers.find(row=>row.attempt_id===attempt.id);
  if(request.action==='pause_worker'){attempt.pause_requested=1;worker.state='pausing';}
  if(request.action==='resume_worker'){attempt.pause_requested=0;worker.state='running';}
  if(request.action==='cancel_worker'){attempt.cancel_requested=1;worker.state='stopping';}
  if(request.action==='steer_worker'){this.run.owner_directives ||= [];this.run.owner_directives.push({id:'directive-'+this.run.owner_directives.length,attempt_id:attempt.id,epoch:request.attempt_epoch,text:request.text});}
 }
 if(request.action==='complete'){this.run.run.revision++;this.run.run.state='completed';}
 if(request.action==='decide_proposal'){this.run.run.revision++;this.run.coordinator_proposals[0].state=request.accept?'accepted':'rejected';}
 if(!['view','events'].includes(request.action))this.events.push({sequence:++this.sequence,kind:'command_'+request.action});
 return {event:'swarm_state',request_id:request.request_id,project:request.project,session_id:request.session_id,
  available:true,enabled:this.enabled,model:{provider:'ollama',model:'explicit-fixture-model'},run:structuredClone(this.run),
  events:this.events.filter(event=>event.sequence>(request.after||0)),
  ...(request.action==='history'?{history:{items:this.run?[{run_id:this.run.run.id,objective:this.run.run.objective,state:this.run.run.state}]:[],next_before_run_id:null}}:{}),
  ...(request.action==='read_artifact'?{artifact_page:{run_id:request.run_id,artifact:{...this.run.artifact_refs.find(row=>row.id===request.artifact_id),size:35,origin:'tool_result'},
   format:'text',offset:request.offset||0,text:'Exact retained <b>observation</b>.',total_characters:35,next_offset:null,verified_sha256:'a'.repeat(64),message:'Complete fixture content verified.'}}:{}),
  ...(request.action==='export_report'?{report:{kind:'simulated-run-report',run_id:request.run_id,requests:{known:2,uncertain:1}}}:{})};
};
fixture.release=function(index,overrides={}){const event=this.held.splice(index,1)[0];app.receiveSwarmState({...event,...overrides});};
fixture.connection=function(online){app.ws.readyState=online?1:3;app.swarmConnectionChanged(online);};
window.app=new FixtureApp();
</script></html>`;

test('Team panel: real controls, scoped races, keyboard, reconnect and compact layout', {timeout: 60000}, async () => {
    const server = http.createServer((req, res) => {
        if (req.url === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(fixture); return; }
        const name = req.url.slice(1);
        if (!['styles.css', 'swarm_view.css', 'swarm_view.js'].includes(name)) { res.writeHead(404); res.end(); return; }
        res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : 'text/css');
        res.end(fs.readFileSync(path.join(assets, name)));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
        ? {headless:true, executablePath:process.env.SWARM_BROWSER_EXECUTABLE}
        : {headless:true, channel:'msedge'});
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'sonn-swarm-browser-'));
    let page;
    try {
        page = await browser.newPage({viewport:{width:1100,height:900}});
        const errors = [];
        page.on('pageerror', error => { errors.push(error.message); console.error('Browser script error:', error.message); });
        await page.goto('http://127.0.0.1:' + server.address().port);
        await page.getByRole('button', {name:'Team', exact:true}).click();
        const dialog = page.getByRole('dialog', {name:'Work together'});
        await page.getByText('ollama · explicit-fixture-model', {exact:true}).waitFor();
        assert.equal(await page.getByRole('button', {name:'Start read-only team'}).isEnabled(), false);
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(() => app._swarmState?.enabled === true);
        await page.getByLabel('Team objective').fill('Investigate the import pipeline');
        const tasks = page.locator('[data-task-objective]');
        await tasks.nth(0).fill('Check backend parsing');
        await tasks.nth(1).fill('Check frontend filtering');
        await page.locator('[data-task-roots]').nth(0).fill('src/backend');
        await page.locator('[data-task-roots]').nth(1).fill('src/frontend');

        // Delay two service responses and deliver out of order. Actual refresh
        // controls and typing remain usable while backend responses are pending.
        await page.evaluate(() => { fixture.hold=true; });
        await page.getByRole('button', {name:'Refresh team'}).click();
        await page.getByRole('button', {name:'Refresh team'}).click();
        await page.getByLabel('Team objective').focus();
        await page.getByLabel('Team objective').press('End');
        await page.getByLabel('Team objective').pressSequentially(' carefully');
        await page.evaluate(() => fixture.release(1));
        assert.equal(await page.getByLabel('Team objective').inputValue(), 'Investigate the import pipeline carefully');
        assert.equal(await page.getByLabel('Team objective').evaluate(node => document.activeElement === node), true);
        await page.evaluate(() => { fixture.release(0, {model:{provider:'wrong',model:'stale-response'}}); fixture.hold=false; });
        assert.equal(await page.getByText('wrong · stale-response', {exact:true}).count(), 0);

        await page.getByRole('button', {name:'Add investigation'}).click();
        await page.getByRole('button', {name:'Start read-only team'}).click();
        assert.equal(await page.evaluate(() => fixture.requests.filter(r=>r.action==='start').length), 0);
        await page.getByLabel('Worker slots').selectOption('3');
        await page.getByRole('button', {name:'Remove investigation 3'}).click();
        for (const invalid of ['1', '1001']) {
            await page.getByLabel('Total model requests').fill(invalid);
            await page.getByRole('button', {name:'Start read-only team'}).click();
            assert.equal(await page.evaluate(() => fixture.requests.filter(r=>r.action==='start').length), 0);
        }
        await page.getByLabel('Total model requests').fill('12');
        // A real packaged startup/discovery event changed separators after the
        // panel opened, incorrectly disabling Start for the same conversation.
        await page.evaluate(() => {app.currentCwd='d:\\FIXTURE\\project\\';app._renderSwarmControls();});
        assert.equal(await page.getByRole('button', {name:'Start read-only team'}).isEnabled(),true);
        await page.evaluate(() => {app.currentCwd='D:/fixture/different';app._renderSwarmControls();});
        assert.equal(await page.getByRole('button', {name:'Start read-only team'}).isEnabled(),false);
        await page.evaluate(() => {app.currentCwd='D:/fixture/project';app._renderSwarmControls();});
        await page.getByRole('button', {name:'Start read-only team'}).click();
        await page.getByRole('heading', {name:'Selected team'}).waitFor();
        const start = await page.evaluate(() => fixture.requests.find(request=>request.action==='start'));
        assert.equal(start.max_workers, 3);
        assert.equal(start.request_limit, 12);
        assert.equal(start.tasks.length, 2);
        assert.deepEqual(start.tasks[0].read_roots, ['src/backend']);
        await page.getByText('Worker 1 · Working', {exact:true}).click();
        await page.getByText('Check backend parsing', {exact:true}).waitFor();
        // Disabling future teams does not stop or hide the active team.
        await page.getByLabel('Enable team preview').uncheck();
        await page.waitForFunction(() => app._swarmState?.enabled === false);
        assert.equal(await page.getByRole('button', {name:'Pause new work'}).isEnabled(), true);
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(() => app._swarmState?.enabled === true);
        await page.getByRole('button', {name:'Pause new work'}).click();
        await page.getByRole('button', {name:'Resume', exact:true}).waitFor();
        await page.getByRole('button', {name:'Resume', exact:true}).click();
        await page.getByRole('button', {name:'Pause new work'}).waitFor();
        const controls = await page.evaluate(() => fixture.requests.filter(r=>['pause','resume'].includes(r.action)));
        assert.deepEqual(controls.map(r=>r.expected_revision), [1,2]);
        const limit=page.getByLabel('Active worker limit',{exact:true});
        assert.deepEqual(await limit.locator('option').evaluateAll(rows=>rows.map(row=>row.value)),['1','2','3']);
        await limit.selectOption('1');
        await limit.focus();
        await page.waitForTimeout(1150);
        assert.equal(await limit.inputValue(),'1');
        assert.equal(await limit.evaluate(node=>document.activeElement===node),true);
        assert.equal(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='set_concurrency').length),0);
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).click();
        await page.getByText('2 active workers · Assignment limit: 1 of 3 allowed · 0 active coordinators',{exact:true}).waitFor();
        await limit.selectOption('2');
        await page.evaluate(()=>{fixture.bumpBeforeConcurrency=true;});
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).click();
        await page.getByText('Run revision changed; refresh the snapshot before deciding again.',{exact:true}).waitFor();
        assert.equal(await limit.inputValue(),'2');
        assert.equal(await page.evaluate(()=>fixture.run.run.worker_limit),1);
        await page.getByRole('button',{name:'Refresh team',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='set_concurrency').length),2,'Refresh must not retry a rejected change');
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).click();
        await page.getByText('2 active workers · Assignment limit: 2 of 3 allowed · 0 active coordinators',{exact:true}).waitFor();
        await page.getByRole('button',{name:'Pause new work',exact:true}).click();
        await page.getByRole('button',{name:'Resume',exact:true}).waitFor();
        await limit.selectOption('3');
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).click();
        await page.getByText('2 active workers · Assignment limit: 3 of 3 allowed · 0 active coordinators',{exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>fixture.run.run.state),'paused');
        await page.getByRole('button',{name:'Resume',exact:true}).click();
        await page.getByRole('button',{name:'Pause new work',exact:true}).waitFor();
        assert.ok(await page.evaluate(()=>fixture.run.attempts.every(row=>row.state==='running')));
        const worker1=page.locator('.swarm-worker').filter({has:page.locator('summary').filter({hasText:'Worker 1 ·'})});
        await page.getByRole('button',{name:'Pause Worker 1',exact:true}).click();
        await worker1.getByText('Pause requested. Waiting for the current activity to reach a checkpoint.',{exact:true}).waitFor();
        assert.equal(await page.getByText('Worker 2 · Working',{exact:true}).count(),1);
        assert.equal(await worker1.getByText('Paused at an observed checkpoint.',{exact:true}).count(),0);
        await page.getByLabel('Guidance for Worker 1',{exact:true}).fill('Inspect the quoted-field edge case within the existing task.');
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Guidance for Worker 1',{exact:true}).evaluate(node=>document.activeElement===node),true);
        await page.getByRole('button',{name:'Send guidance to Worker 1',exact:true}).click();
        await worker1.getByText('Queued for this participant',{exact:true}).waitFor();
        assert.equal(await page.getByLabel('Guidance for Worker 1',{exact:true}).inputValue(),'');
        assert.equal(await worker1.getByText('Included in prepared model input',{exact:true}).count(),0);
        await page.evaluate(()=>{fixture.run.workers[0].state='paused';});
        await worker1.getByText('Paused at an observed checkpoint.',{exact:true}).waitFor();
        await page.getByRole('button',{name:'Pause new work',exact:true}).click();
        await page.getByRole('button',{name:'Resume',exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Resume Worker 1',exact:true}).isEnabled(),false);
        await page.getByRole('button',{name:'Resume',exact:true}).click();
        await page.getByRole('button',{name:'Resume Worker 1',exact:true}).click();
        await page.getByText('Worker 1 · Working',{exact:true}).waitFor();
        await page.evaluate(()=>{fixture.run.owner_directive_receipts=[{directive_id:'directive-0',attempt_id:'attempt-0',epoch:1,request_id:'prepared-request',input_sha256:'b'.repeat(64),input_revision:2}];});
        await worker1.getByText('Included in prepared model input',{exact:true}).waitFor();
        await page.getByText('Worker 2 · Working',{exact:true}).click();
        const worker2=page.locator('.swarm-worker').filter({has:page.locator('summary').filter({hasText:'Worker 2 ·'})});
        await page.getByRole('button',{name:'Stop Worker 2',exact:true}).click();
        await worker2.getByText('Stop requested. Termination is not yet confirmed.',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Stop Worker 2',exact:true}).isEnabled(),false);
        assert.equal(await page.getByText('Worker 1 · Working',{exact:true}).count(),1);
        const individual=await page.evaluate(()=>fixture.requests.filter(row=>['pause_worker','resume_worker','cancel_worker','steer_worker'].includes(row.action)));
        assert.ok(individual.every(row=>row.run_id==='run-fixture'&&row.attempt_epoch===1&&Number.isInteger(row.expected_revision)));
        assert.equal(individual.find(row=>row.action==='steer_worker').attempt_id,'attempt-0');
        assert.equal(individual.find(row=>row.action==='cancel_worker').attempt_id,'attempt-1');

        // Escape closes the native modal and returns keyboard focus to Team.
        await page.evaluate(()=>{window.previousTeamDialog=app._swarmDialog;});
        await page.keyboard.press('Escape');
        await dialog.waitFor({state:'detached'});
        assert.equal(await page.getByRole('button', {name:'Team',exact:true}).evaluate(node=>document.activeElement===node), true);
        await page.keyboard.press('Enter');
        await page.getByRole('heading', {name:'Selected team'}).waitFor();
        await page.evaluate(()=>previousTeamDialog.dispatchEvent(new Event('close')));
        assert.equal(await page.evaluate(()=>app._swarmDialog?.open),true,'A late close event cannot clear the reopened panel');
        await page.getByText('Worker 1 · Working', {exact:true}).click();
        await page.setViewportSize({width:390,height:844});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1), true);
        const bounds = await dialog.boundingBox();
        assert.ok(bounds.x>=0 && bounds.x+bounds.width<=391);
        await page.screenshot({path:path.join(output,'team-compact.png')});

        // Selection changes never retarget the captured panel or its commands.
        await page.evaluate(()=>{app.currentCwd='D:/different';app.currentSessionId='different-session';});
        await page.getByRole('button', {name:'Refresh team'}).click();
        await page.getByText(/Viewing the originally opened conversation/).waitFor();
        await page.evaluate(()=>fixture.connection(false));
        assert.equal(await page.getByRole('button',{name:'Stop team'}).isEnabled(),false);
        await page.evaluate(()=>fixture.connection(true));
        await page.waitForFunction(()=>!app._swarmPending);
        const latest = await page.evaluate(()=>fixture.requests.at(-1));
        assert.equal(latest.project,'D:/fixture/project');
        assert.equal(latest.session_id,'saved-session');
        assert.equal(latest.run_id,'run-fixture');
        assert.equal(latest.action,'view');
        assert.ok(Number.isInteger(latest.after));
        await page.getByRole('button',{name:'Stop team'}).click();
        await page.getByText('Stopping',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Apply worker limit',exact:true}).isEnabled(),false);
        assert.equal(await page.getByText('Complete',{exact:true}).count(),0);

        await page.evaluate(()=>{
            fixture.run.run.state='running';fixture.run.run.revision++;fixture.run.recovery_needed=true;
            fixture.run.model_requests=[{state:'uncertain'}];
            fixture.run.action_receipts=[{state:'uncertain'}];
            fixture.run.work_items[0].state='submitted';
            fixture.run.attempts[0].state='submitted';
            fixture.run.submissions=[{attempt_id:'attempt-0',handoff:'Parsing handles quoted fields. <b>Untrusted text</b>',candidate_revision:'reader-revision'}];
            fixture.run.artifact_refs=[{attempt_id:'attempt-0',id:'evidence-1',label:'Parsing observation',kind:'text',sha256:'a'.repeat(64)}];
        });
        // The open panel follows durable state without user refresh.
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText(/1 model request needs reconciliation/).waitFor();
        await page.getByText(/1 tool action needs reconciliation/).waitFor();
        await page.getByText('Parsing handles quoted fields. <b>Untrusted text</b>',{exact:true}).waitFor();
        assert.equal(await page.getByLabel('Retained evidence for Worker 1').inputValue(),'evidence-1');
        await page.getByRole('button',{name:'Read evidence for Worker 1',exact:true}).click();
        await page.getByText('Exact retained <b>observation</b>.',{exact:true}).waitFor();
        assert.equal(await page.locator('[data-swarm="artifact-text"] b').count(),0);
        // An old disclosure response must not populate a newly opened panel,
        // even when its original run has just been replaced by another run.
        await page.evaluate(()=>{fixture.hold=true;fixture.beforeLate=structuredClone(fixture.run);app.currentCwd='D:/fixture/project';app.currentSessionId='saved-session';});
        await page.getByRole('button',{name:'Read evidence for Worker 1',exact:true}).click();
        await page.keyboard.press('Escape');
        await page.evaluate(()=>{fixture.hold=false;fixture.run.run.id='later-fixture-run';fixture.run.run.objective='Another retained team';});
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmScope.run_id==='later-fixture-run'&&!app._swarmPending);
        await page.evaluate(()=>fixture.release(0));
        assert.equal(await page.locator('[data-swarm="artifact-viewer"]').isVisible(),false);
        assert.equal(await page.evaluate(()=>app._swarmScope.run_id),'later-fixture-run');
        await page.keyboard.press('Escape');
        await page.evaluate(()=>{fixture.run=fixture.beforeLate;delete fixture.beforeLate;});
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmScope.run_id==='run-fixture'&&!app._swarmPending);
        assert.equal(await page.locator('.swarm-worker b').count(),0);
        assert.equal(await page.getByRole('button',{name:'Pause new work'}).isVisible(),false);
        await page.evaluate(()=>{fixture.run.run.lease_until=Date.now()/1000+3;});
        await page.getByRole('button',{name:'Refresh team',exact:true}).click();
        await page.getByText(/previous supervisor lease expires in about/).waitFor();
        assert.equal(await page.getByRole('button',{name:'Take over expired team'}).isEnabled(),false);
        await page.getByRole('button',{name:'Take over expired team'}).click();
        await page.getByText('This host owns recovery. Check execution, then reconcile each uncertain observation.',{exact:true}).waitFor();
        await page.evaluate(()=>{
            fixture.run.run.state='cancelled';fixture.run.run.revision++;
            app.currentCwd='D:/fixture/project';app.currentSessionId='saved-session';
        });
        await page.getByRole('button',{name:'New team',exact:true}).waitFor();
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Team objective').fill('Next independent investigation');
        await page.getByRole('button',{name:'Refresh team'}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.getByLabel('Team objective').inputValue(),'Next independent investigation');
        await page.getByRole('button',{name:'Back to saved team'}).click();
        await page.getByRole('button',{name:'New team',exact:true}).waitFor();
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.locator('[data-task-objective]').nth(0).fill('Inspect independent A');
        await page.locator('[data-task-objective]').nth(1).fill('Inspect independent B');
        await page.getByRole('button',{name:'Start read-only team'}).click();
        await page.waitForFunction(()=>app._swarmScope.run_id==='run-fixture-2');
        const secondStart=await page.evaluate(()=>fixture.requests.filter(r=>r.action==='start').at(-1));
        assert.equal(secondStart.run_id,undefined);
        await page.evaluate(()=>{
            fixture.run.run.revision++;
            fixture.run.attempts.forEach(attempt=>{attempt.state='submitted';attempt.process_state='stopped';});
            fixture.run.work_items.forEach(item=>{item.state='submitted';});
            fixture.run.submissions=fixture.run.attempts.map(attempt=>({attempt_id:attempt.id,handoff:'Reviewed fixture finding. '.repeat(400)+'Final limitation: follow-up needed.',candidate_revision:'candidate-'+attempt.id}));
        });
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await page.getByText(/Final limitation: follow-up needed/).first().waitFor();
        const reviewOne=page.getByRole('form',{name:'Review Worker 1',exact:true});
        await reviewOne.getByRole('button',{name:'Accept findings',exact:true}).click();
        assert.equal(await page.evaluate(()=>fixture.requests.filter(r=>r.action==='review_read_result').length),0);
        await page.getByLabel('Review notes for Worker 1').fill('Compared the finding with the retained observation.');
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Review notes for Worker 1').inputValue(),'Compared the finding with the retained observation.');
        assert.equal(await page.getByLabel('Review notes for Worker 1').evaluate(node=>document.activeElement===node),true);
        await reviewOne.getByRole('button',{name:'Accept findings',exact:true}).click();
        await page.getByText('Worker 1 · Accepted',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Complete team',exact:true}).isVisible(),false);
        await page.getByText('Worker 2 · Awaiting verification',{exact:true}).click();
        await page.getByLabel('Review notes for Worker 2').fill('The retained evidence supports this investigation.');
        await page.getByRole('form',{name:'Review Worker 2',exact:true}).getByRole('button',{name:'Accept findings',exact:true}).click();
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.getByText('Complete',{exact:true}).waitFor();
        const review=await page.evaluate(()=>fixture.requests.find(r=>r.action==='review_read_result'));
        assert.equal(review.attempt_id,'attempt-0');
        assert.equal(review.attempt_epoch,1);
        assert.equal(review.candidate_revision,'candidate-attempt-0');
        assert.equal(review.evidence,'Compared the finding with the retained observation.');
        assert.ok(Number.isInteger(review.expected_revision));
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Planning approach').selectOption('coordinator');
        await page.getByLabel('Planning approach').selectOption('manual');
        assert.equal(await page.locator('[data-task-objective]').first().inputValue(),'Inspect independent A');
        await page.getByLabel('Planning approach').selectOption('coordinator');
        await page.getByLabel('Team objective').fill('Ask for a bounded investigation plan');
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        await page.getByText('Inspect <b>quoted fields</b> safely.',{exact:true}).waitFor();
        assert.equal(await page.locator('.swarm-proposal b').count(),0);
        assert.equal(await page.getByRole('button',{name:'Approve plan',exact:true}).isEnabled(),false);
        await page.getByText('Coordinator · Working',{exact:true}).click();
        await page.getByRole('button',{name:'Pause Coordinator',exact:true}).click();
        await page.getByText('Coordinator · Pausing',{exact:true}).waitFor();
        await page.getByLabel('Guidance for Coordinator',{exact:true}).fill('Keep the proposed tasks within the configured read roots.');
        await page.getByRole('button',{name:'Send guidance to Coordinator',exact:true}).click();
        await page.getByRole('button',{name:'Resume Coordinator',exact:true}).click();
        await page.getByText('Coordinator · Working',{exact:true}).waitFor();
        const coordinatorGuidance=await page.evaluate(()=>fixture.requests.filter(row=>row.action==='steer_worker').at(-1));
        assert.equal(coordinatorGuidance.attempt_id,'coordinator-1');
        assert.equal(coordinatorGuidance.attempt_epoch,1);
        await page.evaluate(()=>{
            fixture.run.run.revision++;
            fixture.run.attempts[0].state='completed';fixture.run.attempts[0].process_state='stopped';
            fixture.run.scheduling={error:'Fixture host needs inspection',blocked:{'planned-1':'Request allowance is held'}};
        });
        await page.getByText('Scheduling needs attention: Fixture host needs inspection',{exact:true}).waitFor();
        await page.getByText('1 investigation is waiting: Request allowance is held',{exact:true}).waitFor();
        await page.getByLabel('Plan review notes').fill('Notes for the original proposal identity');
        await page.evaluate(()=>{fixture.run.run.revision++;fixture.run.coordinator_proposals[0].sha256='b'.repeat(64);});
        await page.waitForFunction(()=>document.querySelector('.swarm-proposal textarea').value==='');
        await page.getByLabel('Plan review notes').fill('Reject this exact plan until the retained host issue is resolved.');
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Plan review notes').evaluate(node=>document.activeElement===node),true);
        await page.getByRole('button',{name:'Reject plan',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.getByText('Plan rejected',{exact:true}).waitFor();
        const planDecision=await page.evaluate(()=>fixture.requests.find(r=>r.action==='decide_proposal'));
        assert.equal(planDecision.sha256,'b'.repeat(64));
        assert.equal(planDecision.proposal_id,'proposal-1');
        assert.equal(planDecision.accept,false);
        assert.equal(planDecision.expected_revision,6);
        await page.evaluate(()=>{
            fixture.run.run.revision++;
            fixture.run.attempts=Array.from({length:21},(_,i)=>({id:'many-'+i,kind:'worker',work_item_id:'many-work-'+i,epoch:1,state:'submitted',process_state:'stopped',
                grant_json:JSON.stringify({model:{provider:'ollama',model:'explicit-fixture-model'},read_roots:['src'],write_roots:[]})}));
            fixture.run.work_items=fixture.run.attempts.map(attempt=>({id:attempt.work_item_id,state:'submitted',objective:'Investigation '+attempt.id}));
            fixture.run.submissions=fixture.run.attempts.map(attempt=>({attempt_id:attempt.id,handoff:'A finding requiring review.',candidate_revision:'result-'+attempt.id}));
        });
        await page.getByText('Worker 21 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        assert.equal(await page.getByRole('form',{name:'Review Worker 1',exact:true}).isVisible(),true);
        await page.getByLabel('Review notes for Worker 1',{exact:true}).fill('Reviewed the first of the outstanding investigations.');
        await page.getByRole('form',{name:'Review Worker 1',exact:true}).getByRole('button',{name:'Accept findings',exact:true}).click();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor({state:'detached'});
        assert.equal(await page.getByText('Worker 21 · Awaiting verification',{exact:true}).count(),1,'Reviewing one task must not rename the others');
        await page.evaluate(()=>{
            fixture.run.run.revision++;fixture.run.run.state='running';fixture.run.run.stop_requested=0;
            fixture.run.recovery_needed=true;fixture.run.recovery=null;fixture.run.recovery_observations=[];
            fixture.run.coordinator_proposals=[];
            fixture.run.attempts=[{id:'recover-attempt',kind:'worker',work_item_id:'recover-work',epoch:1,state:'uncertain',process_state:'running'}];
            fixture.run.work_items=[{id:'recover-work',objective:'Interrupted CSV investigation',state:'uncertain'},{id:'ready-work',objective:'Previously ready investigation',state:'ready'}];
            fixture.run.model_requests=[{id:'recover-request',attempt_id:'recover-attempt',state:'uncertain',purpose:'main'}];
            fixture.run.action_receipts=[{id:'recover-action',attempt_id:'recover-attempt',tool_name:'file_read',state:'uncertain'}];
            fixture.run.reservations=[{attempt_id:'recover-attempt',state:'uncertain',amount:4,used:null}];
            fixture.run.process_observations=[{attempt_id:'recover-attempt',state:'started'}];
        });
        await page.getByRole('button',{name:'Take over expired team',exact:true}).click();
        await page.getByRole('button',{name:'Continue reviewed team',exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Continue reviewed team',exact:true}).isEnabled(),false);
        await page.evaluate(()=>{
            fixture.run.integration_checks=[{id:'interrupted-check',candidate_id:'old-candidate',state:'uncertain'}];
            fixture.run.integration_operations=[{id:'interrupted-operation',kind:'run_check',state:'uncertain'}];
            fixture.run.integration_applications=[{id:'interrupted-application',state:'uncertain'}];
        });
        for(const [title,action,identity] of [
            ['Interrupted verification check','reconcile_effect','interrupted-check'],
            ['Interrupted integration operation','reconcile_operation','interrupted-operation'],
            ['Interrupted branch application','reconcile_application','interrupted-application'],
        ]) {
            const form=page.getByRole('form',{name:`${title}: ${identity}`,exact:true});
            await form.getByLabel('Recovery notes').fill('Inspect the exact retained host and effect evidence.');
            await page.waitForTimeout(1100);
            assert.equal(await form.getByLabel('Recovery notes').evaluate(node=>document.activeElement===node),true);
            assert.equal(await form.locator('select').count(),0,'Owner cannot choose an integration outcome');
            await form.getByRole('button',{name:'Inspect and reconcile',exact:true}).focus();
            await page.keyboard.press('Enter');
            await page.waitForFunction(()=>!app._swarmPending);
            const request=await page.evaluate(()=>fixture.requests.at(-1));
            assert.equal(request.action,action);
            assert.ok(Number.isInteger(request.expected_revision));
            assert.equal(request.effect_id||request.operation_id||request.approval_id,identity);
            assert.ok(!('outcome' in request)&&!('pid' in request));
        }
        await page.evaluate(()=>{fixture.run.integration_checks=[];fixture.run.integration_operations=[];fixture.run.integration_applications=[];});
        const processCard=page.locator('.swarm-recovery-record').filter({has:page.getByRole('heading',{name:'Worker 1 execution',exact:true})});
        await processCard.getByRole('button',{name:'Check process',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await processCard.getByRole('button',{name:'Record host observation',exact:true}).isEnabled(),false);
        await page.evaluate(()=>{fixture.processObservation='stopped';});
        await processCard.getByRole('button',{name:'Check process',exact:true}).click();
        await page.getByText('The host observed that the captured process stopped. Record that observation before continuing.',{exact:true}).waitFor();
        const accounting=page.getByRole('form',{name:'Worker 1 request accounting',exact:true});
        assert.equal(await accounting.getByRole('button',{name:'Record request accounting'}).isEnabled(),false);
        await processCard.getByRole('button',{name:'Record host observation',exact:true}).click();
        await page.getByText('Termination recorded.',{exact:true}).waitFor();
        assert.equal(await accounting.getByLabel('Observed outcome').inputValue(),'');
        await accounting.getByRole('button',{name:'Record request accounting'}).click();
        assert.equal(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='reconcile_request').length),0);
        await accounting.getByLabel('Observed outcome').selectOption('failed');
        await accounting.getByLabel('Observation evidence').fill('Provider receipt establishes one failed request; usage was incurred.');
        await page.waitForTimeout(1150);
        assert.equal(await accounting.getByLabel('Observation evidence').evaluate(node=>document.activeElement===node),true);
        await accounting.getByRole('button',{name:'Record request accounting'}).click();
        await accounting.waitFor({state:'detached'});
        const toolOutcome=page.getByRole('form',{name:'Worker 1 tool outcome',exact:true});
        await toolOutcome.getByLabel('Observed outcome').selectOption('not_started');
        await toolOutcome.getByLabel('Observation evidence').fill('Host trace confirms the admitted read was never invoked.');
        await toolOutcome.getByRole('button',{name:'Record tool outcome'}).click();
        await toolOutcome.waitFor({state:'detached'});
        await page.getByText('Already ready to run: Previously ready investigation',{exact:true}).waitFor();
        assert.equal(await page.getByLabel('Retry: Interrupted CSV investigation',{exact:true}).isChecked(),false);
        await page.getByLabel('Retry: Interrupted CSV investigation',{exact:true}).check();
        await page.getByLabel('Recovered worker request allowance').fill('3');
        await page.getByRole('button',{name:'Continue reviewed team',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.getByRole('button',{name:'Pause new work',exact:true}).waitFor();
        const reconciled=await page.evaluate(()=>fixture.requests.filter(row=>['inspect_process','reconcile_process','reconcile_request','reconcile_action','continue_recovered'].includes(row.action)));
        assert.ok(reconciled.every(row=>row.run_id==='run-fixture-3'&&Number.isInteger(row.expected_revision)));
        assert.ok(reconciled.every(row=>!('pid' in row)&&!('host_id' in row)));
        assert.equal(reconciled.find(row=>row.action==='reconcile_request').used,1);
        assert.deepEqual(reconciled.at(-1).retry_work_items,['recover-work']);
        assert.equal(reconciled.at(-1).worker_requests,3);
        await page.evaluate(()=>{
            fixture.run.run.revision++;fixture.run.run.state='recovery_required';fixture.run.recovery_needed=true;
            fixture.run.work_items[0].state='failed';
        });
        await page.getByLabel('Retry: Interrupted CSV investigation',{exact:true}).check();
        await page.getByRole('button',{name:'Stop team',exact:true}).click();
        await page.getByRole('button',{name:'Finish stopped team',exact:true}).waitFor();
        assert.equal(await page.getByLabel('Retry: Interrupted CSV investigation',{exact:true}).isChecked(),false);
        assert.equal(await page.getByLabel('Retry: Interrupted CSV investigation',{exact:true}).isEnabled(),false);
        await page.getByRole('button',{name:'Finish stopped team',exact:true}).click();
        await page.getByRole('button',{name:'New team',exact:true}).waitFor();
        assert.deepEqual(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='continue_recovered').at(-1).retry_work_items),[]);
        await page.evaluate(()=>{
            fixture.run={run:{...fixture.run.run,state:'running',stop_requested:0,revision:fixture.run.run.revision+1},remaining_requests:8,
                work_items:[{id:'write-work',state:'submitted',objective:'Implement the CSV export'}],
                attempts:[{id:'write-attempt',work_item_id:'write-work',epoch:3,state:'submitted',process_state:'stopped',grant_json:JSON.stringify({model:{provider:'ollama',model:'explicit-fixture-model'},read_roots:['src'],write_roots:['src']})}],
                submissions:[{attempt_id:'write-attempt',handoff:'Writer result requires exact checks.',candidate_revision:'result-writer'}],
                writer_setup:{base_revision:'base',target_branch:'refs/heads/main',write_roots:['src'],checks:[]},
                writer_worktrees:[{id:'writer-1',attempt_id:'write-attempt',state:'ready',result_revision:'result-writer',manifest_json:JSON.stringify({changed_paths:['src/export.py']})}],
                integration_candidates:[{id:'candidate-1',state:'verified',base_revision:'base',result_revision:'target',manifest_json:JSON.stringify({writers:[{attempt_id:'write-attempt'}],checks:[]})}],
                candidate_details:[{candidate_id:'candidate-1',state:'ready',base_revision:'base',result_revision:'target',changed_paths:['src/export.py'],diff:'+ fixture',diff_truncated:true,changed_paths_truncated:false,errors:[]}],
                integration_operations:[],reservations:[],model_requests:[]};
        });
        await page.getByRole('button',{name:'Inspect candidate',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        await page.getByText('Review changed files and diff',{exact:true}).click();
        await page.getByText('The preview is truncated. Applying is unavailable because this preview cannot show the complete retained changes.',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).isEnabled(),false);
        await page.evaluate(()=>{fixture.run.candidate_details[0].diff_truncated=false;fixture.run.candidate_details[0].changed_paths_truncated=true;});
        await page.getByRole('button',{name:'Refresh team',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).isEnabled(),false);
        await page.evaluate(()=>{fixture.run.candidate_details[0].changed_paths_truncated=false;fixture.run.candidate_details[0].result_revision='stale';});
        await page.getByRole('button',{name:'Refresh team',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).isEnabled(),false);
        await page.evaluate(()=>{fixture.run.candidate_details[0].result_revision='target';});
        await page.getByRole('button',{name:'Refresh team',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).isEnabled(),true);
        const inspection=await page.evaluate(()=>fixture.requests.filter(row=>row.action==='inspect_candidate').at(-1));
        assert.equal(inspection.candidate_id,'candidate-1');
        assert.equal(inspection.run_id,'run-fixture-3');
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await page.getByRole('form',{name:'Reject Worker 1',exact:true}).getByRole('button',{name:'Reject result',exact:true}).click();
        assert.equal(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='reject_result').length),0);
        await page.getByLabel('Rejection notes for Worker 1',{exact:true}).fill('The change needs a missing edge case before another candidate.');
        await page.getByRole('form',{name:'Reject Worker 1',exact:true}).getByRole('button',{name:'Reject result',exact:true}).click();
        await page.getByText('Worker 1 · Needs review',{exact:true}).waitFor();
        await page.getByLabel('Retry notes for Worker 1',{exact:true}).fill('Try the same bounded contract with the review notes retained.');
        await page.getByRole('form',{name:'Retry Worker 1',exact:true}).getByRole('button',{name:'Retry task',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        assert.equal(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='reject_result').at(-1).attempt_epoch),3);
        assert.equal(await page.evaluate(()=>fixture.requests.filter(row=>row.action==='retry_work').at(-1).work_item_id),'write-work');
        const downloadPromise=page.waitForEvent('download');
        await page.getByRole('button',{name:'Export run report',exact:true}).click();
        const download=await downloadPromise;
        assert.equal(download.suggestedFilename(),'lumi-team-run-fixture-3.json');
        const reportPath=path.join(output,download.suggestedFilename());
        await download.saveAs(reportPath);
        assert.deepEqual(JSON.parse(fs.readFileSync(reportPath,'utf8')),{kind:'simulated-run-report',run_id:'run-fixture-3',requests:{known:2,uncertain:1}});
        await page.getByRole('button',{name:'Close team panel'}).click();
        const closedRequests=await page.evaluate(()=>fixture.requests.length);
        await page.waitForTimeout(1150);
        assert.equal(await page.evaluate(()=>fixture.requests.length),closedRequests);
        assert.equal(await page.getByLabel('Ordinary chat draft').inputValue(),'Keep this draft');
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'), JSON.stringify({
            kind:'simulated-service-browser-controls', providers_called:false,
            scenarios:['setup','stale responses','focus preservation','task slot validation','worker inspection',
                'pause/resume','Escape/Enter focus','390px layout','captured scope','reconnect/cursor','stop','recovery','submitted findings',
                'request bounds','active disable','automatic polling','new team','poll cleanup','explicit owner review','review draft focus','complete team',
                'coordinator setup','proposal cleanup gate','untrusted plan text','proposal identity binding','scheduling attention','plan rejection','all outstanding reviews reachable',
                'recovery takeover','trusted process checks','unknown accounting retained','explicit accounting/action evidence','selected retries and ready work',
                'candidate inspection identity','truncated diff/path and stale revision gates','explicit rejection/retry notes','local metadata report download',
                'explicit worker limit','original cap retained','paused adjustment','stale adjustment requires new decision','worker limit draft focus',
                'individual pause checkpoint','global pause dominates resume','guidance draft/receipt stages','individual cancel preserves peer'],
            requests:await page.evaluate(()=>fixture.requests),
        },null,2));
        console.log('Simulated browser evidence:', output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>({requests:fixture.requests,pending:app._swarmPending,scope:app._swarmScope,state:app._swarmState})).catch(()=>({})),null,2));
        }
        console.error('Simulated browser failure evidence:',output);
        throw error;
    } finally {
        await browser.close();
        await new Promise(resolve=>server.close(resolve));
    }
});
