/* Source UI + real TLS/PostgreSQL + owned scripted worker/Git/check processes. */
// The app page needs a one-time launch code (lumi/gui/local_access.py); the
// fixture server mints one per page load.
const fixtureLaunch=async info=>(await (await fetch(info.url+'/__fixture__/launch')).json()).url;
const test=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[2]||'playwright');
const {managedGovernanceSkip}=require('./managed_governance.cjs');
const candidate=process.env.SWARM_PACKAGED_EXECUTABLE?path.resolve(process.env.SWARM_PACKAGED_EXECUTABLE):null;

test('Managed writer UI applies exact checked changes and requires separate acceptance', {timeout:120000, skip:managedGovernanceSkip()}, async()=>{
    assert.ok(process.env.SONN_GOVERNANCE_TEST_CONFIG);
    assert.ok(process.env.SWARM_MANAGED_PYTHON);
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-managed-writer-browser-'));
    const server=spawn(process.env.SWARM_MANAGED_PYTHON,[path.join(__dirname,candidate?'fixtures/swarming_packaged_managed_workflows.py':'fixtures/swarming_managed_ui_server.py'),
        output,process.env.SONN_GOVERNANCE_TEST_CONFIG,...(candidate?[candidate]:[]),'--writer'],{cwd:output,windowsHide:true,stdio:[candidate?'pipe':'ignore','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    const errors=[],commands=[],responses=[];
    server.stdout.on('data',chunk=>{stdout+=chunk;});server.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    const wait=()=>page.waitForFunction(()=>app._swarmState&&!app._swarmPending);
    try{
        for(let n=0;n<250;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Managed writer fixture failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Fixture did not become ready: '+stderr);
        const originalValue=fs.readFileSync(path.join(info.workspace,'src/value.txt'));
        const originalPersonal=fs.readFileSync(path.join(info.workspace,'personal.txt'));
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE?
            {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});page.setDefaultTimeout(25000);
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>{
            socket.on('framesent',frame=>{const value=JSON.parse(frame.payload);if(value.command==='swarm')commands.push(value);});
            socket.on('framereceived',frame=>{const value=JSON.parse(frame.payload);if(value.event==='swarm_state')responses.push(value);});
        });
        async function ownerAction(name,action){
            for(let attempt=0;attempt<2;attempt++){
                await wait();const offset=commands.length;
                await page.getByRole('button',{name,exact:true}).click();
                let sent,response;
                for(let n=0;n<100;n++){
                    sent=commands.slice(offset).find(row=>row.action===action);
                    response=sent&&responses.find(row=>row.request_id===sent.request_id);
                    if(response)break;
                    await page.waitForTimeout(50);
                }
                assert.ok(response,'No definite command response; an ambiguous action must not be replayed');
                if(!response.error)return;
                // Only a definite pre-admission revision rejection permits a
                // fresh owner decision after actual UI refresh. Never retry a
                // lost reply, uncertain effect, or failed invoked operation.
                assert.match(response.error,/^Run revision changed/);
                assert.equal(attempt,0,'A second changed revision needs separate investigation');
                await page.getByRole('button',{name:'Refresh team',exact:true}).click();await wait();
            }
        }
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        if(candidate){
            await page.waitForFunction(()=>window.app?.backends?.ollama?.models?.length);
            await page.locator(`.agent-row[data-session-id="${info.session_id}"] .session-title-text`).click();
        }
        await page.waitForFunction(id=>window.app?.currentSessionId===id,info.session_id);
        await page.locator('#user-input').fill('Preserved private ordinary conversation draft');
        await page.getByRole('button',{name:'Team',exact:true}).click();await wait();
        await page.getByLabel('Enable team preview').check();await wait();
        await page.getByLabel('Execution ownership').selectOption('managed');
        await page.waitForFunction(()=>app._swarmState?.execution_mode==='managed'&&!app._swarmPending);
        await page.getByLabel('Team objective').fill('Implement the isolated managed value change');
        await page.getByRole('button',{name:'Remove investigation 2',exact:true}).click();
        await page.getByLabel('Allow scoped file changes').check();
        await page.getByLabel('Writable project folders').fill('src');
        await page.getByLabel('Allowance per writer').fill('4');
        await page.getByLabel('Check name',{exact:true}).fill('value-check');
        await page.getByLabel('Executable',{exact:true}).fill(info.python);
        await page.getByLabel('Arguments, one per line').fill('verify_value.py');
        await page.getByLabel('Check timeout in seconds').fill('10');
        await page.locator('[data-task-objective]').fill('Update src/value.txt');
        await page.locator('[data-task-roots]').fill('src');
        await page.getByLabel('Assignment type').selectOption('implement');
        await page.getByLabel('Writable folders for this task').fill('src');
        await page.getByLabel('Required check names').fill('value-check');
        await page.locator('[data-swarm="slots"]').selectOption('1');
        await page.locator('[data-swarm="allowance"]').fill('6');
        await page.setViewportSize({width:390,height:844});
        const dialog=page.getByRole('dialog',{name:'Work together'});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.getByRole('button',{name:'Start scoped team',exact:true}).focus();
        await page.screenshot({path:path.join(output,'managed-writer-setup.png')});
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===1
            &&app._swarmState.run.attempts.every(row=>row.process_state==='stopped')&&!app._swarmPending);
        assert.equal(commands.filter(row=>row.action==='start').length,1);
        assert.equal(commands.find(row=>row.action==='start').execution_mode,'managed');
        assert.deepEqual(fs.readFileSync(path.join(info.workspace,'src/value.txt')),originalValue);
        assert.equal((await page.evaluate(()=>app._swarmState.run.process_observations))[0].state,'stopped');
        await page.locator('.swarm-writer-result input').check();
        await ownerAction('Prepare selected changes','prepare_candidate');
        await page.getByText('Ready for verification',{exact:true}).waitFor();
        await page.getByRole('button',{name:'Inspect candidate',exact:true}).click();
        await page.getByText('This complete diff belongs to the candidate revision shown above.',{exact:true}).waitFor({state:'attached'});
        await page.getByText('Review changed files and diff',{exact:true}).click();
        assert.match(await page.locator('[data-candidate-diff]').innerText(),/verified change/);
        await ownerAction('Run value-check','run_check');
        await page.getByText('Checks passed for this exact revision',{exact:true}).waitFor();
        await page.getByText('Check output',{exact:true}).click();
        await page.getByText('Exact managed writer value verified',{exact:true}).waitFor();
        await page.getByLabel('Application review notes').fill('Owner reviewed this exact managed diff and named check.');
        fs.writeFileSync(path.join(info.workspace,'personal.txt'),'unfinished personal work\n');
        await ownerAction('Apply reviewed changes','apply_candidate');
        await page.waitForFunction(()=>app._swarmState.run.integration_operations.some(row=>row.kind==='apply'&&row.state==='failed'));
        assert.equal(fs.readFileSync(path.join(info.workspace,'personal.txt'),'utf8'),'unfinished personal work\n');
        assert.deepEqual(fs.readFileSync(path.join(info.workspace,'src/value.txt')),originalValue);
        fs.writeFileSync(path.join(info.workspace,'personal.txt'),originalPersonal);
        await ownerAction('Apply reviewed changes','apply_candidate');
        await page.getByText('Changes applied; task acceptance remains separate',{exact:true}).waitFor();
        assert.equal((await page.evaluate(()=>app._swarmState.run.work_items))[0].state,'submitted');
        assert.equal(await page.getByRole('button',{name:'Complete team',exact:true}).count(),0);
        const form=page.getByRole('form',{name:'Accept applied result: Update src/value.txt',exact:true});
        await form.getByLabel('Acceptance notes').fill('Owner accepts the applied SHA and its exact named check receipt.');
        await ownerAction('Accept applied result','accept_writer');
        await form.waitFor({state:'hidden'});await wait();
        await ownerAction('Complete team','complete');
        await page.locator('[data-swarm="run-state"]').filter({hasText:'Complete'}).waitFor();
        await page.screenshot({path:path.join(output,'managed-writer-completed.png')});
        let evidence;
        for(let n=0;n<60;n++){
            evidence=await (await fetch((info.api_url||info.url)+'/__fixture__/evidence')).json();
            if(evidence.owner_effects.length&&evidence.owner_effects.every(row=>row.state==='completed'))break;
            await page.waitForTimeout(100);
        }
        if(candidate){
            evidence.runs=[await page.evaluate(()=>app._swarmState)];
            evidence.owned_child=evidence.owned_children===1;
            evidence.backend_instances=evidence.owned_children;
            evidence.backend_requests=evidence.requests.length;
            assert.equal(evidence.observed_children.length,1);
            assert.equal(path.resolve(evidence.observed_children[0].exe).toLowerCase(),candidate.toLowerCase());
            assert.equal(evidence.children.filter(row=>row.argv.includes('--swarm-worker')).length,0);
            const log=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
            assert.match(log,/Application startup complete/);assert.match(log,/WebSocket.*accepted/);
            for(const forbidden of ['Traceback (most recent call last)','BEGIN PRIVATE KEY','managed.json','first.key'])assert.equal(log.includes(forbidden),false);
        }
        assert.equal(evidence.owned_child,true);assert.equal(evidence.backend_instances,1);assert.equal(evidence.backend_requests,2);
        assert.equal(evidence.private_configuration_in_model,false);assert.equal(evidence.remote_contains_content,false);
        assert.deepEqual([...new Set(evidence.owner_effects.map(row=>row.kind))].sort(),
            ['candidate_check','candidate_git','checkout_apply','writer_git']);
        assert.ok(evidence.owner_effects.every(row=>row.state==='completed'));
        assert.equal(new Set(evidence.owner_effects.map(row=>row.semantics_sha256)).size,evidence.owner_effects.length);
        const final=evidence.runs[0].run;
        assert.equal(final.run.state,'completed');assert.equal(final.writer_acceptances.length,1);
        assert.equal(final.integration_checks[0].state,'passed');
        assert.equal(final.integration_processes.length,evidence.owner_effects.length);
        assert.equal(fs.readFileSync(path.join(info.workspace,'src/value.txt'),'utf8').replace(/\r\n/g,'\n'),'verified change\n');
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({...evidence,candidate:info,kind:candidate?'frozen-managed-writer':'source-managed-writer',commands,responses:responses.filter(row=>row.error)},null,2));
        console.log('Managed writer browser evidence:',output);
    }catch(error){
        if(page){
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState).catch(()=>({})),null,2));
        }
        console.error('Managed writer browser failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();
        if(candidate)server.stdin.end();
        else if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);})]);
        clearTimeout(timer);if(!result)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
