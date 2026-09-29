/* Stop keeps an ended writer team's unapplied work in the repository; Discard kept work removes it.
 * Actual source UI/WS/SQLite/Git; trusted scripted thread writers. Not live-model evidence.
 * node tests/swarm_kept_work.browser.cjs [absolute-path-to-playwright-module]
 */
// The app page needs a one-time launch code (lumi/gui/local_access.py); the
// fixture server mints one per page load.
const fixtureLaunch=async info=>(await (await fetch(info.url+'/__fixture__/launch')).json()).url;
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn, spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

test('An ended writer team keeps its unapplied work until Discard kept work', {timeout: 120000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-swarm-kept-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--writer'],
        {cwd:output, windowsHide:true, stdio:['ignore','pipe','pipe']});
    let stdout='', stderr='', info, browser, page;
    server.stdout.on('data', chunk=>{stdout+=chunk;});
    server.stderr.on('data', chunk=>{stderr+=chunk;});
    const exited = new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    const branches=()=>spawnSync('git',['-C',info.workspace,'branch','--list','--format=%(refname:short)','lumi/team-*'],
        {encoding:'utf8',windowsHide:true}).stdout.split(/\r?\n/).filter(Boolean);
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
        const errors=[], commands=[], replies=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>{
            socket.on('framesent',frame=>{
                const data=JSON.parse(frame.payload);
                if(data.command==='swarm')commands.push(data);
            });
            socket.on('framereceived',frame=>{
                try { const data=JSON.parse(frame.payload); if(data.event==='swarm_state')replies.push(data); } catch(_) { /* not JSON */ }
            });
        });
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>window.app?.currentSessionId===session,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Team objective').fill('Update the independent backend and frontend fixture files');
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
        await page.getByRole('button',{name:'Start scoped team',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===2
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending);
        const writers=await page.evaluate(()=>app._swarmState.run.writer_worktrees.map(row=>JSON.parse(row.manifest_json).branch));
        assert.deepEqual(branches().sort(),[...writers].sort());

        // Stop says what it keeps. The runner's lease renewal advances the run's
        // revision every few seconds; a Stop that meets one is refused and the
        // owner refreshes and sends it again.
        const notice=page.locator('[data-swarm="notice"]');
        for(let attempt=0;;attempt++) {
            await page.getByRole('button',{name:'Stop team',exact:true}).click();
            await page.waitForFunction(()=>/Stop requested|revision changed/i.test(document.querySelector('[data-swarm="notice"]').textContent));
            if(!/revision changed/i.test(await notice.textContent()))break;
            assert.ok(attempt<3,'Stop kept meeting a revision change');
            await page.getByRole('button',{name:'Refresh team',exact:true}).click();
            await page.waitForFunction(()=>!app._swarmPending);
        }
        assert.match(await notice.textContent(),/Its unapplied work stays in your repository until you discard it: 2 writer branches \(lumi\/team-/);
        await page.waitForFunction(()=>['cancelled','failed'].includes(app._swarmState?.run?.run?.state)
            && app._swarmState.run.kept_work?.ended && !app._swarmPending);
        const kept=page.getByRole('region',{name:'Kept in your repository'});
        await kept.waitFor();
        assert.match(await kept.innerText(),/This team ended with changes nobody applied: 2 writer branches\./);
        for(const branch of writers)assert.match(await kept.innerText(),new RegExp(`${branch} · committed changes nobody applied`));
        assert.deepEqual(branches().sort(),[...writers].sort(),'Stop must keep the unapplied work');
        const inbox=page.locator('[data-swarm="inbox"]');
        assert.match(await inbox.innerText(),/Changes nobody applied are kept in your repository/);

        // Discard needs its confirmation; by keyboard: Space checks it, Tab reaches the button, Enter.
        const discard=page.getByRole('button',{name:'Discard kept work',exact:true});
        assert.equal(await discard.isDisabled(),true);
        const confirm=page.getByLabel('I no longer need these unapplied changes');
        await confirm.focus();
        await page.keyboard.press('Space');
        await page.waitForFunction(()=>!document.querySelector('[data-swarm="discard"]').disabled);
        await page.keyboard.press('Tab');
        assert.equal(await discard.evaluate(node=>document.activeElement===node),true);
        await page.setViewportSize({width:390,height:844});
        await discard.scrollIntoViewIfNeeded();
        const dialog=page.getByRole('dialog',{name:'Work together'});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'kept-work-compact.png')});
        assert.equal(commands.filter(row=>row.action==='discard_kept_work').length,0);
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>(app._swarmState.run.kept_work?.items||[]).length===0 && !app._swarmPending);
        const sent=commands.filter(row=>row.action==='discard_kept_work');
        assert.equal(sent.length,1);
        assert.ok(Number.isInteger(sent[0].expected_revision) && sent[0].run_id);
        const reply=replies.find(row=>row.request_id===sent[0].request_id);
        assert.equal(reply.message,'Discarded 2 worktrees and 2 branches.',JSON.stringify(reply.error||reply.message));
        assert.deepEqual(branches(),[]);
        await page.waitForFunction(()=>(app._swarmState.run.kept_work?.items||[]).length===0);
        assert.equal(await kept.isHidden(),true);
        assert.equal(await confirm.isChecked(),false);
        assert.doesNotMatch(await inbox.innerText(),/Changes nobody applied/);
        await page.getByText('Recent activity',{exact:true}).click();
        await page.getByText('Kept work was discarded',{exact:true}).waitFor();
        assert.deepEqual(errors,[]);
        console.log('Kept-work browser evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState).catch(()=>({})),null,2));
        }
        console.error('Kept-work browser failure evidence:',output);
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
