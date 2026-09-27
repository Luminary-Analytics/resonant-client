/* Full source app + real WebSocket/worker/file/SQLite path; inference scripted.
 * node tests/swarm_app.browser.cjs [absolute-path-to-playwright-module]
 * Optional SWARM_PYTHON and SWARM_BROWSER_EXECUTABLE select local runtimes.
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

test('Source app Team controls use real WebSocket and scripted native workers', {timeout: 90000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'sonn-swarm-app-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--hold-first-workers'],
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
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ? {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE} : {headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(15000);
        const errors=[];
        const commands=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>socket.on('framesent',frame=>{
            const data=JSON.parse(frame.payload);
            if(data.command==='swarm')commands.push(data);
        }));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>window.app?.currentSessionId===session,info.session_id);
        await page.locator('#user-input').fill('Preserve the ordinary conversation draft');
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Team objective').fill('Inspect two independent CSV concerns');
        await page.locator('[data-task-objective]').nth(0).fill('Read fact.txt and inspect quoting');
        await page.locator('[data-task-objective]').nth(1).fill('Read fact.txt and inspect delimiter handling');
        await page.getByRole('button',{name:'Start read-only team'}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.filter(row=>row.state==='started').length===2
            && app._swarmState.run.attempts.every(row=>row.state==='running') && !app._swarmPending);
        await page.getByText('2 active workers · Assignment limit: 2 of 2 allowed · 0 active coordinators',{exact:true}).waitFor();
        await page.getByLabel('Active worker limit',{exact:true}).selectOption('1');
        await page.getByLabel('Active worker limit',{exact:true}).focus();
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Active worker limit',{exact:true}).inputValue(),'1');
        assert.equal(await page.getByLabel('Active worker limit',{exact:true}).evaluate(node=>document.activeElement===node),true);
        await page.setViewportSize({width:390,height:844});
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(output,'source-worker-limit-compact.png')});
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.getByText('2 active workers · Assignment limit: 1 of 2 allowed · 0 active coordinators',{exact:true}).waitFor();
        const limited=await page.evaluate(()=>app._swarmState.run);
        assert.equal(limited.run.worker_limit,1);
        assert.equal(JSON.parse(limited.run.policy_json).max_workers,2);
        assert.ok(limited.attempts.every(row=>row.state==='running'&&row.process_state==='running'),'Lowering admission must not cancel either existing worker');
        await fetch(info.url+'/__fixture__/release-workers',{method:'POST'});
        await page.setViewportSize({width:1180,height:900});
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText('Worker 2 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await page.getByText('Scripted reader finding: quoted CSV fields preserve commas. Independent review remains required.',{exact:true}).first().waitFor();
        assert.equal(await page.getByText('Complete',{exact:true}).count(),0);
        const runId=await page.evaluate(()=>app._swarmScope.run_id);
        await page.getByRole('button',{name:'Pause new work'}).click();
        await page.getByRole('button',{name:'Resume',exact:true}).waitFor();
        await page.getByLabel('Active worker limit',{exact:true}).selectOption('2');
        await page.getByRole('button',{name:'Apply worker limit',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState.run.run.worker_limit===2 && app._swarmState.run.run.state==='paused');
        await page.getByRole('button',{name:'Resume',exact:true}).click();
        await page.getByRole('button',{name:'Pause new work'}).waitFor();

        // Closing the actual socket exercises app reconnect against the same
        // server-owned run; it does not replace responses or fixture state.
        await page.evaluate(()=>app.ws.close());
        await page.waitForFunction(run=>app.ws?.readyState===1 && !app._swarmPending && app._swarmScope.run_id===run,runId);
        await page.getByText('Worker 2 · Awaiting verification',{exact:true}).waitFor();
        await page.keyboard.press('Escape');
        await page.locator(`.agent-row[data-session-id="${info.other_session_id}"]`).click();
        await page.getByText('Finish or stop the current run before changing projects or sessions. Your work is retained.',{exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>app.currentSessionId),info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).waitFor();
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        await page.setViewportSize({width:390,height:844});
        const dialog=page.getByRole('dialog',{name:'Work together'});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'source-app-team-compact.png')});
        await page.getByRole('button',{name:'Stop team'}).click();
        await page.getByRole('button',{name:'New team',exact:true}).waitFor();
        await page.setViewportSize({width:1180,height:900});
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Team objective').fill('Review two independent reader findings');
        await page.locator('[data-task-objective]').nth(0).fill('Inspect the quoted CSV fixture');
        await page.locator('[data-task-objective]').nth(1).fill('Inspect comma preservation');
        await page.getByRole('button',{name:'Start read-only team'}).click();
        for(const worker of [1,2]) {
            await page.getByText(`Worker ${worker} · Awaiting verification`,{exact:true}).waitFor();
            await page.getByText(`Worker ${worker} · Awaiting verification`,{exact:true}).click();
            const review=page.getByRole('form',{name:`Review Worker ${worker}`,exact:true});
            await review.getByRole('button',{name:'Accept findings',exact:true}).click();
            assert.equal(await page.getByText(`Worker ${worker} · Accepted`,{exact:true}).count(),0);
            await page.getByLabel(`Review notes for Worker ${worker}`).fill(`Reviewed fixture fact.txt and the retained read observation for worker ${worker}.`);
            if(worker===1) {
                await page.waitForTimeout(1150);
                assert.equal(await page.getByLabel('Review notes for Worker 1').evaluate(node=>document.activeElement===node),true);
                await page.setViewportSize({width:390,height:844});
                await review.getByRole('button',{name:'Accept findings',exact:true}).scrollIntoViewIfNeeded();
                await page.screenshot({path:path.join(output,'source-app-owner-review-compact.png')});
            }
            await review.getByRole('button',{name:'Accept findings',exact:true}).click();
            await page.getByText(`Worker ${worker} · Accepted`,{exact:true}).waitFor();
            if(worker===1)await page.setViewportSize({width:1180,height:900});
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.getByText('Complete',{exact:true}).waitFor();
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Planning approach').selectOption('coordinator');
        await page.getByLabel('Team objective').fill('Propose three independent CSV investigations');
        await page.getByLabel('Total model requests').fill('6');
        const previousStarts=commands.filter(command=>command.action==='start').length;
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        assert.equal(commands.filter(command=>command.action==='start').length,previousStarts);
        await page.getByLabel('Total model requests').fill('12');
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        await page.getByText('Coordinator · Complete',{exact:true}).waitFor();
        await page.getByText('Three independent CSV investigations share two worker slots.',{exact:true}).waitFor();
        assert.equal(await page.locator('[data-proposal-tasks] > li').count(),3);
        const coordinatorStart=commands.filter(command=>command.action==='start').at(-1);
        assert.equal(coordinatorStart.plan_mode,'coordinator');
        assert.equal(coordinatorStart.coordinator_requests,3);
        assert.equal(coordinatorStart.worker_requests,4);
        assert.equal('tasks' in coordinatorStart,false);
        const plannedRun=await page.evaluate(()=>app._swarmState.run);
        assert.equal(plannedRun.attempts.length,1,'Proposal must not dispatch workers before owner approval');
        assert.equal(plannedRun.work_items.length,0);
        await page.getByRole('button',{name:'Approve plan',exact:true}).click();
        assert.equal(commands.filter(command=>command.action==='decide_proposal').length,0,'Empty review notes must prevent approval');
        await page.getByLabel('Plan review notes').fill('Approve these three file-only investigations with owner review and two worker slots.');
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Plan review notes').evaluate(node=>document.activeElement===node),true);
        await page.setViewportSize({width:390,height:844});
        await page.getByRole('button',{name:'Approve plan',exact:true}).scrollIntoViewIfNeeded();
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'source-app-plan-review-compact.png')});
        await page.getByRole('button',{name:'Approve plan',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.getByText('Plan approved',{exact:true}).waitFor();
        const decision=commands.filter(command=>command.action==='decide_proposal').at(-1);
        assert.equal(decision.proposal_id,plannedRun.coordinator_proposals[0].id);
        assert.equal(decision.sha256,plannedRun.coordinator_proposals[0].sha256);
        assert.equal(decision.accept,true);
        assert.equal(Number.isInteger(decision.expected_revision),true);
        await page.setViewportSize({width:1180,height:900});
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===3
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending);
        for(const worker of [1,2,3]) {
            await page.getByText(`Worker ${worker} · Awaiting verification`,{exact:true}).waitFor();
            await page.getByText(`Worker ${worker} · Awaiting verification`,{exact:true}).click();
            await page.getByLabel(`Review notes for Worker ${worker}`).fill(`Reviewed fixture fact.txt for planned investigation ${worker}.`);
            await page.getByRole('form',{name:`Review Worker ${worker}`,exact:true}).getByRole('button',{name:'Accept findings',exact:true}).click();
            await page.getByText(`Worker ${worker} · Accepted`,{exact:true}).waitFor();
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.locator('[data-swarm="run-state"]').filter({hasText:'Complete'}).waitFor();
        await page.getByRole('button',{name:'New team',exact:true}).click();
        await page.getByLabel('Team objective').fill('Inspect a proposal that the owner will reject');
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        await page.getByText('Coordinator · Complete',{exact:true}).waitFor();
        await page.getByLabel('Plan review notes').fill('Reject this plan; the owner has enough evidence from the prior team.');
        await page.getByRole('button',{name:'Reject plan',exact:true}).click();
        await page.getByText('Plan rejected',{exact:true}).waitFor();
        assert.equal(commands.filter(command=>command.action==='decide_proposal').at(-1).accept,false);
        assert.equal(await page.evaluate(()=>app._swarmState.run.attempts.length),1);
        await page.getByRole('button',{name:'Stop team',exact:true}).click();
        await page.getByRole('button',{name:'New team',exact:true}).waitFor();
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#user-input').inputValue(),'Preserve the ordinary conversation draft');
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        assert.equal(evidence.backend_instances,9);
        assert.equal(evidence.backend_requests,16);
        assert.equal(evidence.runs[0].run.submissions.length,2);
        assert.equal(evidence.runs[0].run.action_receipts.filter(row=>row.tool_name==='file_read' && row.state==='completed').length,2);
        assert.equal(evidence.runs[0].run.run.state,'cancelled');
        assert.equal(evidence.runs[0].run.run.worker_limit,2);
        assert.deepEqual(commands.filter(row=>row.action==='set_concurrency').map(row=>row.max_workers),[1,2]);
        assert.equal(evidence.runs[1].run.run.state,'completed');
        assert.equal(evidence.runs[1].run.check_receipts.length,2);
        assert.ok(evidence.runs[1].run.check_receipts.every(row=>row.criterion_id==='owner_review' && row.evidence.startsWith('Reviewed fixture fact.txt')));
        assert.equal(evidence.runs[2].run.run.state,'completed');
        assert.equal(evidence.runs[2].run.coordinator_proposals[0].state,'accepted');
        assert.equal(evidence.runs[2].run.work_items.length,3);
        assert.equal(evidence.runs[2].run.attempts.length,4);
        assert.equal(evidence.runs[2].run.check_receipts.length,3);
        assert.equal(evidence.runs[3].run.coordinator_proposals[0].state,'rejected');
        assert.equal(evidence.runs[3].run.attempts.length,1);
        assert.equal(evidence.runs[3].run.work_items.length,0);
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify(evidence,null,2));
        console.log('Source-app scripted native browser evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
        }
        console.error('Browser failure evidence:',output);
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
