/* Frozen desktop reference scenario: scripted CSV writers, real failed check,
 * explicit repair, exact combined verification, dirty checkout, and acceptance.
 * References are scripted fixture output, never a measured model result.
 * node tests/swarm_packaged_csv.browser.cjs <candidate-exe> <playwright-module>
 */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawn,spawnSync}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[3]||'playwright');

test('Frozen CSV team retains failed verification and completes an explicitly repaired candidate',{timeout:160000},async()=>{
    const executable=path.resolve(process.argv[2]);
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-swarm-packaged-csv-'));
    const fixture=spawn(process.env.SWARM_PYTHON||'python',[path.join(__dirname,'fixtures/swarming_packaged_server.py'),output,executable,'--csv'],
        {windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    const commands=[],responses=[],interventions=[];
    fixture.stdout.on('data',chunk=>{stdout+=chunk;});
    fixture.stderr.on('data',chunk=>{stderr+=chunk;});
    const exited=new Promise(resolve=>fixture.once('exit',resolve));
    try {
        for(let i=0;i<350;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(fixture.exitCode!==null)throw Error('Fixture startup failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Frozen fixture startup metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ?{headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(25000);
        const errors=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>{
            socket.on('framesent',frame=>{
                const row=JSON.parse(frame.payload);
                if(row.command==='swarm')commands.push(row);
            });
            socket.on('framereceived',frame=>{
                const row=JSON.parse(frame.payload);
                if(row.event==='swarm_state')responses.push(row);
            });
        });
        async function rejectWithCurrentRevision(button){
            for(let attempt=0;attempt<2;attempt++){
                await page.waitForFunction(()=>!app._swarmPending);
                const offset=commands.length;
                await button.click();
                let sent,response;
                for(let n=0;n<100;n++){
                    sent=commands.slice(offset).find(row=>row.action==='reject_result');
                    response=sent&&responses.find(row=>row.request_id===sent.request_id);
                    if(response)break;
                    await page.waitForTimeout(50);
                }
                assert.ok(response,'Missing rejection reply is not permission to replay');
                if(!response.error)return;
                assert.equal(response.error,'Run revision changed; refresh the snapshot');
                assert.equal(attempt,0,'Only one explicitly rejected stale decision may be refreshed');
                interventions.push({kind:'owner_refresh_after_definite_revision_rejection',command:sent,response});
                await page.getByRole('button',{name:'Refresh team',exact:true}).click();
                await page.waitForFunction(()=>!app._swarmPending);
                // The original task and notes are retained. The next click is a
                // new owner decision with a new command ID and current revision.
            }
        }
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(info.url);
        await page.locator(`.agent-row[data-session-id="${info.session_id}"]`).click();
        await page.waitForFunction(session=>app.currentSessionId===session,info.session_id);
        await page.locator('#user-input').fill('Keep the project draft through CSV repair.');
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Team objective').fill('Implement the shared GET /export.csv contract with correctly quoted CSV and an enabled export form.');
        await page.getByLabel('Allow scoped file changes').check();
        await page.getByLabel('Writable project folders').fill(info.writer_cases.map(item=>item.path).join(','));
        await page.getByLabel('Allowance per writer').fill('4');
        await page.getByLabel('Check name',{exact:true}).fill('csv-reference');
        await page.getByLabel('Executable',{exact:true}).fill(info.python);
        await page.getByLabel('Arguments, one per line').fill(info.check_script);
        await page.getByLabel('Check timeout in seconds').fill('30');
        for(const [index,item] of info.writer_cases.entries()) {
            await page.locator('[data-task-objective]').nth(index).fill(`Update ${item.path}`);
            await page.locator('[data-task-roots]').nth(index).fill(item.path+',app.py,rows.json');
            await page.getByLabel('Assignment type').nth(index).selectOption('implement');
            await page.getByLabel('Writable folders for this task').nth(index).fill(item.path);
            await page.getByLabel('Required check names').nth(index).fill('csv-reference');
        }
        await page.getByRole('button',{name:'Start scoped team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.filter(row=>row.state==='started').length===2);
        const during=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        assert.equal(during.children.filter(child=>child.argv.includes('--swarm-worker')).length,2);
        await fetch(info.api_url+'/__fixture__/release',{method:'POST'});
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===2&&app._swarmState.run.attempts.every(row=>row.process_state==='stopped'));
        for(const item of info.writer_cases)assert.equal(fs.readFileSync(path.join(info.project,item.path),'utf8'),item.original);
        for(const index of [0,1])await page.locator('.swarm-writer-result input').nth(index).check();
        await page.getByRole('button',{name:'Prepare selected changes',exact:true}).click();
        const first=page.locator('.swarm-candidate').first();
        await first.getByText('Ready for verification',{exact:true}).waitFor();
        await first.getByRole('button',{name:'Run csv-reference',exact:true}).click();
        for(let i=0;i<200&&!fs.existsSync(path.join(output,'check-started.json'));i++)await new Promise(resolve=>setTimeout(resolve,50));
        assert.ok(fs.existsSync(path.join(output,'check-started.json')));
        const checkIdentity=JSON.parse(fs.readFileSync(path.join(output,'check-started.json'),'utf8'));
        const checkEvidence=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        assert.ok(checkEvidence.children.some(child=>child.pid===checkIdentity.parent&&child.argv.includes('--swarm-effect')));
        fs.writeFileSync(path.join(output,'check-release'),'release');
        await first.getByText('Verification needs review',{exact:true}).waitFor();
        assert.equal(await first.getByRole('button',{name:'Apply reviewed changes',exact:true}).isEnabled(),false);
        const failed=await page.evaluate(()=>app._swarmState.run);
        assert.equal(failed.integration_checks[0].exit_code,1);
        assert.equal(failed.run.state,'running');
        assert.equal(failed.writer_acceptances.length,0);
        const initialReceipt=fs.readdirSync(output).find(name=>name.startsWith('check-result-'));
        const rejectedEvidence=JSON.parse(fs.readFileSync(path.join(output,initialReceipt),'utf8'));
        assert.equal(rejectedEvidence.accepted,false);
        assert.ok(rejectedEvidence.checks.some(row=>row.name==='frontend_export_control'&&!row.passed));
        await page.getByText('Worker 2 · Awaiting verification',{exact:true}).click();
        const reject=page.getByRole('form',{name:'Reject Worker 2',exact:true});
        await reject.getByLabel('Rejection notes for Worker 2').fill('The real frontend_export_control check found /pending instead of the shared /export.csv route.');
        await rejectWithCurrentRevision(reject.getByRole('button',{name:'Reject result',exact:true}));
        const retry=page.getByRole('form',{name:'Retry Worker 2',exact:true});
        await retry.getByLabel('Retry notes for Worker 2').fill('Repair only the frontend route; preserve the backend result and failed check evidence.');
        await retry.getByRole('button',{name:'Retry task',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===3&&app._swarmState.run.attempts.every(row=>row.process_state==='stopped'));
        for(const index of [0,2])await page.locator('.swarm-writer-result input').nth(index).check();
        await page.getByRole('button',{name:'Prepare selected changes',exact:true}).click();
        const candidate=page.locator('.swarm-candidate').nth(1);
        await candidate.getByText('Ready for verification',{exact:true}).waitFor();
        await candidate.getByRole('button',{name:'Inspect candidate',exact:true}).click();
        await candidate.getByText('This complete diff belongs to the candidate revision shown above.',{exact:true}).waitFor({state:'attached'});
        await candidate.getByRole('button',{name:'Run csv-reference',exact:true}).click();
        await candidate.getByText('Checks passed for this exact revision',{exact:true}).waitFor();
        await candidate.getByLabel('Application review notes').fill('Reviewed exact CSV backend/frontend changes and real quoting, form and HTTP checks after repair.');
        fs.writeFileSync(path.join(info.project,'personal.txt'),'unfinished fixture edit\n');
        await candidate.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState.run.integration_operations.some(row=>row.kind==='apply'&&row.state==='failed'));
        assert.equal(fs.readFileSync(path.join(info.project,'personal.txt'),'utf8'),'unfinished fixture edit\n');
        fs.writeFileSync(path.join(info.project,'personal.txt'),'committed personal\n');
        await candidate.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
        await candidate.getByText('Changes applied; task acceptance remains separate',{exact:true}).waitFor();
        const appliedRevision=await page.evaluate(()=>app._swarmState.run.integration_candidates.at(-1).result_revision);
        for(const item of info.writer_cases) {
            const actual=fs.readFileSync(path.join(info.project,item.path));
            // Native file_write uses the platform's newline convention. The
            // applied bytes must still equal the exact verified Git candidate.
            assert.equal(actual.toString('utf8').replace(/\r\n/g,'\n'),item.expected);
            const retained=spawnSync('git',['-C',info.project,'cat-file','blob',`${appliedRevision}:${item.path}`],{windowsHide:true});
            assert.equal(retained.status,0,retained.stderr.toString());
            assert.equal(Buffer.compare(actual,retained.stdout),0);
            const form=candidate.getByRole('form',{name:`Accept applied result: Update ${item.path}`,exact:true});
            await form.getByLabel('Acceptance notes').fill('The repaired exact applied revision passes all declared CSV reference checks.');
            await form.getByRole('button',{name:'Accept applied result',exact:true}).click();
            await form.waitFor({state:'hidden'});
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='completed');
        const final=await page.evaluate(()=>app._swarmState.run);
        assert.equal(final.integration_checks.length,2);
        assert.deepEqual(final.integration_checks.map(row=>row.state),['failed','passed']);
        assert.notEqual(final.integration_checks[0].candidate_revision,final.integration_checks[1].candidate_revision);
        assert.equal(final.attempts.length,3);
        assert.equal(final.writer_acceptances.length,2);
        assert.equal(final.model_requests.length,6);
        assert.ok(final.attempts.every(row=>row.process_state==='stopped'));
        const transport=await(await fetch(info.api_url+'/__fixture__/evidence')).json();
        assert.equal(transport.requests.length,6);
        assert.equal(transport.children.filter(child=>child.argv.includes('--swarm-worker')).length,0);
        await page.setViewportSize({width:390,height:844});
        assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'packaged-csv-complete.png')});
        const downloading=page.waitForEvent('download');
        await page.getByRole('button',{name:'Export run report',exact:true}).click();
        await(await downloading).saveAs(path.join(output,'run-report.json'));
        assert.equal(JSON.parse(fs.readFileSync(path.join(output,'run-report.json'),'utf8')).run.state,'completed');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#user-input').inputValue(),'Keep the project draft through CSV repair.');
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({candidate:info,during,checkEvidence,failed,rejectedEvidence,final,transport,errors,
            commands,responses:responses.filter(row=>row.error),interventions,
            retained_prior_failure:{directory:'sonn-swarm-packaged-csv-16B5I5',notice:'Run revision changed; refresh the snapshot',
                effect:'The rejected owner command did not create a retryable result; both submissions remained unchanged.'}},null,2));
        console.log('Packaged CSV repair evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState).catch(()=>({})),null,2));
        }
        fs.writeFileSync(path.join(output,'command-evidence.json'),JSON.stringify({commands,responses:responses.filter(row=>row.error),interventions},null,2));
        console.error('Packaged CSV failure evidence:',output);throw error;
    } finally {
        if(browser)await browser.close();
        fixture.stdin.end('stop\n');
        let timer;
        const ended=await Promise.race([exited.then(()=>true),new Promise(resolve=>{timer=setTimeout(()=>resolve(false),12000);})]);
        clearTimeout(timer);if(!ended)fixture.kill();
        fs.writeFileSync(path.join(output,'fixture.log'),stdout+'\n'+stderr);
    }
});
