/* Stop keeps an ended writer team's unapplied work in the repository; Discard kept work removes it,
 * Remove folder removes what Lumi left with a branch someone committed to, and Discard all kept work
 * does it for every ended team of the conversation. Sizes are shown; "delete it yourself" never is.
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

test('Ended writer teams keep their unapplied work until it is discarded', {timeout: 240000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-swarm-kept-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--writer'],
        {cwd:output, windowsHide:true, stdio:['ignore','pipe','pipe']});
    let stdout='', stderr='', info, browser, page;
    server.stdout.on('data', chunk=>{stdout+=chunk;});
    server.stderr.on('data', chunk=>{stderr+=chunk;});
    const exited = new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    const git=(...args)=>spawnSync('git',args,{encoding:'utf8',windowsHide:true,
        env:{...process.env,GIT_AUTHOR_NAME:'Fixture',GIT_AUTHOR_EMAIL:'fixture@example.invalid',
            GIT_COMMITTER_NAME:'Fixture',GIT_COMMITTER_EMAIL:'fixture@example.invalid'}});
    const branches=()=>git('-C',info.workspace,'branch','--list','--format=%(refname:short)','lumi/team-*')
        .stdout.split(/\r?\n/).filter(Boolean);
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
        const errors=[], commands=[], replies=[], dialogs=[];
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
        // Confirmations: accepted unless a test step sets the next answer.
        let nextAnswer=true;
        page.on('dialog',dialog=>{ dialogs.push(dialog.message()); const answer=nextAnswer; nextAnswer=true;
            (answer ? dialog.accept() : dialog.dismiss()).catch(()=>{}); });
        const replyTo=async action=>{
            const sent=commands.filter(row=>row.action===action).at(-1);
            assert.ok(sent,`No ${action} was sent`);
            await page.waitForFunction(()=>!app._swarmPending);
            for(let i=0;i<100&&!replies.some(row=>row.request_id===sent.request_id);i++)await new Promise(resolve=>setTimeout(resolve,50));
            const reply=replies.find(row=>row.request_id===sent.request_id);
            assert.ok(reply,`No reply to ${action}`);
            return reply;
        };
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
        const notice=page.locator('[data-swarm="notice"]');
        // Starts the team the form describes and stops it once both writers have submitted.
        const runAndStop=async()=>{
            const before=new Set(await page.evaluate(()=>app._swarmState?.run?.run?.id ? [app._swarmState.run.run.id] : []));
            await page.getByRole('button',{name:'Start scoped team',exact:true}).click();
            await page.waitForFunction(ids=>app._swarmState?.run?.run?.id && !ids.includes(app._swarmState.run.run.id)
                && app._swarmState.run.submissions?.length===2
                && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending,[...before]);
            const writers=await page.evaluate(()=>app._swarmState.run.writer_worktrees.map(row=>({branch:JSON.parse(row.manifest_json).branch,path:row.path})));
            // Stop says what it keeps. A Stop that meets another change to the
            // run (the team's own work) is refused, and the owner refreshes and
            // sends it again.
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
            return writers;
        };
        const dialog=page.getByRole('dialog',{name:'Work together'});
        const first=await runAndStop();
        assert.deepEqual(branches().sort(),first.map(row=>row.branch).sort(),'Stop must keep the unapplied work');
        const kept=page.getByRole('region',{name:'Kept in your repository'});
        await kept.waitFor();
        assert.match(await kept.innerText(),/This team ended with changes nobody applied: 2 writer branches\./);
        for(const {branch} of first)assert.match(await kept.innerText(),new RegExp(`${branch} · committed changes nobody applied · \\d`));
        // How much disk the kept folders take, measured in the background.
        await page.waitForFunction(()=>/Its folders take \d/.test(document.querySelector('[data-swarm="kept-summary"]').textContent));
        const inbox=page.locator('[data-swarm="inbox"]');
        assert.match(await inbox.innerText(),/Changes nobody applied are kept in your repository/);

        // Someone commits on the first writer's branch in its worktree: Discard never deletes that branch.
        assert.equal(git('-C',first[0].path,'commit','--allow-empty','-m','Salvaged by the person').status,0);
        const salvaged=git('-C',info.workspace,'rev-parse',first[0].branch).stdout.trim();
        // Discard needs its confirmation; by keyboard: Space checks it, Tab reaches the button, Enter.
        const discard=page.getByRole('button',{name:'Discard kept work',exact:true});
        assert.equal(await discard.isDisabled(),true);
        const confirm=page.getByLabel('I no longer need what this team kept');
        await confirm.focus();
        await page.keyboard.press('Space');
        await page.waitForFunction(()=>!document.querySelector('[data-swarm="discard"]').disabled);
        await page.keyboard.press('Tab');
        assert.equal(await discard.evaluate(node=>document.activeElement===node),true);
        await page.setViewportSize({width:390,height:844});
        await discard.scrollIntoViewIfNeeded();
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'kept-work-compact.png')});
        assert.equal(commands.filter(row=>row.action==='discard_kept_work').length,0);
        await page.keyboard.press('Enter');
        const discarded=await replyTo('discard_kept_work');
        const sent=commands.filter(row=>row.action==='discard_kept_work');
        assert.equal(sent.length,1);
        assert.ok(Number.isInteger(sent[0].expected_revision) && sent[0].run_id);
        assert.equal(discarded.message,`Discarded 1 worktree and 1 branch. Kept ${first[0].branch}: it has commits the team didn't make.`,
            JSON.stringify(discarded.error||discarded.message));
        assert.deepEqual(branches(),[first[0].branch]);
        assert.equal(await confirm.isChecked(),false);

        // What Lumi left with that branch: listed with its folder, which only Remove folder takes.
        const left=page.getByRole('list',{name:'Branches kept for you'});
        await left.waitFor();
        assert.match(await left.innerText(),new RegExp(`Kept for you: ${first[0].branch} \\(it has commits the team didn’t make\\), with its worktree `));
        assert.doesNotMatch(await dialog.innerText(),/delete it yourself/i);
        const remove=left.getByRole('button',{name:`Remove the folder kept with ${first[0].branch}`,exact:true});
        await remove.scrollIntoViewIfNeeded();
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'kept-left-compact.png')});
        nextAnswer=false;  // the confirmation says what goes and what stays; cancelled, nothing is sent
        await remove.click();
        assert.match(dialogs.at(-1),/The branch lumi\/team-\S+ and its commits stay in your repository\. Changes not committed in that folder are lost\./);
        assert.equal(commands.filter(row=>row.action==='remove_left_worktree').length,0);
        await remove.click();
        const removed=await replyTo('remove_left_worktree');
        assert.equal(removed.message,`Removed ${first[0].path}. The branch ${first[0].branch} and its commits stay in your repository.`);
        assert.equal(fs.existsSync(first[0].path),false);
        assert.equal(git('-C',info.workspace,'rev-parse',first[0].branch).stdout.trim(),salvaged);
        await page.waitForFunction(()=>/Its folder was removed; the branch and its commits stay/.test(document.querySelector('[data-swarm="kept-left"]').textContent));
        assert.equal(await remove.isHidden(),true);
        await page.setViewportSize({width:1180,height:900});

        // A second team; then Discard all kept work, from Saved teams, takes what every ended team kept.
        await page.getByRole('button',{name:'New team',exact:true}).click();
        const second=await runAndStop();
        assert.deepEqual(branches().sort(),[first[0].branch,...second.map(row=>row.branch)].sort());
        await page.getByText('Saved teams in this conversation',{exact:true}).click();
        const everywhere=page.locator('[data-swarm="kept-everywhere"]');
        await page.waitForFunction(()=>/1 ended team in this conversation keeps 2 worktrees in Lumi’s folder\. They take \d/.test(
            document.querySelector('[data-swarm="kept-everywhere"]').textContent));
        const all=page.getByRole('button',{name:'Discard all kept work',exact:true});
        await all.click();
        assert.match(dialogs.at(-1),/Discard what every ended team in this conversation kept\?/);
        const discardedAll=await replyTo('discard_all_kept_work');
        assert.equal(discardedAll.message,'Discarded 2 worktrees and 2 branches.',JSON.stringify(discardedAll.error||discardedAll.message));
        assert.deepEqual(branches(),[first[0].branch],'a branch someone committed to stays');
        await page.waitForFunction(()=>document.querySelector('[data-swarm="kept-everywhere"]').hidden
            && document.querySelector('[data-swarm="discard-all"]').hidden);
        assert.equal(await everywhere.isHidden(),true);
        await page.getByText('Recent activity',{exact:true}).click();
        await page.getByText('Kept work was discarded',{exact:true}).waitFor();
        assert.doesNotMatch(await dialog.innerText(),/delete it yourself/i);
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
