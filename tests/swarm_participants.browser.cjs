/* Actual source UI/WS/Session controls with gated scripted providers and temp state.
 * node tests/swarm_participants.browser.cjs [absolute-path-to-playwright-module]
 * These observations do not qualify live providers or packaged process workers.
 */
// The app page needs a one-time launch code (lumi/gui/local_access.py); the
// fixture server mints one per page load.
const fixtureLaunch=async info=>(await (await fetch(info.url+'/__fixture__/launch')).json()).url;
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

test('Source Team participant controls retain peer execution and exact guidance input', {timeout: 90000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'sonn-swarm-participants-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--participant-controls'],
        {cwd:output, windowsHide:true, stdio:['ignore','pipe','pipe']});
    let stdout='', stderr='', info, browser, page;
    server.stdout.on('data', chunk=>{stdout+=chunk;});
    server.stderr.on('data', chunk=>{stderr+=chunk;});
    const exited = new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for (let i=0;i<150;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Fixture server failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Fixture server ready metadata missing: '+stderr);
        const release=(participant,request_number)=>fetch(info.url+'/__fixture__/release-participant',
            {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({participant,request_number})});
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ? {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE} : {headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(15000);
        const errors=[], commands=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>socket.on('framesent',frame=>{
            const data=JSON.parse(frame.payload);
            if(data.command==='swarm')commands.push(data);
        }));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>window.app?.currentSessionId===session,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Team objective').fill('Inspect independent participant controls');
        await page.getByLabel('Worker slots').selectOption('3');
        await page.getByRole('button',{name:'Add investigation',exact:true}).click();
        for(const [index,name] of ['one','two','three'].entries())await page.locator('[data-task-objective]').nth(index).fill(`Participant ${name}: inspect fact.txt`);
        await page.getByRole('button',{name:'Start read-only team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.filter(row=>row.state==='started').length===3
            && app._swarmState.run.attempts.every(row=>row.state==='running') && !app._swarmPending);
        const initial=await page.evaluate(()=>app._swarmState.run);
        const ids=Object.fromEntries(initial.work_items.map(item=>[item.objective.split(':')[0],initial.attempts.find(row=>row.work_item_id===item.id).id]));
        await page.getByText('Worker 1 · Working',{exact:true}).click();
        const one=page.locator('.swarm-worker').filter({has:page.locator('summary').filter({hasText:'Worker 1 ·'})});
        await page.getByRole('button',{name:'Pause Worker 1',exact:true}).click();
        await one.getByText('Pause requested. Waiting for the current activity to reach a checkpoint.',{exact:true}).waitFor();
        assert.equal(await one.getByText('Paused at an observed checkpoint.',{exact:true}).count(),0);
        await release('one',1);
        await release('two',1);
        await one.getByText('Paused at an observed checkpoint.',{exact:true}).waitFor();
        await page.getByText('Worker 2 · Awaiting verification',{exact:true}).waitFor();
        let snapshot=await page.evaluate(()=>app._swarmState.run);
        assert.equal(snapshot.submissions.some(row=>row.attempt_id===ids['Participant one']),false);
        assert.ok(snapshot.submissions.some(row=>row.attempt_id===ids['Participant two']));
        await page.getByLabel('Guidance for Worker 1',{exact:true}).fill('Focus on quoted CSV fields.');
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Guidance for Worker 1',{exact:true}).evaluate(node=>document.activeElement===node),true);
        await page.setViewportSize({width:390,height:844});
        await page.getByRole('button',{name:'Send guidance to Worker 1',exact:true}).scrollIntoViewIfNeeded();
        assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'source-participant-guidance-compact.png')});
        await page.getByRole('button',{name:'Send guidance to Worker 1',exact:true}).focus();
        await page.keyboard.press('Enter');
        await one.getByText('Queued for this participant',{exact:true}).waitFor();
        assert.equal(await one.getByText('Included in prepared model input',{exact:true}).count(),0);
        await page.getByRole('button',{name:'Resume Worker 1',exact:true}).click();
        await one.getByText('Included in prepared model input',{exact:true}).waitFor();
        await page.waitForFunction(id=>app._swarmState.run.model_requests.filter(row=>row.attempt_id===id&&row.state==='started').length===1,ids['Participant one']);
        const prepared=await (await fetch(info.url+'/__fixture__/evidence')).json();
        assert.ok(prepared.participant_inputs.some(row=>row.participant==='one'&&row.request_number===2&&row.guidance_prepared));
        snapshot=await page.evaluate(()=>app._swarmState.run);
        assert.equal(snapshot.owner_directives.length,1);
        assert.equal(snapshot.owner_directive_receipts.length,1);
        assert.equal(snapshot.owner_directive_receipts[0].attempt_id,ids['Participant one']);
        assert.equal(snapshot.owner_directive_receipts[0].directive_id,snapshot.owner_directives[0].id);
        await page.getByText('Worker 3 · Working',{exact:true}).click();
        const three=page.locator('.swarm-worker').filter({has:page.locator('summary').filter({hasText:'Worker 3 ·'})});
        await page.getByRole('button',{name:'Stop Worker 3',exact:true}).click();
        await three.getByText('Stop requested. Termination is not yet confirmed.',{exact:true}).waitFor();
        assert.equal(await page.getByRole('button',{name:'Stop Worker 3',exact:true}).isEnabled(),false);
        snapshot=await page.evaluate(()=>app._swarmState.run);
        assert.equal(snapshot.attempts.find(row=>row.id===ids['Participant one']).process_state,'running');
        assert.equal(snapshot.attempts.find(row=>row.id===ids['Participant one']).cancel_requested,0);
        assert.equal(snapshot.attempts.find(row=>row.id===ids['Participant three']).process_state,'running');
        await release('three',1);
        await page.waitForFunction(id=>app._swarmState.run.attempts.find(row=>row.id===id).process_state==='stopped',ids['Participant three']);
        assert.equal(await page.evaluate(id=>app._swarmState.run.attempts.find(row=>row.id===id).process_state,ids['Participant one']),'running');
        await release('one',2);
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        const result=evidence.runs[0].run;
        assert.equal(result.submissions.length,2);
        // Cancellation stopped the runtime before its model response was fully
        // observed. Termination is known; request consumption remains unknown.
        assert.equal(result.attempts.find(row=>row.id===ids['Participant three']).state,'uncertain');
        const interrupted=result.model_requests.find(row=>row.attempt_id===ids['Participant three']);
        assert.equal(interrupted.state,'uncertain');
        assert.equal(interrupted.used,null);
        assert.equal(result.reservations.find(row=>row.attempt_id===ids['Participant three']).state,'uncertain');
        assert.equal(result.run.stop_requested,0);
        assert.ok(result.attempts.every(row=>row.process_state==='stopped'));
        assert.deepEqual(commands.filter(row=>['pause_worker','resume_worker','steer_worker','cancel_worker'].includes(row.action)).map(row=>row.action),['pause_worker','steer_worker','resume_worker','cancel_worker']);
        assert.ok(commands.filter(row=>row.attempt_id).every(row=>row.run_id===initial.run.id&&row.attempt_epoch===1&&Number.isInteger(row.expected_revision)));
        assert.deepEqual(errors,[]);
        await page.getByRole('button',{name:'Stop team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState.run.run.stop_requested===1 && !app._swarmPending);
        assert.equal(await page.getByRole('button',{name:'New team',exact:true}).count(),0,'Stop cannot erase uncertain accounting');
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({...evidence,commands,
            stopped_view:await page.evaluate(()=>app._swarmState.run)},null,2));
        console.log('Source-app participant control evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState).catch(()=>({})),null,2));
        }
        console.error('Participant browser failure evidence:',output);
        throw error;
    } finally {
        if(browser)await browser.close();
        if(info)await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let cleanupTimer;
        const result=await Promise.race([exited,new Promise(resolve=>{cleanupTimer=setTimeout(()=>resolve(null),5000);})]);
        clearTimeout(cleanupTimer);
        if(!result)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
