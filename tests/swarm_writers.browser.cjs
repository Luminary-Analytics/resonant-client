/* Actual source UI/WS/SQLite/Git/check subprocess; trusted scripted thread writers.
 * This is neither live-model evidence nor production child-process qualification.
 * node tests/swarm_writers.browser.cjs [absolute-path-to-playwright-module]
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

test('Source Team UI reviews two isolated writers, verifies, applies and accepts', {timeout: 90000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'sonn-swarm-writers-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--writer'],
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
        page.setDefaultTimeout(20000);
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
        await page.getByLabel('Team objective').fill('Update the independent backend and frontend fixture files');
        assert.equal(await page.getByLabel('Allow scoped file changes').isChecked(),false);
        await page.getByLabel('Allow scoped file changes').check();
        await page.getByLabel('Writable project folders').fill('src');
        await page.getByLabel('Allowance per writer').fill('4');
        await page.getByLabel('Check name',{exact:true}).fill('combined-files');
        await page.getByLabel('Executable',{exact:true}).fill(info.python);
        await page.getByLabel('Arguments, one per line').fill('verify_changes.py');
        await page.getByLabel('Check timeout in seconds').fill('20');
        for(const [i,part] of ['backend','frontend'].entries()) {
            await page.locator('[data-task-objective]').nth(i).fill(`Update src/${part}.txt`);
            await page.locator('[data-task-roots]').nth(i).fill('src');
            await page.getByLabel('Assignment type').nth(i).selectOption('implement');
            await page.getByLabel('Writable folders for this task').nth(i).fill(`src/${part}.txt`);
            await page.getByLabel('Required check names').nth(i).fill('combined-files');
        }
        await page.setViewportSize({width:390,height:844});
        await page.getByRole('button',{name:'Start scoped team',exact:true}).scrollIntoViewIfNeeded();
        const dialog=page.getByRole('dialog',{name:'Work together'});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'source-writer-setup-compact.png')});
        await page.getByRole('button',{name:'Start scoped team',exact:true}).focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===2
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending);
        assert.equal(commands.filter(row=>row.action==='start').length,1);
        assert.deepEqual(commands.find(row=>row.action==='start').checks,[{key:'combined-files',argv:[info.python,'verify_changes.py'],timeout_seconds:20}]);
        for(const part of ['backend','frontend'])assert.equal(fs.readFileSync(path.join(info.workspace,'src',part+'.txt'),'utf8'),`original ${part}\n`);
        const initial=await page.evaluate(()=>app._swarmState.run);
        assert.equal(initial.writer_worktrees.length,2);
        assert.equal(initial.writer_setup.target_branch,'refs/heads/main');
        await page.getByText('Worker 1 · Awaiting verification',{exact:true}).click();
        assert.equal(await page.getByRole('button',{name:'Accept findings',exact:true}).count(),0);
        await page.locator('.swarm-writer-result input').nth(0).check();
        await page.locator('.swarm-writer-result input').nth(1).check();
        await page.getByRole('button',{name:'Prepare selected changes',exact:true}).click();
        await page.getByText('Ready for verification',{exact:true}).waitFor();
        await page.getByRole('button',{name:'Inspect candidate',exact:true}).click();
        await page.getByText('This complete diff belongs to the candidate revision shown above.',{exact:true}).waitFor({state:'attached'});
        await page.getByText('Review changed files and diff',{exact:true}).click();
        assert.match(await page.locator('[data-candidate-diff]').innerText(),/verified backend/);
        assert.match(await page.locator('[data-candidate-diff]').innerText(),/verified frontend/);
        assert.equal(await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).isEnabled(),false);
        await page.getByRole('button',{name:'Run combined-files',exact:true}).click();
        await page.getByText('Checks passed for this exact revision',{exact:true}).waitFor();
        await page.getByText('Check output',{exact:true}).click();
        await page.getByText('Both independently written files verified',{exact:true}).waitFor();
        await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
        assert.equal(commands.filter(row=>row.action==='apply_candidate').length,0,'Application needs explicit review notes');
        await page.getByLabel('Application review notes').fill('Reviewed the complete backend/frontend diff and the combined-files check for this exact candidate.');
        await page.waitForTimeout(1150);
        assert.equal(await page.getByLabel('Application review notes').evaluate(node=>document.activeElement===node),true);
        await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).scrollIntoViewIfNeeded();
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'source-writer-review-compact.png')});
        // A user edit after worker dispatch must remain intact. The UI sends a
        // reviewed apply; the real Git integration denies it while dirty.
        fs.writeFileSync(path.join(info.workspace,'personal.txt'),'unfinished personal edit\n');
        await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState.run.integration_operations.some(row=>row.kind==='apply'&&row.state==='failed')
            || document.querySelector('[data-swarm="notice"]').textContent.includes('Run revision changed before integration admission'));
        if((await page.locator('[data-swarm="notice"]').textContent()).includes('Run revision changed before integration admission')){
            // A concurrent durable observation may invalidate the displayed
            // revision. Model an explicit owner refresh/decision; the product
            // must not replay an ambiguous application automatically.
            assert.equal((await page.evaluate(()=>app._swarmState.run.integration_applications)).length,0);
            await page.getByRole('button',{name:'Refresh team',exact:true}).click();
            await page.waitForFunction(()=>!app._swarmPending);
            assert.equal(await page.getByLabel('Application review notes').inputValue(),'Reviewed the complete backend/frontend diff and the combined-files check for this exact candidate.');
            await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
        }
        await page.waitForFunction(()=>app._swarmState.run.integration_operations.some(row=>row.kind==='apply'&&row.state==='failed'));
        assert.equal(fs.readFileSync(path.join(info.workspace,'personal.txt'),'utf8'),'unfinished personal edit\n');
        for(const part of ['backend','frontend'])assert.equal(fs.readFileSync(path.join(info.workspace,'src',part+'.txt'),'utf8'),`original ${part}\n`);
        // Restore only this test's authored edit; no checkout/reset/stash.
        fs.writeFileSync(path.join(info.workspace,'personal.txt'),'committed personal\n');
        await page.getByRole('button',{name:'Apply reviewed changes',exact:true}).click();
        await page.getByText('Changes applied; task acceptance remains separate',{exact:true}).waitFor();
        const applied=await page.evaluate(()=>app._swarmState.run);
        assert.ok(applied.work_items.every(row=>row.state==='submitted'));
        assert.equal(await page.getByRole('button',{name:'Complete team',exact:true}).count(),0);
        for(const part of ['backend','frontend']) {
            const form=page.getByRole('form',{name:`Accept applied result: Update src/${part}.txt`,exact:true});
            await form.getByLabel('Acceptance notes').fill(`Reviewed the applied ${part} file and its exact combined-files check receipt.`);
            await form.getByRole('button',{name:'Accept applied result',exact:true}).click();
            await form.waitFor({state:'hidden'});
        }
        await page.getByRole('button',{name:'Complete team',exact:true}).click();
        await page.locator('[data-swarm="run-state"]').filter({hasText:'Complete'}).waitFor();
        for(const part of ['backend','frontend'])assert.equal(fs.readFileSync(path.join(info.workspace,'src',part+'.txt'),'utf8').replace(/\r\n/g,'\n'),`verified ${part}\n`);
        const downloadPromise=page.waitForEvent('download');
        await page.getByRole('button',{name:'Export run report',exact:true}).click();
        const download=await downloadPromise;
        const reportPath=path.join(output,download.suggestedFilename());
        await download.saveAs(reportPath);
        const report=JSON.parse(fs.readFileSync(reportPath,'utf8'));
        assert.equal(report.format,'sonn-swarm-report');
        assert.equal(report.content_included,false);
        assert.equal(report.run.id,initial.run.id);
        assert.equal(report.run.state,'completed');
        assert.equal(report.accounting.observed_used_request_units,4);
        assert.equal(report.accounting.usage.reported_usd.unknown_requests,4);
        assert.equal(report.accounting.usage.reported_usd.complete,false);
        assert.equal(report.checks[0].state,'passed');
        assert.equal(report.writer_acceptances.length,2);
        assert.equal(fs.readFileSync(reportPath,'utf8').includes('Reviewed the complete backend/frontend'),false);
        assert.equal(download.suggestedFilename(),`lumi-team-${initial.run.id}.json`);
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        const result=evidence.runs[0].run;
        assert.equal(evidence.backend_requests,4);
        assert.equal(result.run.state,'completed');
        assert.equal(result.writer_acceptances.length,2);
        assert.equal(result.integration_checks[0].state,'passed');
        assert.equal(result.action_receipts.filter(row=>row.tool_name==='file_write'&&row.state==='completed').length,2);
        assert.ok(commands.filter(row=>['prepare_candidate','run_check','apply_candidate','accept_writer'].includes(row.action)).every(row=>Number.isInteger(row.expected_revision)&&row.run_id===result.run.id));
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({...evidence,commands},null,2));
        console.log('Source-app scripted writer browser evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState).catch(()=>({})),null,2));
        }
        console.error('Writer browser failure evidence:',output);
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
