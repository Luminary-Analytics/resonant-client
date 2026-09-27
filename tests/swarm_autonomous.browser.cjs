/* An orchestrated team in the full source app: real WebSocket, runtime and store; inference scripted.
 * node tests/swarm_autonomous.browser.cjs [absolute-path-to-playwright-module]
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

test('The orchestrator runs a team from the panel and reports back', {timeout: 90000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-swarm-autonomous-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--autonomous'],
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
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Planning approach').selectOption('coordinator');
        const autonomy=page.getByLabel('Let the orchestrator run the team');
        // Off until chosen; the rounds field appears only with it.
        assert.equal(await autonomy.isChecked(),false);
        assert.equal(await page.getByLabel('Orchestrator rounds').isVisible(),false);
        await autonomy.focus();
        await page.keyboard.press('Space');
        assert.equal(await autonomy.isChecked(),true);
        // The chat's /team opens the same panel with the objective and the orchestrator filled in.
        await page.getByRole('button',{name:'Close team panel'}).click();
        await page.locator('#user-input').fill('/team Check how the CSV export handles delimiters');
        await page.locator('#user-input').press('Enter');
        await page.getByRole('dialog',{name:'Work together'}).waitFor();
        assert.equal(await page.getByLabel('Team objective').inputValue(),'Check how the CSV export handles delimiters');
        assert.equal(await page.getByLabel('Planning approach').inputValue(),'coordinator');
        assert.equal(await page.getByLabel('Let the orchestrator run the team').isChecked(),true);
        assert.equal(await page.evaluate(()=>document.activeElement?.dataset.swarm),'rounds');
        assert.equal(await page.locator('#user-input').inputValue(),'');
        await page.keyboard.press('Control+A');
        await page.keyboard.type('2');
        await page.getByText('findings it uses are marked accepted by the orchestrator, not reviewed by you',{exact:false}).waitFor();
        // Workers can run on another model than the orchestrator's (the session's).
        await page.getByLabel('Worker model').selectOption({label:'ollama · fixture-worker'});
        await page.getByLabel('Total model requests').fill('20');
        // While the orchestrator runs the team, the panel asks the owner for none
        // of the decisions it takes, and offers no follow-up planning of its own.
        await page.evaluate(()=>{
            window.__ownerPrompts=[]; window.__orchestratedInbox=false; window.__followupShown=false;
            new MutationObserver(()=>{
                const text=document.querySelector('[data-swarm="inbox"]')?.innerText||'';
                if(/your review|awaiting independent verification|review before another attempt/.test(text))window.__ownerPrompts.push(text);
                if(text.includes('making the team’s decisions under your grant'))window.__orchestratedInbox=true;
                const form=document.querySelector('[data-swarm="followup-form"]');
                if(form&&!form.hidden&&app._swarmState?.autonomy?.active)window.__followupShown=true;
            }).observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true});
        });
        await page.getByRole('button',{name:'Start orchestrated team'}).click();
        const report=page.locator('[data-swarm="orchestrator-report-text"]');
        await report.waitFor({state:'visible',timeout:45000});
        const text=await report.innerText();
        assert.match(text,/^Final report: quoted CSV fields preserve commas\. The orchestrator read 2 findings and 1 worker question/);
        await page.waitForFunction(()=>app._swarmState?.run?.run?.state==='completed');
        assert.deepEqual(await page.evaluate(()=>window.__ownerPrompts),[]);
        assert.equal(await page.evaluate(()=>window.__orchestratedInbox),true);
        assert.equal(await page.evaluate(()=>window.__followupShown),false);
        // Orchestrator turns say what each was for.
        const purposes=await page.evaluate(()=>[...document.querySelectorAll('.swarm-worker')].map(node=>node.textContent).join('\n'));
        assert.ok(purposes.includes('Plan the team’s work, or write its report')&&purposes.includes('Answer workers’ questions'),purposes.slice(0,2000));
        assert.equal(starts.length,1);
        assert.deepEqual(starts[0].autonomy,{rounds:2});
        assert.equal(starts[0].plan_mode,'coordinator');
        // The team's own messages, readable in the panel.
        // The worker waited for its answer, and the orchestrator answered in the same round.
        const summary=page.locator('[data-swarm="messages-summary"]');
        assert.equal(await summary.innerText(),'Team messages (2)');
        await summary.click();
        await page.getByText('Worker 1 → Orchestrator · Question',{exact:true}).waitFor();
        await page.getByText('Should the CSV export also be checked for semicolons?',{exact:true}).waitFor();
        await page.getByText('Orchestrator → Worker 1 · Answer',{exact:true}).waitFor();
        await page.getByText('No: the export writes commas only, so semicolons need no check.',{exact:true}).waitFor();
        const status=await page.locator('[data-swarm="orchestrator-status"]').innerText();
        assert.match(status,/The orchestrator finished the objective\./);
        assert.equal(await page.locator('[data-swarm="worker-model-note"]').innerText(),'Workers use ollama · fixture-worker; the orchestrator uses this session’s model.');
        // The last orchestrator turn reads as its report, and decisions aren't attributed to the owner.
        await page.getByRole('heading',{name:'Orchestrator report'}).waitFor();
        await page.getByText('No more work proposed: this is the final report.',{exact:true}).waitFor();
        assert.equal(await page.getByText(/^Owner decision:/).count(),0);
        assert.equal(await page.getByText(/^Decision: Accepted by the team.s orchestrator/).count(),2);
        // Worker cards name the coordinator as the orchestrator in this team.
        await page.locator('.swarm-worker summary').filter({hasText:'Orchestrator'}).first().waitFor();
        // Phone width: the report and messages wrap inside the panel.
        await page.setViewportSize({width:390,height:844});
        const overflow=await page.evaluate(()=>{
            const body=document.querySelector('.swarm-body');
            return {scroll:body.scrollWidth,client:body.clientWidth};
        });
        assert.ok(overflow.scroll<=overflow.client+1,'Team panel scrolls sideways at phone width: '+JSON.stringify(overflow));
        await page.screenshot({path:path.join(output,'orchestrated-team.png'),fullPage:false});
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        const run=evidence.runs[0].run;
        assert.equal(run.run.state,'completed');
        assert.deepEqual(run.coordinator_proposals.map(row=>row.state),['accepted','accepted']);
        assert.ok(run.check_receipts.length===2&&run.check_receipts.every(row=>row.executor_id.startsWith('autonomy:')));
        assert.equal(evidence.followup_inputs.length,2);
        assert.deepEqual(starts[0].worker_model,{provider:'ollama',model:'fixture-worker'});
        assert.deepEqual([...new Set(run.attempts.filter(row=>row.kind==='worker').map(row=>JSON.parse(row.grant_json).model.model))],['fixture-worker']);
        assert.deepEqual([...new Set(run.attempts.filter(row=>row.kind==='coordinator').map(row=>JSON.parse(row.grant_json).model.model))],['fixture-native']);
        // One answer turn, which proposed nothing: the two proposals above are the plan and the report.
        assert.deepEqual(run.attempts.filter(row=>row.worker_id.startsWith('orchestrator-answer-')).map(row=>row.state),['completed']);
        assert.equal(evidence.followup_inputs[1].untrusted_messages_to_orchestrator.length,1);
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify(evidence,null,2));
        console.log('Orchestrated team browser evidence:',output);
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
