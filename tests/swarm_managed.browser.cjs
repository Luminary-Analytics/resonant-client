/* Real source GUI, encrypted host channel, PostgreSQL and guarded native reader. */
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

async function runCase(hold){
    assert.ok(process.env.SONN_GOVERNANCE_TEST_CONFIG,'Explicit disposable PostgreSQL configuration required');
    assert.ok(process.env.SWARM_MANAGED_PYTHON,'Explicit isolated GUI/TLS Python required');
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-managed-browser-'));
    const args=[path.join(__dirname,'fixtures/swarming_managed_ui_server.py'),output,process.env.SONN_GOVERNANCE_TEST_CONFIG];
    if(hold)args.push('--hold');
    const server=spawn(process.env.SWARM_MANAGED_PYTHON,args,{cwd:output,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    const responses=[],commands=[],errors=[];
    server.stdout.on('data',chunk=>{stdout+=chunk;});server.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    async function ready(){await page.waitForFunction(()=>app._swarmState&&!app._swarmPending);}
    async function evidence(){return (await fetch(info.url+'/__fixture__/evidence')).json();}
    try{
        for(let n=0;n<300;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Managed fixture startup failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Managed fixture did not become ready: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});page.setDefaultTimeout(18000);
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>{
            socket.on('framesent',frame=>{const value=JSON.parse(frame.payload);if(value.command==='swarm')commands.push(value);});
            socket.on('framereceived',frame=>{const value=JSON.parse(frame.payload);if(value.event==='swarm_state')responses.push(value);});
        });
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));await page.waitForFunction(id=>window.app?.currentSessionId===id,info.session_id);
        await page.locator('#user-input').fill('Keep this private ordinary conversation draft');
        await page.getByRole('button',{name:'Team',exact:true}).click();await ready();
        assert.equal(await page.getByLabel('Execution ownership').inputValue(),'personal');
        await page.getByText('Saved teams in this conversation',{exact:true}).click();
        await page.waitForFunction(()=>app._swarmHistory?.items.length===1&&!app._swarmPending);
        assert.ok(await page.getByLabel('Saved team',{exact:true}).textContent().then(text=>text.includes('PRIVATE retained personal objective')));
        await page.getByLabel('Saved team',{exact:true}).selectOption('personal-retained-team');
        await page.getByRole('button',{name:'Open saved team',exact:true}).click();await ready();
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Enable team preview').check();await ready();
        await page.getByLabel('Execution ownership').focus();await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.execution_mode==='managed'&&!app._swarmPending);
        assert.equal(await page.getByLabel('Execution ownership').inputValue(),'managed');
        assert.equal(await page.getByRole('button',{name:'Back to saved team',exact:true}).isVisible(),false);
        assert.equal(await page.getByText('Share with another personal conversation',{exact:true}).isVisible(),false);
        await page.waitForFunction(()=>app._swarmHistory?.items.length===0&&!app._swarmPending);
        assert.equal(await page.getByText('PRIVATE retained personal objective',{exact:true}).isVisible(),false);
        let observed=await evidence();assert.equal(observed.backend_instances,0);assert.deepEqual(observed.remote.runs,[]);
        await page.getByLabel('Team objective').fill('PRIVATE managed browser investigation');
        await page.getByRole('button',{name:'Remove investigation 2',exact:true}).click();
        await page.locator('[data-task-objective]').fill('PRIVATE read fact.txt and report its observed fact');
        await page.locator('[data-task-roots]').fill('fact.txt');
        await page.locator('[data-swarm="slots"]').selectOption('1');
        await page.locator('[data-swarm="allowance"]').fill('4');
        await page.setViewportSize({width:390,height:844});
        await page.getByLabel('Team objective').focus();await page.waitForTimeout(1100);
        assert.equal(await page.getByLabel('Team objective').evaluate(node=>node===document.activeElement),true);
        assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'managed-compact-setup.png')});
        await page.getByRole('button',{name:'Start read-only team',exact:true}).focus();await page.keyboard.press('Enter');await ready();
        await page.waitForFunction(()=>app._swarmState?.managed?.effective_policy?.authenticated);
        const runId=await page.evaluate(()=>app._swarmScope.run_id);
        // Forged mode/resource combinations use the actual WebSocket handler.
        const denied=await page.evaluate(async({project,session,run})=>{
            const request_id=crypto.randomUUID();
            const response=new Promise(resolve=>{const listen=event=>{const data=JSON.parse(event.data);if(data.request_id===request_id){app.ws.removeEventListener('message',listen);resolve(data);}};app.ws.addEventListener('message',listen);});
            app.send({command:'swarm',action:'view',execution_mode:'managed',project,session_id:session,run_id:run,request_id});return response;
        },{project:info.workspace,session:info.session_id,run:'personal-retained-team'});
        assert.ok(denied.error&&!denied.run);
        if(hold){
            for(let n=0;n<100;n++){observed=await evidence();if(observed.provider_entered)break;await page.waitForTimeout(50);}
            assert.ok(observed.provider_entered);
            assert.equal((await fetch(info.url+'/__fixture__/offline',{method:'POST'})).status,200);
            await page.getByRole('button',{name:'Refresh team',exact:true}).click();await ready();
            const started=Date.now();await page.getByRole('button',{name:'Stop team',exact:true}).click();await ready();
            assert.ok(Date.now()-started<3000,'Local Stop waited for unavailable governance');
            assert.equal(await page.evaluate(()=>Boolean(app._swarmState.run.run.stop_requested)),true);
            observed=await evidence();assert.ok(observed.runs[0].run.model_requests.some(row=>['started','uncertain'].includes(row.state)));
            await fetch(info.url+'/__fixture__/release',{method:'POST'});
            await page.waitForFunction(()=>app._swarmState?.run?.workers.every(row=>!row.alive));
        }else{
            await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
            await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
            await page.getByText('Managed scripted finding: quoted CSV fields preserve commas. Owner review remains required.',{exact:true}).waitFor();
            for(let n=0;n<150;n++){observed=await evidence();if(observed.remote.runs[0]?.projection?.counts.requests_known===2)break;await page.waitForTimeout(100);}
            assert.equal(observed.remote.runs[0].projection.counts.requests_known,2);
            assert.equal(observed.backend_requests,2);assert.equal(observed.runs[0].run.action_receipts.length,1);
            const remote=await fetch(info.url+'/__fixture__/remote-stop',{method:'POST'});assert.equal(remote.status,200);
            await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='cancelled');
            for(let n=0;n<100;n++){observed=await evidence();if(observed.remote.runs[0].controls.some(row=>row.outcome==='applied'))break;await page.waitForTimeout(100);}
            assert.ok(observed.remote.runs[0].controls.some(row=>row.outcome==='applied'));
        }
        observed=await evidence();
        assert.equal(observed.private_configuration_in_model,false);assert.equal(observed.remote_contains_content,false);
        const publicFrames=JSON.stringify(responses);
        for(const forbidden of ['BEGIN PRIVATE KEY','managed.json','first.key','fixture-ca.pem'])assert.equal(publicFrames.includes(forbidden),false);
        assert.deepEqual(errors,[]);
        await page.screenshot({path:path.join(output,'managed-final.png')});
        await page.keyboard.press('Escape');assert.equal(await page.locator('#user-input').inputValue(),'Keep this private ordinary conversation draft');
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:hold?'managed-offline-stop-browser':'managed-mtls-postgresql-browser',
            live_providers_called:false,run_id:runId,keyboard:true,compact:true,denied,observed,commands},null,2));
        console.log('Managed browser evidence:',output);
    }catch(error){
        fs.writeFileSync(path.join(output,'debug.json'),JSON.stringify({errors,commands,responses},null,2));
        if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));}
        console.error('Managed browser failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),7000);})]);clearTimeout(timer);
        if(!result)server.kill();fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
}
test('Managed ownership is explicit, private, and remotely controllable', {timeout:90000},()=>runCase(false));
test('Local Stop remains responsive with governance offline', {timeout:90000},()=>runCase(true));
