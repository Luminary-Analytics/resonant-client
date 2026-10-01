/* Unmodified frozen app crash/restart with actual owned child, TLS and PG. */
const test=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[3]||'playwright');
const {managedGovernanceSkip}=require('./managed_governance.cjs');

test('Frozen managed restart requires proof before a new enforced epoch', {timeout:210000, skip:managedGovernanceSkip()}, async()=>{
    assert.ok(process.env.SONN_GOVERNANCE_TEST_CONFIG&&process.env.SWARM_MANAGED_PYTHON);
    const candidate=path.resolve(process.argv[2]);assert.ok(fs.existsSync(candidate));
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-packaged-managed-recovery-'));
    const fixture=spawn(process.env.SWARM_MANAGED_PYTHON,[path.join(__dirname,'fixtures/swarming_packaged_managed_workflows.py'),
        output,process.env.SONN_GOVERNANCE_TEST_CONFIG,candidate],{cwd:output,windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    const errors=[],commands=[];
    fixture.stdout.on('data',chunk=>{stdout+=chunk;});fixture.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>fixture.once('exit',(code,signal)=>resolve({code,signal})));
    const wait=()=>page.waitForFunction(()=>app._swarmState&&!app._swarmPending);
    async function control(name){const response=await fetch(info.api_url+'/__fixture__/'+name,{method:'POST'});assert.equal(response.status,200);return response.json();}
    async function evidence(){return (await fetch(info.api_url+'/__fixture__/evidence')).json();}
    async function openTeam(){
        await page.goto(info.url);
        await page.waitForFunction(()=>window.app?.backends?.ollama?.models?.length);
        await page.locator(`.agent-row[data-session-id="${info.session_id}"] .session-title-text`).click();
        await page.waitForFunction(id=>app.currentSessionId===id,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();await wait();
        await page.getByLabel('Enable team preview').check();await wait();
        await page.getByLabel('Execution ownership').selectOption('managed');
        await page.waitForFunction(()=>app._swarmState?.execution_mode==='managed'&&!app._swarmPending);
    }
    try{
        for(let n=0;n<400;n++){
            const line=stdout.split(/\r?\n/).find(row=>row.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(fixture.exitCode!==null)throw Error('Frozen recovery infrastructure failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Frozen fixture not ready: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE?
            {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});page.setDefaultTimeout(30000);
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>socket.on('framesent',frame=>{const row=JSON.parse(frame.payload);if(row.command==='swarm')commands.push(row);}));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await openTeam();
        await page.getByLabel('Team objective').fill('Recover the independently observed managed file investigation');
        await page.getByRole('button',{name:'Remove investigation 2',exact:true}).click();
        await page.locator('[data-task-objective]').fill('Read fact');
        await page.locator('[data-task-roots]').fill('fact.txt');
        await page.locator('[data-swarm="slots"]').selectOption('1');
        await page.locator('[data-swarm="allowance"]').fill('6');
        await page.getByRole('button',{name:'Start read-only team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.some(row=>row.state==='started'));
        let initial;
        for(let n=0;n<100;n++){initial=await evidence();if(initial.requests.length)break;await page.waitForTimeout(50);}
        assert.equal(initial.requests.length,1);
        assert.equal(initial.observed_children.length,1);
        assert.equal(path.resolve(initial.observed_children[0].exe).toLowerCase(),candidate.toLowerCase());
        const runId=await page.evaluate(()=>app._swarmState.run.run.id);
        const crash=await control('crash');
        assert.equal(crash.host_exited,true);assert.equal(crash.children_stopped,true);
        assert.ok(crash.children.some(row=>row.argv.includes('--swarm-worker')));
        const restarted=await control('restart');assert.notEqual(restarted.host_pid,info.host_pid);
        await openTeam();
        // Wait for the captured durable lease itself; no clock or ledger edits.
        await page.getByRole('button',{name:'Take over expired team',exact:true}).waitFor({timeout:100000});
        await page.waitForFunction(()=>!document.querySelector('[data-swarm="recover"]')?.disabled,null,{timeout:100000});
        await page.getByRole('button',{name:'Take over expired team',exact:true}).click();
        await page.getByRole('heading',{name:'Organization recovery observations',exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Continue reviewed team',exact:true}).isEnabled(),false);
        const managed=page.locator('[data-swarm="managed-recovery"]');
        await page.getByLabel('Retained record type').selectOption('workers');
        await page.waitForFunction(()=>app._swarmState?.run?.managed_recovery?.kind==='workers'&&!app._swarmPending);
        await managed.getByRole('button',{name:'Derive and report retained observation',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.run?.managed_recovery?.worker_cleanup_pending===0&&!app._swarmPending);
        const request=page.getByRole('form',{name:'Worker 1 request accounting',exact:true});
        await request.getByLabel('Observed outcome').selectOption('failed');
        await request.getByLabel('Observation evidence').fill('The exact frozen app and owned child were killed after this request reached the loopback provider. Count the failed invocation; do not refund it.');
        await request.getByRole('button',{name:'Record request accounting',exact:true}).click();
        await request.waitFor({state:'detached'});
        await page.getByLabel('Retained record type').selectOption('requests');
        await page.waitForFunction(()=>app._swarmState?.run?.managed_recovery?.kind==='requests'&&!app._swarmPending);
        await managed.getByRole('button',{name:'Derive and report retained observation',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.managed_recovery?.unknown_request_units===0&&!app._swarmPending);
        await page.evaluate(()=>app.ws.close());
        await page.waitForFunction(()=>app.ws?.readyState===WebSocket.OPEN&&!app._swarmPending);
        await page.getByRole('button',{name:'Refresh team',exact:true}).click();await wait();
        assert.equal(await page.getByLabel('Retained record type').inputValue(),'requests');
        await page.setViewportSize({width:390,height:844});await managed.scrollIntoViewIfNeeded();
        assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'frozen-managed-recovery.png')});
        await page.getByLabel('Retry: Read fact',{exact:true}).check();
        await page.getByLabel('Recovered worker request allowance').fill('3');
        await control('release');
        await page.getByRole('button',{name:'Continue reviewed team',exact:true}).focus();await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===1&&app._swarmState.run.workers.every(row=>!row.alive)&&!app._swarmPending);
        const final=await page.evaluate(()=>app._swarmState), observed=await evidence();
        assert.equal(final.run.run.id,runId);assert.equal(final.run.run.epoch,2);assert.equal(final.execution_mode,'managed');
        assert.equal(observed.requests.length,3);assert.equal(observed.remote.runs.length,2);
        assert.equal(observed.observed_children.length,2);
        assert.ok(observed.observed_children.every(row=>path.resolve(row.exe).toLowerCase()===candidate.toLowerCase()));
        assert.equal(observed.remote_requests.filter(row=>row.state==='failed').length,1);
        assert.equal(observed.remote_requests.filter(row=>row.state==='completed').length,2);
        assert.equal(observed.private_configuration_in_model,false);assert.equal(observed.remote_contains_content,false);
        assert.ok(commands.filter(row=>row.action.startsWith('managed_')).every(row=>!('outcome'in row)&&!('pid'in row)&&!('evidence'in row)));
        assert.deepEqual(errors,[]);
        const log=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
        assert.equal((log.match(/Application startup complete/g)||[]).length,2);assert.match(log,/WebSocket.*accepted/);
        for(const forbidden of ['Traceback (most recent call last)','BEGIN PRIVATE KEY','managed.json','first.key'])assert.equal(log.includes(forbidden),false);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:'frozen-managed-killed-host-recovery',candidate:info,crash,restarted,observed,final,commands},null,2));
        console.log('Frozen managed recovery evidence:',output);
    }catch(error){
        if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState||{}).catch(()=>({})),null,2));}
        console.error('Frozen managed recovery failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();fixture.stdin.end();
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),15000);})]);
        clearTimeout(timer);if(!result)fixture.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
