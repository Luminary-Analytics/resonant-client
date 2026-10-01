/* Unmodified frozen GUI + actual managed frozen workers + loopback scripted API.
 * node tests/swarm_packaged.browser.cjs <candidate-exe> [playwright-module]
 * This verifies packaging and native transport, not live inference quality.
 */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[3]||'playwright');
const recoveryFixture=process.argv.includes('--writer-recovery');
const writerFixture=process.argv.includes('--writer')||recoveryFixture;

test(`Frozen candidate ${recoveryFixture?'recovers interrupted verification':'serves Team UI'} and owns two real frozen ${writerFixture?'writer':'reader'} processes`, {timeout:180000}, async()=>{
    const executable=path.resolve(process.argv[2]);
    assert.ok(fs.existsSync(executable));
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-swarm-packaged-'));
    const fixture=spawn(process.env.SWARM_PYTHON||'python',[path.join(__dirname,'fixtures/swarming_packaged_server.py'),output,executable,...(recoveryFixture?['--writer-recovery']:writerFixture?['--writer']:[])],
        {windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    fixture.stdout.on('data',chunk=>{stdout+=chunk;});
    fixture.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>fixture.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for(let i=0;i<350;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(fixture.exitCode!==null)throw Error('Packaged fixture failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Candidate startup metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(20000);
        const errors=[];
        page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);
        await page.locator(`.agent-row[data-session-id="${info.session_id}"]`).click();
        await page.waitForFunction(session=>app.currentSessionId===session,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Team objective').fill('Verify packaged native reader ownership');
        await page.locator('[data-task-objective]').nth(0).fill('Read fact.txt and inspect quoting');
        await page.locator('[data-task-objective]').nth(1).fill('Read fact.txt and inspect delimiters');
        if(writerFixture) {
            await page.getByLabel('Team objective').fill('Verify packaged isolated writer integration');
            await page.getByLabel('Allow scoped file changes').check();
            await page.getByLabel('Writable project folders').fill('src');
            await page.getByLabel('Allowance per writer').fill('4');
            await page.getByLabel('Check name',{exact:true}).fill('combined-files');
            await page.getByLabel('Executable',{exact:true}).fill(info.python);
            await page.getByLabel('Arguments, one per line').fill('verify_changes.py');
            await page.getByLabel('Check timeout in seconds').fill('20');
            for(const [i,part] of ['backend','frontend'].entries()) {
                await page.locator('[data-task-objective]').nth(i).fill(`Update src/${part}.txt`);
                await page.locator('[data-task-roots]').nth(i).fill(`src/${part}.txt`);
                await page.getByLabel('Assignment type').nth(i).selectOption('implement');
                await page.getByLabel('Writable folders for this task').nth(i).fill(`src/${part}.txt`);
                await page.getByLabel('Required check names').nth(i).fill('combined-files');
            }
        }
        await page.getByRole('button',{name:writerFixture?'Start scoped team':'Start read-only team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.filter(row=>row.state==='started').length===2);
        const during=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        const children=during.children.filter(child=>child.argv.includes('--swarm-worker'));
        assert.equal(children.length,2,'Two real private worker children must be observable while inference is gated');
        assert.ok(children.every(child=>path.resolve(child.exe).toLowerCase()===executable.toLowerCase()));
        const active=await page.evaluate(()=>app._swarmState.run);
        assert.equal(active.process_observations.length,2);
        assert.ok(active.process_observations.every(row=>children.some(child=>child.pid===row.pid)));
        assert.ok(active.process_observations.every(row=>!('launch_token' in row)));
        await fetch(info.api_url+'/__fixture__/release',{method:'POST'});
        for(const number of writerFixture?[]:[1,2]) {
            await page.getByText(`Worker ${number} · Awaiting verification`,{exact:true}).waitFor();
            await page.getByText(`Worker ${number} · Awaiting verification`,{exact:true}).click();
            const form=page.getByRole('form',{name:`Review Worker ${number}`,exact:true});
            await form.getByLabel(`Review notes for Worker ${number}`,{exact:true}).fill('Reviewed fixture fact.txt and the packaged observed read.');
            await form.getByRole('button',{name:'Accept findings',exact:true}).click();
            await page.getByText(`Worker ${number} · Accepted`,{exact:true}).waitFor();
        }
        let checkEvidence;
        if(writerFixture) {
            await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===2&&app._swarmState.run.attempts.every(row=>row.process_state==='stopped'));
            for(const part of ['backend','frontend'])assert.equal(fs.readFileSync(path.join(info.project,'src',part+'.txt'),'utf8'),`original ${part}\n`);
            await page.locator('.swarm-writer-result input').nth(0).check();
            await page.locator('.swarm-writer-result input').nth(1).check();
            await page.getByRole('button',{name:'Prepare selected changes',exact:true}).click();
            await page.getByText('Ready for verification',{exact:true}).waitFor();
            await page.getByRole('button',{name:'Inspect candidate',exact:true}).click();
            await page.getByText('This complete diff belongs to the candidate revision shown above.',{exact:true}).waitFor({state:'attached'});
            await page.getByText('Review changed files and diff',{exact:true}).click();
            assert.match(await page.locator('[data-candidate-diff]').innerText(),/verified backend/);
            assert.match(await page.locator('[data-candidate-diff]').innerText(),/verified frontend/);
            await page.getByRole('button',{name:'Run combined-files',exact:true}).click();
            for(let i=0;i<150&&!fs.existsSync(path.join(output,'check-started.json'));i++)await new Promise(resolve=>setTimeout(resolve,50));
            assert.ok(fs.existsSync(path.join(output,'check-started.json')));
            const checkIdentity=JSON.parse(fs.readFileSync(path.join(output,'check-started.json'),'utf8'));
            checkEvidence=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
            const helper=checkEvidence.children.find(child=>child.pid===checkIdentity.parent);
            assert.ok(helper?.argv.includes('--swarm-effect'),'The actual Python verifier is owned by the frozen effect helper');
            assert.equal(path.resolve(helper.exe).toLowerCase(),executable.toLowerCase());
            if(recoveryFixture) {
                const interrupted=await page.evaluate(()=>app._swarmState.run);
                const originalRun=interrupted.run.id;
                const originalCheck=interrupted.integration_checks.find(row=>row.state==='running');
                assert.ok(originalCheck,'Actual verification must be in flight before killing its host');
                const crash=await(await fetch(info.api_url+'/__fixture__/crash',{method:'POST'})).json();
                assert.equal(crash.host_pid,info.host_pid);
                assert.ok(crash.children.some(child=>child.pid===checkIdentity.pid));
                assert.deepEqual(crash.still_alive,[],'The OS-owned job must end check descendants when the GUI host dies');
                const replacement=await(await fetch(info.api_url+'/__fixture__/restart',{method:'POST'})).json();
                assert.notEqual(replacement.host_pid,info.host_pid);
                await page.goto(info.url);
                await page.locator(`.agent-row[data-session-id="${info.session_id}"]`).click();
                await page.waitForFunction(session=>app.currentSessionId===session,info.session_id);
                await page.getByRole('button',{name:'Team',exact:true}).click();
                await page.waitForFunction(()=>app._swarmState?.run?.recovery_needed===true);
                assert.equal((await(await fetch(info.api_url+'/__fixture__/evidence')).json()).requests.length,4,'Reopening cannot invoke a model');
                // Observe real lease expiry; do not patch the packaged clock or durable state.
                await page.waitForFunction(()=>{
                    const button=[...document.querySelectorAll('button')].find(node=>node.textContent==='Take over expired team');
                    const leaseUntil=app._swarmState?.run?.run?.lease_until;
                    return button&&!button.disabled&&Number.isFinite(leaseUntil)&&Date.now()>=leaseUntil*1000;
                },null,{timeout:75000});
                await page.getByRole('button',{name:'Take over expired team',exact:true}).click();
                await page.getByText('This host owns recovery. Check execution, then reconcile each uncertain observation.',{exact:true}).waitFor();
                const checkForm=page.getByRole('form',{name:`Interrupted verification check: ${originalCheck.id}`,exact:true});
                await checkForm.getByLabel('Recovery notes').fill('Inspected the retained named-job process identity after the frozen host was killed.');
                await page.waitForTimeout(1100);
                assert.equal(await checkForm.getByLabel('Recovery notes').evaluate(node=>document.activeElement===node),true);
                await checkForm.getByRole('button',{name:'Inspect and reconcile',exact:true}).focus();
                await page.keyboard.press('Enter');
                await page.waitForFunction(id=>app._swarmState?.run?.integration_checks?.find(row=>row.id===id)?.state==='cancelled',originalCheck.id);
                const operation=page.getByRole('form',{name:/^Interrupted integration operation:/}).first();
                await operation.getByLabel('Recovery notes').fill('Reconcile the exact interrupted verification operation from the observed process cleanup.');
                await operation.getByRole('button',{name:'Inspect and reconcile',exact:true}).click();
                await page.waitForFunction(()=>app._swarmState.run.integration_operations.every(row=>!['queued','running','uncertain'].includes(row.state)));
                await page.getByRole('button',{name:'Stop team',exact:true}).click();
                await page.getByRole('button',{name:'Finish stopped team',exact:true}).click();
                await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='cancelled');
                const recovered=await page.evaluate(()=>app._swarmState.run);
                assert.equal(recovered.run.id,originalRun);
                assert.equal(recovered.run.epoch,interrupted.run.epoch+1);
                const finalCheck=recovered.integration_checks.find(row=>row.id===originalCheck.id);
                assert.equal(finalCheck.state,'cancelled');
                assert.equal(finalCheck.exit_code,null,'Process termination must not invent check success or command exit status');
                assert.ok(recovered.integration_processes.filter(row=>row.effect_kind==='check').every(row=>row.state==='stopped'));
                assert.equal(recovered.writer_acceptances.length,0);
                for(const part of ['backend','frontend'])assert.equal(fs.readFileSync(path.join(info.project,'src',part+'.txt'),'utf8'),`original ${part}\n`);
                await page.setViewportSize({width:390,height:844});
                await page.screenshot({path:path.join(output,'packaged-recovered-check.png')});
                const transport=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
                assert.equal(transport.requests.length,4);
                assert.deepEqual(errors,[]);
                fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({candidate:info,during,checkEvidence,interrupted,crash,replacement,recovered,transport,errors},null,2));
                console.log('Packaged actual host-crash recovery evidence:',output);
                return;
            }
            fs.writeFileSync(path.join(output,'check-release'),'release');
            await page.getByText('Checks passed for this exact revision',{exact:true}).waitFor();
            await page.getByLabel('Application review notes').fill('Reviewed packaged exact diff and combined verification receipt.');
            fs.writeFileSync(path.join(info.project,'personal.txt'),'fixture unfinished edit\n');
            await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
            await page.waitForFunction(()=>app._swarmState.run.integration_operations.some(row=>row.kind==='apply'&&row.state==='failed'));
            assert.equal(fs.readFileSync(path.join(info.project,'personal.txt'),'utf8'),'fixture unfinished edit\n');
            fs.writeFileSync(path.join(info.project,'personal.txt'),'committed personal\n');
            await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
            await page.getByText('Changes applied; task acceptance remains separate',{exact:true}).waitFor();
            for(const part of ['backend','frontend']) {
                const form=page.getByRole('form',{name:`Accept applied result: Update src/${part}.txt`,exact:true});
                await form.getByLabel('Acceptance notes').fill('Reviewed exact applied revision and packaged check.');
                await form.getByRole('button',{name:'Accept applied result',exact:true}).click();
                await form.waitFor({state:'hidden'});
            }
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='completed');
        const final=await page.evaluate(()=>app._swarmState.run);
        assert.equal(final.action_receipts.filter(row=>row.tool_name===(writerFixture?'file_write':'file_read')&&row.state==='completed').length,2);
        if(writerFixture) {
            assert.equal(final.writer_acceptances.length,2);
            assert.equal(final.integration_checks[0].state,'passed');
            assert.ok(final.integration_processes.some(row=>row.effect_kind==='check'&&row.state==='stopped'&&row.exit_code===0));
            assert.ok(final.integration_processes.every(row=>!('launch_token' in row)&&!('cwd' in row)));
        }
        assert.equal(final.model_requests.length,4);
        assert.ok(final.attempts.every(row=>row.process_state==='stopped'));
        const transport=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        assert.equal(transport.requests.length,4);
        assert.equal(transport.requests.filter(row=>row.observed_file_result).length,2);
        assert.equal(transport.children.filter(child=>child.argv.includes('--swarm-worker')).length,0);
        await page.setViewportSize({width:390,height:844});
        await page.screenshot({path:path.join(output,'packaged-team-complete.png')});
        const downloadPromise=page.waitForEvent('download');
        await page.getByRole('button',{name:'Export run report',exact:true}).click();
        const download=await downloadPromise;
        const reportPath=path.join(output,'run-report.json');
        await download.saveAs(reportPath);
        const report=JSON.parse(fs.readFileSync(reportPath,'utf8'));
        assert.equal(report.run.state,'completed');
        assert.equal(report.accounting.observed_used_request_units,4);
        for(const name of ['swarm_view.js','swarm_view.css']) {
            const response=await fetch(info.url+'/static/'+name);
            assert.equal(response.status,200);
            assert.equal(Buffer.compare(Buffer.from(await response.arrayBuffer()),fs.readFileSync(path.join(path.dirname(executable),'_internal','lumi','gui','static',name))),0);
        }
        assert.deepEqual(errors,[]);
        const startupLog=fs.readFileSync(path.join(output,'candidate-stream.log'),'utf8');
        assert.match(startupLog,/Application startup complete/);
        assert.match(startupLog,/WebSocket.*accepted/);
        assert.ok(!startupLog.includes('Traceback (most recent call last)'),'Candidate startup or request traceback');
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({candidate:info,during,checkEvidence,final,transport,errors},null,2));
        console.log('Packaged scripted native process evidence:',output);
    } catch(error) {
        if(page){
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>({state:app._swarmState,
                currentCwd:app.currentCwd,currentSessionId:app.currentSessionId,scope:app._swarmScope,pending:app._swarmPending})).catch(()=>({})),null,2));
        }
        console.error('Packaged failure evidence:',output);
        throw error;
    } finally {
        if(browser)await browser.close();
        fixture.stdin.end('stop\n');
        let timer;
        const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),12000);})]);
        clearTimeout(timer);
        if(!result)fixture.kill();
        fs.writeFileSync(path.join(output,'fixture.log'),stdout+'\n'+stderr);
        const startup=path.join(output,'home','.lumi','logs','lumi-startup.log');
        if(fs.existsSync(startup))fs.copyFileSync(startup,path.join(output,'startup.log'));
    }
});
