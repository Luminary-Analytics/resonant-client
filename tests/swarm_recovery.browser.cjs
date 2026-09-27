/* Actual source-app recovery after an isolated fixture host exits without cleanup.
 * All inference is scripted; seed observations are deliberate fault injections.
 * node tests/swarm_recovery.browser.cjs [absolute-path-to-playwright-module]
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

test('Source app recovers an exited fixture host through explicit browser decisions', {timeout:90000}, async()=>{
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-swarm-recovery-browser-'));
    const server=spawn(process.env.SWARM_PYTHON||'python',[path.join(__dirname,'fixtures/swarming_ui_server.py'),output,'--recovery'],
        {cwd:output,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    server.stdout.on('data',chunk=>{stdout+=chunk;});
    server.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for(let i=0;i<150;i++){
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Recovery fixture failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Recovery fixture ready metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(15000);
        const errors=[],commands=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>socket.on('framesent',frame=>{const data=JSON.parse(frame.payload);if(data.command==='swarm')commands.push(data);}));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);
        await page.waitForFunction(session=>app?.currentSessionId===session,info.session_id);
        await page.locator('#user-input').fill('Keep this draft through recovery');
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByRole('button',{name:'Take over expired team',exact:true}).click();
        await page.getByText('This host owns recovery. Check execution, then reconcile each uncertain observation.',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Continue reviewed team',exact:true}).isEnabled(),false);
        assert.equal((await (await fetch(info.url+'/__fixture__/evidence')).json()).backend_instances,0,'Takeover must not invoke a provider');
        assert.equal(await page.getByRole('button',{name:'Stop team',exact:true}).isEnabled(),true);
        for(const worker of [1,2]){
            const card=page.locator('.swarm-recovery-record').filter({has:page.getByRole('heading',{name:`Worker ${worker} execution`,exact:true})});
            await card.getByRole('button',{name:'Check process',exact:true}).click();
            await card.getByText('The host observed that the captured process stopped. Record that observation before continuing.',{exact:true}).waitFor();
            await card.getByRole('button',{name:'Record host observation',exact:true}).click();
            await card.getByText('Termination recorded.',{exact:true}).waitFor();
        }
        // The fixture trace is independent of the unknown ledger labels. It
        // identifies the deliberate stop before provider/read invocation.
        assert.match(fs.readFileSync(path.join(output,'interruption-trace.txt'),'utf8'),/before any provider invocation/);
        const accounting=page.getByRole('form',{name:'Worker 1 request accounting',exact:true});
        assert.equal(await accounting.getByLabel('Observed outcome').inputValue(),'');
        await accounting.getByRole('button',{name:'Record request accounting',exact:true}).click();
        assert.equal(commands.filter(command=>command.action==='reconcile_request').length,0);
        await accounting.getByLabel('Observed outcome').selectOption('not_started');
        await accounting.getByLabel('Observation evidence').fill('Reviewed isolated interruption-trace.txt and fixture seed: process exited before provider invocation.');
        await page.waitForTimeout(1150);
        assert.equal(await accounting.getByLabel('Observation evidence').evaluate(node=>document.activeElement===node),true);
        await page.setViewportSize({width:390,height:844});
        await accounting.getByRole('button',{name:'Record request accounting',exact:true}).scrollIntoViewIfNeeded();
        assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'source-app-recovery-compact.png')});
        await accounting.getByRole('button',{name:'Record request accounting',exact:true}).click();
        await accounting.waitFor({state:'detached'});
        assert.equal(commands.find(command=>command.action==='reconcile_request').used,0);
        const action=page.getByRole('form',{name:'Worker 2 tool outcome',exact:true});
        await action.getByLabel('Observed outcome').selectOption('not_started');
        await action.getByLabel('Observation evidence').fill('Reviewed fixture seed and interruption-trace.txt: admitted file_read was never invoked.');
        await action.getByRole('button',{name:'Record tool outcome',exact:true}).click();
        await action.waitFor({state:'detached'});
        await page.getByText('Already ready to run: Recovered CSV investigation 3',{exact:true}).waitFor();
        for(const worker of [1,2]){
            const checkbox=page.getByLabel(`Retry: Recovered CSV investigation ${worker}`,{exact:true});
            assert.equal(await checkbox.isChecked(),false);
            await checkbox.check();
        }
        await page.getByLabel('Recovered worker request allowance').fill('4');
        await page.getByRole('button',{name:'Continue reviewed team',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.getByRole('button',{name:'Pause new work',exact:true}).waitFor();
        const continuation=commands.find(command=>command.action==='continue_recovered');
        assert.deepEqual(continuation.retry_work_items,['recovery-work-1','recovery-work-2']);
        assert.equal(continuation.worker_requests,4);
        assert.ok(commands.filter(command=>['recover','inspect_process','reconcile_process','reconcile_request','reconcile_action','continue_recovered'].includes(command.action))
            .every(command=>command.run_id==='fixture-crashed-team'&&Number.isInteger(command.expected_revision)&&!('pid' in command)));
        await page.setViewportSize({width:1180,height:900});
        // The scheduler's third admission legitimately changes the revision.
        // Review only after the observed wave drains; do not replay stale edits.
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===3
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending);
        assert.equal(await page.getByText('Worker 1 · Needs review',{exact:true}).count(),1,'A historical failed attempt must not borrow its replacement outcome');
        for(const worker of [3,4,5]){
            await page.getByText(`Worker ${worker} · Awaiting verification`,{exact:true}).click();
            await page.getByLabel(`Review notes for Worker ${worker}`,{exact:true}).fill('Reviewed the resumed fixture finding and completed file read.');
            await page.getByRole('form',{name:`Review Worker ${worker}`,exact:true}).getByRole('button',{name:'Accept findings',exact:true}).click();
            await page.getByText(`Worker ${worker} · Accepted`,{exact:true}).waitFor();
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.locator('[data-swarm="run-state"]').filter({hasText:'Complete'}).waitFor();
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#user-input').inputValue(),'Keep this draft through recovery');
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        assert.equal(evidence.backend_instances,3);
        assert.equal(evidence.backend_requests,6);
        const run=evidence.runs[0].run;
        assert.equal(run.run.state,'completed');
        assert.equal(run.run.epoch,2);
        assert.equal(run.attempts.length,5);
        assert.equal(run.check_receipts.length,3);
        assert.ok(run.model_requests.some(row=>row.state==='not_started'&&row.used===0));
        assert.ok(run.action_receipts.some(row=>row.metadata_json?.includes('interruption-trace.txt')));
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({...evidence,kind:'source-app-injected-interruption-browser',commands},null,2));
        console.log('Source-app recovery browser evidence:',output);
    } catch(error){
        if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));}
        console.error('Recovery browser failure evidence:',output);throw error;
    } finally {
        if(browser)await browser.close();
        if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let timer;
        const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);})]);
        clearTimeout(timer);if(!result)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
