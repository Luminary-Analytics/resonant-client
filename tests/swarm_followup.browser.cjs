/* Actual source GUI, WebSocket, coordinator inputs and native file reads.
 * Only inference is scripted. No paid provider or installed bundle is used. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');
const candidate = process.env.SWARM_PACKAGED_EXECUTABLE ? path.resolve(process.env.SWARM_PACKAGED_EXECUTABLE) : null;

test('Owner requests a findings-only follow-up while another worker continues', {timeout:90000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'sonn-followup-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', candidate
        ? [path.join(__dirname, 'fixtures/swarming_packaged_server.py'), output, candidate, '--followup-planning']
        : [path.join(__dirname, 'fixtures/swarming_ui_server.py'), output, '--followup-planning', '--history'],
        {cwd:output, windowsHide:true, stdio:['pipe','pipe','pipe']});
    let stdout='', stderr='', info, browser, page;
    server.stdout.on('data', data=>stdout+=data);
    server.stderr.on('data', data=>stderr+=data);
    const exited = new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for(let i=0;i<350;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null) throw Error(stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info, 'Fixture ready metadata missing: '+stderr);
        browser=await chromium.launch({headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(15000);
        const errors=[], commands=[], responses=[];
        page.on('pageerror', error=>errors.push(error.message));
        page.on('websocket', socket=>{
        socket.on('framesent', frame=>{
            const data=JSON.parse(frame.payload);
            if(data.command==='swarm') commands.push(data);
        });
        socket.on('framereceived', frame=>{
            const data=JSON.parse(frame.payload);
            if(data.event==='swarm_state') responses.push(data);
        });
        });
        async function review(button) {
            for(let attempt=0;attempt<2;attempt++) {
                await page.waitForFunction(()=>!app._swarmPending);
                const offset=commands.length;
                await button.click();
                let result;
                for(let n=0;n<100;n++) {
                    const sent=commands.slice(offset).find(row=>row.action==='review_read_result');
                    result=sent && responses.find(row=>row.request_id===sent.request_id);
                    if(result) break;
                    await page.waitForTimeout(50);
                }
                assert.ok(result,'A lost review reply must not be replayed');
                if(!result.error) return;
                assert.match(result.error,/^Run revision changed/);
                assert.equal(attempt,0);
                // A visible, definite rejection permits a fresh owner decision.
                await page.getByRole('button',{name:'Refresh team',exact:true}).click();
                await page.waitForFunction(()=>!app._swarmPending);
            }
        }
        await page.route('**/*', route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);
        if(candidate){
            await page.waitForFunction(()=>window.app?.backends?.ollama?.models?.length);
            await page.locator(`.agent-row[data-session-id="${info.session_id}"] .session-title-text`).click();
        }
        await page.waitForFunction(session=>app.currentSessionId===session,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled && !app._swarmPending);
        // An existing stopped team is deliberately retained for later history navigation.
        if(await page.getByRole('button',{name:'New team',exact:true}).isVisible())
            await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Team objective').fill('Use partial findings while the second investigation continues');
        await page.locator('[data-swarm="plan-mode"]').selectOption('coordinator');
        await page.getByLabel('Total model requests').fill('20');
        await page.getByLabel('Coordinator request allowance',{exact:true}).fill('2');
        await page.getByLabel('Allowance per worker',{exact:true}).fill('4');
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        await page.getByText('Coordinator · Complete',{exact:true}).waitFor();
        await page.getByLabel('Plan review notes').fill('Approve two independent scoped investigations.');
        await page.getByRole('button',{name:'Approve plan',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===1
            && app._swarmState.run.attempts.some(row=>row.kind==='worker' && row.process_state==='running')
            && app._swarmState.coordinator_planning?.available && !app._swarmPending);
        const initial=await page.evaluate(()=>app._swarmState.run);
        const runId=initial.run.id;
        const peer=initial.attempts.find(row=>row.kind==='worker' && row.process_state==='running');
        const peerRequest=initial.model_requests.find(row=>row.attempt_id===peer.id && row.state==='started');
        assert.ok(peerRequest);
        if(candidate){
            const actual=await (await fetch(info.api_url+'/__fixture__/evidence')).json();
            const children=actual.children.filter(row=>row.argv.includes('--swarm-worker'));
            assert.equal(children.length,1);
            assert.equal(path.resolve(children[0].exe).toLowerCase(),candidate.toLowerCase());
            assert.ok(initial.process_observations.some(row=>row.pid===children[0].pid));
        }
        await page.getByLabel('Follow-up coordinator allowance',{exact:true}).fill('2');
        await page.getByLabel('Follow-up readable folders',{exact:true}).fill('fact.txt');
        await page.getByLabel('Follow-up readable folders',{exact:true}).focus();
        await page.waitForTimeout(1200);
        assert.equal(await page.getByLabel('Follow-up readable folders',{exact:true}).inputValue(),'fact.txt');
        assert.equal(await page.getByLabel('Follow-up readable folders',{exact:true}).evaluate(node=>document.activeElement===node),true);
        assert.equal(commands.filter(row=>row.action==='request_plan').length,0);
        // Real history controls preserve this run's draft; opening history does not stop its peer.
        if(!candidate){
        await page.getByText('Saved teams in this conversation',{exact:true}).click();
        await page.getByRole('button',{name:'Refresh history',exact:true}).click();
        await page.waitForFunction(()=>!app._swarmPending);
        await page.locator('[data-swarm="history-select"]').selectOption('history-22');
        await page.locator('[data-swarm="history-open"]').click();
        await page.waitForFunction(()=>app._swarmScope.run_id==='history-22' && !app._swarmPending);
        await page.locator('[data-swarm="history-select"]').selectOption(runId);
        await page.locator('[data-swarm="history-open"]').click();
        await page.waitForFunction(id=>app._swarmScope.run_id===id && !app._swarmPending,runId);
        assert.equal(await page.getByLabel('Follow-up readable folders',{exact:true}).inputValue(),'fact.txt');
        }
        await page.getByLabel('Follow-up readable folders',{exact:true}).fill('');
        await page.setViewportSize({width:390,height:844});
        await page.getByRole('button',{name:'Request follow-up plan',exact:true}).scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(output,'followup-findings-only-compact.png')});
        assert.equal(await page.getByRole('dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.getByRole('button',{name:'Request follow-up plan',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.run?.coordinator_proposals?.length===2
            && app._swarmState.run.attempts.filter(row=>row.kind==='coordinator').every(row=>row.state==='completed') && !app._swarmPending);
        const followup=commands.filter(row=>row.action==='request_plan');
        assert.equal(followup.length,1);
        assert.equal(followup[0].coordinator_requests,2);
        assert.deepEqual(followup[0].read_roots,[]);
        assert.equal(Number.isInteger(followup[0].expected_revision),true);
        const during=await page.evaluate(()=>app._swarmState.run);
        assert.equal(during.attempts.find(row=>row.id===peer.id).process_state,'running');
        assert.equal(during.model_requests.find(row=>row.id===peerRequest.id).state,'started');
        assert.equal(during.attempts.filter(row=>row.kind==='worker').length,2,'Proposal alone must start no worker');
        const plan=page.locator('.swarm-proposal').filter({hasText:'Follow up on the first retained finding.'});
        await plan.getByLabel('Plan review notes').fill('Approve this new scoped task while the existing peer continues.');
        await plan.getByRole('button',{name:'Approve plan',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===2 && !app._swarmPending);
        assert.equal(await page.evaluate(id=>app._swarmState.run.attempts.find(row=>row.id===id).process_state,peer.id),'running');
        await fetch(candidate ? info.api_url+'/__fixture__/release' : info.url+'/__fixture__/release-workers',{method:'POST'});
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===3
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending);
        await page.setViewportSize({width:1180,height:900});
        for(const worker of [1,2,3]) {
            await page.getByText(`Worker ${worker} · Awaiting verification`,{exact:true}).click();
            await page.getByLabel(`Review notes for Worker ${worker}`).fill(`Owner reviewed retained file evidence for worker ${worker}.`);
            await review(page.getByRole('form',{name:`Review Worker ${worker}`,exact:true}).getByRole('button',{name:'Accept findings',exact:true}));
            await page.getByText(`Worker ${worker} · Accepted`,{exact:true}).waitFor();
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.locator('[data-swarm="run-state"]').filter({hasText:'Complete'}).waitFor();
        const evidence=await (await fetch((info.api_url||info.url)+'/__fixture__/evidence')).json();
        const finalRun=await page.evaluate(()=>app._swarmState.run);
        if(candidate){
            assert.equal(evidence.requests.length,8);
            assert.equal(evidence.requests.filter(row=>row.observed_file_result).length,3);
            assert.equal(evidence.children.filter(row=>row.argv.includes('--swarm-worker')).length,0);
            const log=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
            assert.match(log,/Application startup complete/);
            assert.match(log,/WebSocket.*accepted/);
            assert.equal(log.includes('Traceback (most recent call last)'),false);
            const asset=await fetch(info.url+'/static/swarm_view.js');
            assert.equal(Buffer.compare(Buffer.from(await asset.arrayBuffer()),fs.readFileSync(path.join(path.dirname(candidate),'_internal','lumi','gui','static','swarm_view.js'))),0);
        }else{
            assert.equal(evidence.backend_instances,5);
            assert.equal(evidence.backend_requests,8);
        }
        assert.equal(evidence.followup_inputs.length,2);
        assert.deepEqual(evidence.followup_inputs[1].coordinator_read_roots,[]);
        assert.equal(evidence.followup_inputs[1].recent_untrusted_findings.length,1);
        assert.ok(evidence.followup_inputs[1].recent_untrusted_findings[0].excerpt.includes('quoted CSV fields'));
        assert.equal(finalRun.run.state,'completed');
        assert.equal(finalRun.check_receipts.length,3);
        assert.equal(commands.filter(row=>['cancel_worker','pause_worker','stop'].includes(row.action)).length,0);
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({commands,responses:responses.filter(row=>row.error),evidence,finalRun,candidate:info,
            kind:candidate?'frozen-native-followup-planning':'source-native-followup-planning'},null,2));
        await page.screenshot({path:path.join(output,'followup-completed.png')});
        console.log('Follow-up planning browser evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
        }
        console.error('Follow-up browser failure evidence:',output);
        throw error;
    } finally {
        if(browser) await browser.close();
        if(info && !candidate) await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        server.stdin.end();
        let timer;
        const stopped=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);})]);
        clearTimeout(timer);
        if(!stopped) server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
