/* Unmodified frozen app and real owned child. Only provider output and actual
 * WebSocket reply delivery timing are controlled by the outside test harness. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=require(process.argv[3]||'playwright');

test('Frozen Stop closes owned workers while follow-up reply is withheld',{timeout:100000},async()=>{
    const executable=path.resolve(process.argv[2]);
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-frozen-followup-stop-'));
    const fixture=spawn(process.env.SWARM_PYTHON||'python',[path.join(__dirname,'fixtures/swarming_packaged_server.py'),output,executable,'--followup-planning'],
        {windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    fixture.stdout.on('data',data=>stdout+=data);fixture.stderr.on('data',data=>stderr+=data);
    const exited=new Promise(resolve=>fixture.once('exit',(code,signal)=>resolve({code,signal})));
    try{
        for(let n=0;n<350;n++){
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(fixture.exitCode!==null)throw Error(stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Frozen fixture did not become ready: '+stderr);
        browser=await chromium.launch({headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(25000);
        const commands=[],responses=[],errors=[];
        let plannerId,refreshId,plannerFrame,refreshFrame,clientSocket;
        page.on('pageerror',error=>errors.push(error.message));
        await page.routeWebSocket('**/ws',socket=>{
            clientSocket=socket;
            const server=socket.connectToServer();
            socket.onMessage(message=>{
                const value=JSON.parse(message.toString());
                if(value.command==='swarm'){
                    commands.push(value);
                    if(value.action==='request_plan')plannerId=value.request_id;
                    if(plannerFrame && !refreshId && value.action==='view')refreshId=value.request_id;
                }
                server.send(message);
            });
            server.onMessage(message=>{
                const value=JSON.parse(message.toString());
                if(value.event==='swarm_state'){
                    responses.push(value);
                    if(value.request_id===plannerId){plannerFrame=message;return;}
                    if(value.request_id===refreshId){refreshFrame=message;return;}
                }
                socket.send(message);
            });
        });
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);
        await page.waitForFunction(()=>window.app?.backends?.ollama?.models?.length);
        await page.locator(`.agent-row[data-session-id="${info.session_id}"] .session-title-text`).click();
        await page.waitForFunction(id=>app.currentSessionId===id,info.session_id);
        await page.setViewportSize({width:390,height:844});
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled&&!app._swarmPending);
        await page.getByLabel('Team objective').fill('Stop a frozen team while its follow-up reply is delayed');
        await page.locator('[data-swarm="plan-mode"]').selectOption('coordinator');
        await page.getByLabel('Total model requests').fill('20');
        await page.getByLabel('Coordinator request allowance',{exact:true}).fill('2');
        await page.getByLabel('Allowance per worker',{exact:true}).fill('4');
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        await page.getByText('Coordinator · Complete',{exact:true}).waitFor();
        await page.getByLabel('Plan review notes').fill('Approve scoped fixture work; retain Stop authority.');
        await page.getByRole('button',{name:'Approve plan',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===1
            &&app._swarmState.run.attempts.some(row=>row.kind==='worker'&&row.process_state==='running')
            &&app._swarmState.coordinator_planning?.available&&!app._swarmPending);
        const captured=await page.evaluate(()=>({scope:app._swarmScope,run:app._swarmState.run}));
        const peer=captured.run.attempts.find(row=>row.kind==='worker'&&row.process_state==='running');
        const native=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        const children=native.children.filter(row=>row.argv.includes('--swarm-worker'));
        assert.equal(children.length,1);
        assert.equal(path.resolve(children[0].exe).toLowerCase(),executable.toLowerCase());
        assert.ok(captured.run.process_observations.some(row=>row.pid===children[0].pid));
        await page.getByLabel('Follow-up coordinator allowance',{exact:true}).fill('2');
        await page.getByLabel('Follow-up readable folders',{exact:true}).fill('');
        await page.getByRole('button',{name:'Request follow-up plan',exact:true}).click();
        for(let n=0;n<200&&!plannerFrame;n++)await page.waitForTimeout(25);
        assert.ok(plannerFrame,'Hold an actual acknowledged planner reply');
        assert.equal(JSON.parse(plannerFrame.toString()).error,undefined);
        // Keep the reply withheld while the short coordinator actually exits.
        // This leaves only the deliberately blocked peer when requesting Stop.
        let planned;
        for(let n=0;n<160;n++){
            planned=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
            if(planned.requests.length===5&&planned.children.filter(row=>row.argv.includes('--swarm-worker')).length===1)break;
            await page.waitForTimeout(25);
        }
        assert.equal(planned.requests.length,5);
        assert.equal(planned.children.filter(row=>row.argv.includes('--swarm-worker')).length,1);
        const stop=page.getByRole('button',{name:'Stop team',exact:true});
        assert.equal(await stop.isEnabled(),true);
        await stop.focus();await page.keyboard.press('Enter');
        for(let n=0;n<200&&!refreshFrame;n++)await page.waitForTimeout(25);
        assert.ok(refreshFrame,'Stop must obtain the actual current revision');
        assert.equal(commands.filter(row=>row.action==='stop').length,0);
        await page.getByRole('button',{name:'Close team panel',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmDialog);
        const refreshed=JSON.parse(refreshFrame.toString());
        clientSocket.send(refreshFrame);
        for(let n=0;n<200&&!commands.some(row=>row.action==='stop');n++)await page.waitForTimeout(25);
        const sent=commands.filter(row=>row.action==='stop');
        assert.equal(sent.length,1);
        assert.equal(sent[0].run_id,captured.scope.run_id);
        assert.equal(sent[0].session_id,captured.scope.session_id);
        assert.equal(sent[0].project,captured.scope.project);
        assert.equal(sent[0].expected_revision,refreshed.run.run.revision);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.run.stop_requested
            &&app._swarmState.run.attempts.every(row=>row.process_state==='stopped')&&!app._swarmPending);
        // The provider gate has not been released: only actual owned process
        // cleanup, not fixture response completion, can satisfy this boundary.
        const observed=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        assert.equal(observed.children.filter(row=>row.argv.includes('--swarm-worker')).length,0);
        const stopped=await page.evaluate(()=>app._swarmState.run);
        assert.equal(stopped.attempts.find(row=>row.id===peer.id).process_state,'stopped');
        assert.equal(stopped.process_observations.find(row=>row.pid===children[0].pid).state,'stopped');
        assert.equal(stopped.attempts.filter(row=>row.kind==='coordinator').length,2);
        assert.ok(stopped.model_requests.some(row=>row.attempt_id===peer.id&&row.state==='uncertain'));
        const before=await page.evaluate(()=>({scope:app._swarmScope,run:app._swarmState.run.run}));
        clientSocket.send(plannerFrame);
        await page.waitForTimeout(100);
        assert.deepEqual(await page.evaluate(()=>({scope:app._swarmScope,run:app._swarmState.run.run})),before);
        assert.equal(commands.filter(row=>row.action==='request_plan').length,1);
        assert.equal(commands.filter(row=>row.action==='stop').length,1);
        assert.deepEqual(errors,[]);
        const asset=await fetch(info.url+'/static/swarm_view.js');
        assert.equal(Buffer.compare(Buffer.from(await asset.arrayBuffer()),fs.readFileSync(path.join(path.dirname(executable),'_internal','lumi','gui','static','swarm_view.js'))),0);
        const log=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
        assert.match(log,/Application startup complete/);assert.match(log,/WebSocket.*accepted/);
        assert.equal(log.includes('Traceback (most recent call last)'),false);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({kind:'frozen-followup-stop',candidate:info,commands,
            responses:responses.filter(row=>row.error),native,observed,stopped},null,2));
        await page.locator('[data-swarm="run-state"]').scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(output,'stopped-frozen-team.png')});
        console.log('Frozen follow-up Stop evidence:',output);
    }catch(error){
        if(page){
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
        }
        console.error('Frozen follow-up Stop failure:',output);throw error;
    }finally{
        if(browser)await browser.close();fixture.stdin.end();
        let timer;const stopped=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),10000);})]);
        clearTimeout(timer);if(!stopped)fixture.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
