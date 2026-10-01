/* An orchestrated writer team that applies checked changes, in the full source app:
 * real WebSocket, runtime, store, Git and check subprocess; inference scripted.
 * The conversation is in Full-auto (the fixture's --mode bypass), so the team
 * starts without asking; swarm_autonomous.browser.cjs covers the one-run grant
 * a conversation in another mode is offered.
 * node tests/swarm_autonomous_writers.browser.cjs [absolute-path-to-playwright-module]
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

test('The orchestrator applies writers’ checked changes and reports in Markdown', {timeout: 120000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-swarm-autonomous-writers-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--autonomous', '--writer', '--mode', 'bypass'],
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
        const errors=[];
        const starts=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>socket.on('framesent',frame=>{
            const data=JSON.parse(frame.payload);
            if(data.command==='swarm'&&data.action==='start')starts.push(data);
        }));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>window.app?.currentSessionId===session,info.session_id);
        assert.equal(await page.locator('#perm-label').textContent(),'Full-auto');
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Planning approach').selectOption('coordinator');
        await page.getByLabel('Team objective').fill('Update the backend and frontend fixture files');
        await page.getByLabel('Let the orchestrator run the team').check();
        const apply=page.getByLabel('Apply changes that pass every check');
        // Applying needs writer access: the switch appears only with it.
        assert.equal(await apply.isVisible(),false);
        await page.getByLabel('Allow scoped file changes').check();
        await apply.waitFor();
        assert.equal(await apply.isChecked(),false);
        await page.getByText('File changes still wait for you.',{exact:true}).waitFor();
        await apply.focus();
        await page.keyboard.press('Space');
        assert.equal(await apply.isChecked(),true);
        await page.getByText('Writers’ changes are combined and applied to your checkout once every declared check passes',{exact:false}).waitFor();
        assert.equal(await page.getByText('File changes still wait for you.',{exact:true}).isVisible(),false);
        await page.getByLabel('Allow scoped file changes').uncheck();
        assert.equal(await apply.isVisible(),false);
        await page.getByLabel('Allow scoped file changes').check();
        await page.getByLabel('Writable project folders').fill('src');
        await page.getByLabel('Check name',{exact:true}).fill('combined-files');
        await page.getByLabel('Executable',{exact:true}).fill(info.python);
        await page.getByLabel('Arguments, one per line').fill('verify_changes.py');
        await page.getByLabel('Check timeout in seconds').fill('20');
        await page.getByLabel('Orchestrator rounds').fill('1');
        await page.getByLabel('Total model requests').fill('20');
        await page.getByRole('button',{name:'Start orchestrated team'}).click();
        const report=page.locator('[data-swarm="orchestrator-report-text"]');
        await report.waitFor({state:'visible',timeout:60000});
        // The model's Markdown renders through the chat's sanitizer.
        assert.equal(await report.locator('strong').innerText(),'Final report:');
        assert.deepEqual(await report.locator('li code').allInnerTexts(),['src/backend.txt','src/frontend.txt']);
        await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='completed');
        // Already in Full-auto: no notice, one Start, and no grant sent with it.
        assert.equal(starts.length,1);
        assert.equal(starts[0].full_auto,undefined);
        assert.equal(await page.locator('.swarm-full-auto-grant').count(),0);
        assert.deepEqual(starts[0].autonomy,{rounds:1,apply:true});
        assert.deepEqual(starts[0].checks,[{key:'combined-files',argv:[info.python,'verify_changes.py'],timeout_seconds:20}]);
        const status=await page.locator('[data-swarm="orchestrator-status"]').innerText();
        assert.match(status,/Applies checked changes · The orchestrator finished the objective\./);
        for(const part of ['backend','frontend'])  // Windows checkouts may write CRLF.
            assert.equal(fs.readFileSync(path.join(info.workspace,'src',part+'.txt'),'utf8').replace(/\r\n/g,'\n'),`verified ${part}\n`);
        assert.equal(fs.readFileSync(path.join(info.workspace,'personal.txt'),'utf8'),'committed personal\n');
        // Phone width: the report and setup wrap inside the panel.
        await page.setViewportSize({width:390,height:844});
        const overflow=await page.evaluate(()=>{
            const body=document.querySelector('.swarm-body');
            return {scroll:body.scrollWidth,client:body.clientWidth};
        });
        assert.ok(overflow.scroll<=overflow.client+1,'Team panel scrolls sideways at phone width: '+JSON.stringify(overflow));
        await page.screenshot({path:path.join(output,'orchestrated-writers.png'),fullPage:false});
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        const run=evidence.runs[0].run;
        assert.equal(run.run.state,'completed');
        assert.deepEqual(run.integration_candidates.map(row=>row.state),['applied']);
        assert.deepEqual(run.integration_operations.map(row=>row.kind),['prepare_candidate','run_check','apply']);
        assert.equal(run.writer_acceptances.length,2);
        assert.ok(run.writer_acceptances.every(row=>row.owner_id.startsWith('autonomy:')));
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify(evidence,null,2));
        console.log('Orchestrated writer team browser evidence:',output);
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
