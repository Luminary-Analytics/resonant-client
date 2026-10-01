/* Unmodified GUI CLI, native child, actual mTLS/PostgreSQL and scripted Ollama.
 * node tests/swarm_packaged_managed.browser.cjs <candidate-exe|--source> [playwright]
 */
const test=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[3]||'playwright');

async function runCase(offline){
    assert.ok(process.env.SONN_GOVERNANCE_TEST_CONFIG,'Explicit disposable PostgreSQL configuration required');
    assert.ok(process.env.SWARM_MANAGED_PYTHON,'Explicit isolated TLS fixture Python required');
    const candidate=process.argv[2]==='--source'?'--source':path.resolve(process.argv[2]);
    if(candidate!=='--source')assert.ok(fs.existsSync(candidate));
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-packaged-managed-'));
    const fixture=spawn(process.env.SWARM_MANAGED_PYTHON,[path.join(__dirname,'fixtures/swarming_packaged_managed_server.py'),
        output,process.env.SONN_GOVERNANCE_TEST_CONFIG,candidate],{windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    const errors=[],frames=[],commands=[];
    fixture.stdout.on('data',chunk=>{stdout+=chunk;});fixture.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>fixture.once('exit',(code,signal)=>resolve({code,signal})));
    const workerChildren=value=>value.children.filter(child=>child.argv.some(arg=>arg.includes('--swarm-worker')));
    async function evidence(){return (await fetch(info.api_url+'/__fixture__/evidence')).json();}
    async function ready(){await page.waitForFunction(()=>app._swarmState&&!app._swarmPending);}
    try{
        for(let n=0;n<350;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(fixture.exitCode!==null)throw Error('Managed candidate fixture exited: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Candidate startup metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});page.setDefaultTimeout(22000);
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>{
            socket.on('framesent',frame=>{const value=JSON.parse(frame.payload);if(value.command==='swarm')commands.push(value);});
            socket.on('framereceived',frame=>{const value=JSON.parse(frame.payload);if(value.event==='swarm_state')frames.push(value);});
        });
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);
        await page.locator(`.agent-row[data-session-id="${info.session_id}"]`).click();
        await page.waitForFunction(id=>app.currentSessionId===id,info.session_id);
        await page.locator('#user-input').fill('Keep this PRIVATE_PACKAGED ordinary draft');
        await page.getByRole('button',{name:'Team',exact:true}).click();await ready();
        assert.equal(await page.getByLabel('Execution ownership').inputValue(),'personal');
        await page.getByLabel('Enable team preview').check();await ready();
        await page.getByLabel('Execution ownership').focus();await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.execution_mode==='managed'&&!app._swarmPending);
        let observed=await evidence();assert.equal(observed.requests.length,0);assert.deepEqual(observed.remote.runs,[]);
        await page.getByLabel('Team objective').fill('PRIVATE_PACKAGED managed reader qualification');
        await page.getByRole('button',{name:'Remove investigation 2',exact:true}).click();
        await page.locator('[data-task-objective]').fill('Read fact.txt and report only the observed fact');
        await page.locator('[data-task-roots]').fill('fact.txt');
        await page.locator('[data-swarm="slots"]').selectOption('1');
        await page.locator('[data-swarm="allowance"]').fill('4');
        await page.getByRole('button',{name:'Start read-only team',exact:true}).focus();await page.keyboard.press('Enter');await ready();
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.some(row=>row.state==='started'));
        await page.waitForFunction(()=>app._swarmState?.managed?.effective_policy?.authenticated);
        for(let n=0;n<100;n++){observed=await evidence();if(observed.requests.length)break;await page.waitForTimeout(50);}
        assert.equal(observed.requests.length,1,'Exactly one admitted provider call is gated');
        const during=observed, children=workerChildren(during);
        assert.equal(children.length,1,'The production runner must own a real child');
        assert.equal(path.resolve(children[0].exe).toLowerCase(),path.resolve(info.executable).toLowerCase());
        assert.ok(children.every(child=>!JSON.stringify(child.argv).includes('managed.json')));
        const active=await page.evaluate(()=>app._swarmState.run);
        assert.ok(active.process_observations.some(row=>row.pid===children[0].pid&&row.state==='started'));
        let stopMilliseconds=null;
        if(offline){
            assert.equal((await fetch(info.api_url+'/__fixture__/offline',{method:'POST'})).status,200);
            const started=Date.now();
            await page.getByRole('button',{name:'Stop team',exact:true}).click();await ready();
            stopMilliseconds=Date.now()-started;
            assert.ok(stopMilliseconds<3000,'Local Stop waited for unavailable governance');
            assert.equal(await page.evaluate(()=>Boolean(app._swarmState.run.run.stop_requested)),true);
            await page.waitForFunction(()=>app._swarmState?.run?.workers?.every(row=>!row.alive));
            await page.waitForFunction(()=>app._swarmState.run.attempts.every(row=>row.process_state==='stopped'));
            // Keep inference blocked until owned-process termination is observed;
            // releasing it earlier legitimately allows a complete response.
            await fetch(info.api_url+'/__fixture__/release',{method:'POST'});
            observed=await evidence();
            assert.equal(observed.requests.length,1,'Offline Stop must not invoke a second request');
            assert.equal(workerChildren(observed).length,0);
            const stopped=await page.evaluate(()=>app._swarmState.run);
            assert.ok(stopped.model_requests.some(row=>row.state==='uncertain'));
            assert.equal(stopped.action_receipts.length,0);
            assert.notEqual(stopped.run.state,'completed');
        }else{
            await fetch(info.api_url+'/__fixture__/release',{method:'POST'});
            await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
            await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
            await page.getByText('Packaged managed finding: quoted CSV fields preserve commas. Owner review remains required.',{exact:true}).waitFor();
            const form=page.getByRole('form',{name:'Review Worker 1',exact:true});
            await form.getByLabel('Review notes for Worker 1',{exact:true}).fill('Reviewed exact fixture file and observed read.');
            await form.getByRole('button',{name:'Accept findings',exact:true}).click();
            await page.getByText('Worker 1 · Accepted',{exact:true}).waitFor();
            await page.getByRole('button',{name:'Complete team',exact:true}).click();
            await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='completed');
            for(let n=0;n<150;n++){observed=await evidence();if(observed.remote.runs[0]?.projection?.counts.requests_known===2)break;await page.waitForTimeout(100);}
            assert.equal(observed.requests.length,2);
            assert.equal(observed.requests.filter(row=>row.observed_file_result).length,1);
            assert.equal(observed.remote.runs[0].projection.counts.requests_known,2);
            assert.equal(workerChildren(observed).length,0);
            const final=await page.evaluate(()=>app._swarmState.run);
            assert.equal(final.action_receipts.filter(row=>row.tool_name==='file_read'&&row.state==='completed').length,1);
            assert.ok(final.attempts.every(row=>row.process_state==='stopped'));
        }
        observed=await evidence();
        assert.equal(observed.remote_contains_private_content,false);
        assert.ok(observed.requests.every(row=>row.private_configuration_in_model===false));
        const serialized=JSON.stringify(frames);
        for(const forbidden of ['BEGIN PRIVATE KEY','managed.json','first.key','fixture-ca.pem'])assert.equal(serialized.includes(forbidden),false);
        const final=await page.evaluate(()=>app._swarmState);
        await page.setViewportSize({width:390,height:844});
        assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'managed-packaged-final.png')});
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#user-input').inputValue(),'Keep this PRIVATE_PACKAGED ordinary draft');
        assert.deepEqual(errors,[]);
        const log=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
        assert.match(log,/Application startup complete/);assert.match(log,/WebSocket.*accepted/);
        assert.equal(log.includes('Traceback (most recent call last)'),false);
        for(const forbidden of ['BEGIN PRIVATE KEY','managed.json','first.key'])assert.equal(log.includes(forbidden),false);
        if(!info.source_mode){
            for(const name of ['swarm_view.js','swarm_view.css']){
                const response=await fetch(info.url+'/static/'+name);
                assert.equal(response.status,200);
                assert.equal(Buffer.compare(Buffer.from(await response.arrayBuffer()),fs.readFileSync(path.join(path.dirname(candidate),'_internal','lumi','gui','static',name))),0);
            }
        }
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:offline?'unmodified-managed-offline-stop':'unmodified-managed-reader',
            candidate:info,during,final,observed,stopMilliseconds,errors,commands,live_providers_called:false},null,2));
        console.log('Unmodified managed candidate evidence:',output);
    }catch(error){
        fs.writeFileSync(path.join(output,'debug.json'),JSON.stringify({errors,commands,frames},null,2));
        if(page){await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));}
        console.error('Managed candidate failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();fixture.stdin.end('stop\n');
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),12000);})]);clearTimeout(timer);
        if(!result)fixture.kill();
        fs.writeFileSync(path.join(output,'fixture.log'),stdout+'\n'+stderr);
        const startup=path.join(output,'home','.lumi','logs','lumi-startup.log');
        if(fs.existsSync(startup))fs.copyFileSync(startup,path.join(output,'startup.log'));
    }
}
test('Unmodified managed candidate dispatches an owned reader and reports metadata privately',{timeout:120000},()=>runCase(false));
test('Unmodified managed candidate preserves local Stop while governance is offline',{timeout:120000},()=>runCase(true));
