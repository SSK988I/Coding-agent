// Real renderer, Python workspace readers, PTY and sandboxed browser. No model API calls.
const { app, BrowserWindow, ipcMain } = require("electron");
const { NativeWorkbench } = require("../dist-electron/main/workbench.js");
const { spawn, execFileSync } = require("node:child_process");
const { createServer } = require("node:http");
const readline = require("node:readline");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const sideChatOnly = process.env.CODING_AGENT_SIDE_CHAT_SMOKE_ONLY === "1";
  if (sideChatOnly) app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-workbench-smoke-"));
  app.setPath("userData", path.join(output, "profile"));
  const project = path.join(output, "project");
  await fs.mkdir(project);
  const git = (...args) => execFileSync("git", ["-C", project, ...args], { stdio: ["ignore", "pipe", "pipe"] });
  git("init", "-b", "main"); git("config", "user.name", "Smoke"); git("config", "user.email", "smoke@example.invalid"); git("config", "core.autocrlf", "false");
  await fs.writeFile(path.join(project, "README.md"), "# Original\n\nOriginal text.\n");
  git("add", "."); git("commit", "-m", "baseline");
  await fs.writeFile(path.join(project, "README.md"), "# Workbench\n\nChanged text.\n");
  await fs.writeFile(path.join(project, "sample.py"), "print('file preview')\n");
  await app.whenReady();
  const root = path.resolve(__dirname, "../../..");
  const python = process.env.CODING_AGENT_PYTHON || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const child = spawn(python, ["-m", "coding_agent.desktop"], { cwd: root, windowsHide: true, env: { ...process.env, CODING_AGENT_HOME: path.join(output, "agent"), PYTHONUTF8: "1", PYTHONUNBUFFERED: "1", PYTHONPATH: ["llm", "core", "tui", "app"].map((name) => path.join(root, "packages", name, "src")).join(path.delimiter) }, stdio: ["pipe", "pipe", "pipe"] });
  let next = 0, window, native;
  const pending = new Map();
  const calls = [];
  const errors = [];
  const lines = readline.createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    if (message.type === "event") { window?.webContents.send("agent:event", message); return; }
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id); clearTimeout(request.timeout);
    if (message.error) request.reject(Error(message.error.code + ": " + message.error.message)); else request.resolve(message.result);
  });
  child.stderr.on("data", (data) => errors.push(data.toString()));
  const request = (method, params = {}) => new Promise((resolve, reject) => {
    calls.push({ method, params });
    const id = "wb-" + ++next;
    const timeout = setTimeout(() => { pending.delete(id); reject(Error("Backend timeout: " + method)); }, 15000);
    pending.set(id, { resolve, reject, timeout }); child.stdin.write(JSON.stringify({ v: 1, id, method, params }) + "\n");
  });
  const server = createServer((_request, response) => { response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }); response.end("<!doctype html><title>Workbench local browser</title><h1>Local page</h1><button id='interact' onclick='document.querySelector(\"#result\").textContent=\"Browser interaction works\"'>Click me</button><p id='result'></p>"); });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  window = new BrowserWindow({ show: !sideChatOnly, width: 1440, height: 920, webPreferences: { preload: path.join(__dirname, "workbench.preload.cjs"), contextIsolation: true, sandbox: true, backgroundThrottling: false, offscreen: sideChatOnly } });
  native = new NativeWorkbench(window, request);
  const trusted = (event) => assert.equal(event.sender, window.webContents);
  ipcMain.handle("wb:bootstrap", (event) => { trusted(event); return { defaultWorkspace: project, platform: process.platform }; });
  ipcMain.handle("wb:agent", (event, method, params) => { trusted(event); return request(method, params); });
  ipcMain.handle("wb:native", (event, method, params) => { trusted(event); return native.dispatch(method, params); });
  const evaluate = (source) => window.webContents.executeJavaScript(source).catch((error) => { throw Error("Renderer evaluation failed: " + source, { cause: error }); });
  const waitFor = async (source, contents = window.webContents) => {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) { if (await contents.executeJavaScript(source)) return; await new Promise((resolve) => setTimeout(resolve, 40)); }
    throw Error("Timed out: " + source + "\n" + await contents.executeJavaScript("document.body.innerText"));
  };
  const click = (label) => evaluate(`(() => { const b=[...document.querySelectorAll('button')].find(b=>b.getAttribute('aria-label')===${JSON.stringify(label)} || b.textContent.trim()===${JSON.stringify(label)}); if(!b||b.disabled) throw Error('Missing button '+${JSON.stringify(label)}); b.click(); })()`);
  const screenshot = async (name) => fs.writeFile(path.join(output, name), (await window.capturePage()).toPNG());
  const paint = () => evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  const assertSideConversationDraft = async () => {
    await click("开始侧边对话");
    await waitFor("document.querySelector('textarea[aria-label=侧边对话消息]') && document.querySelector('select[aria-label=选择侧边对话]')?.value && !document.querySelector('select[aria-label=选择侧边对话]').disabled");
    const selection = () => evaluate(`(() => {
      const select = document.querySelector('select[aria-label=选择侧边对话]');
      return { selected: select.value, placeholderDisabled: select.options[0].disabled,
        options: [...select.options].filter(option => option.value).map(option => ({ id: option.value, title: option.textContent })) };
    })()`);
    const first = await selection();
    assert.equal(first.options.length, 1);
    assert.equal(first.options[0].id, first.selected);
    assert(first.options[0].title.startsWith("新对话 · "), "Unused drafts need a distinguishable title");
    assert(first.placeholderDisabled, "The placeholder must not behave as a new-chat action");
    for (let attempt = 0; attempt < 3; attempt++) {
      await click("新建侧边对话");
      await waitFor("!document.querySelector('select[aria-label=选择侧边对话]').disabled");
      assert.deepEqual(await selection(), first, "Repeated new actions must reuse the same unsent draft");
    }
    const opensBeforePlaceholder = calls.filter(call => call.method === "workbench.chat.open").length;
    await evaluate(`(() => {
      const select = document.querySelector('select[aria-label=选择侧边对话]');
      select.value = '';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
    await paint();
    assert.equal(calls.filter(call => call.method === "workbench.chat.open").length, opensBeforePlaceholder);
    assert.deepEqual(await selection(), first);
    await click("关闭工具栏");
    assert(await evaluate("document.querySelector('.workbench').hidden"));
    await click("打开工具栏");
    await waitFor("document.querySelector('select[aria-label=选择侧边对话]')?.checkVisibility()");
    assert.deepEqual(await selection(), first, "Hiding and reopening the workbench must retain the selected draft");
    assert.equal(calls.some(call => call.method === "run.start" || call.method === "workbench.chat.send"), false);
    await paint();
    await screenshot("side-conversation-wide.png");
    return { selected: first.selected, title: first.options[0].title, options: 1, repeatedNewReused: true, placeholderNoop: true };
  };
  const dragWidth = (delta) => evaluate(`(() => {
    const separator=document.querySelector('.workbench-resize');
    separator.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:1000}));
    document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:1000-(${delta})}));
    document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}));
  })()`);
  const assertWidth = async () => {
    await waitFor(`(() => {const w=document.querySelector('.workbench').getBoundingClientRect();const c=document.querySelector('button[aria-label="关闭工具栏"]').getBoundingClientRect();return w.right<=innerWidth+1 && c.right<=innerWidth && c.left>=0})()`);
    assert(await evaluate("document.querySelector('.conversation').getBoundingClientRect().width >= 280"));
  };
  try {
    await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
    await waitFor("document.querySelector('textarea') && !document.querySelector('textarea').disabled");
    await click("打开工具栏");
    await waitFor("document.querySelector('.workbench-launcher button')");
    assert.equal(await evaluate("document.querySelectorAll('.workbench-launcher > div > button').length"), 5);
    if (sideChatOnly) {
      await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(b=>b.textContent.includes('侧边对话')).click()");
      const sideChat = await assertSideConversationDraft();
      console.log(JSON.stringify({ passed: true, output, sideChat, backendErrors: errors }));
      return;
    }
    await screenshot("launcher-wide.png");
    await dragWidth(2000);
    await assertWidth();
    await screenshot("workbench-max-width.png");
    window.setSize(1200, 800);
    await waitFor("innerWidth <= 1200 && innerWidth > 1150");
    await assertWidth();
    await evaluate("document.querySelector('.sidebar-toggle').click()");
    await waitFor("document.querySelector('.workspace-grid.sidebar-closed')");
    await dragWidth(2000);
    await assertWidth();
    await evaluate("document.querySelector('.sidebar-toggle').click()");
    await waitFor("!document.querySelector('.workspace-grid.sidebar-closed')");
    await assertWidth();
    window.setSize(1440, 920);
    await waitFor("innerWidth > 1400");
    await dragWidth(-2000);
    for (let step = 0; step < 3; step++) await evaluate("document.querySelector('.workbench-resize').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}))");
    await assertWidth();
    await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(b=>b.textContent.includes('变更')).click()");
    await waitFor("document.querySelector('.changed-file')?.textContent.includes('README.md')");
    assert(await evaluate("document.querySelector('.diff-add')?.textContent.includes('Workbench')"));
    await screenshot("changes-wide.png");
    await click("在文件中打开 ↗");
    await waitFor("document.querySelector('.file-preview')?.textContent.includes('Changed text.')");
    await screenshot("files-wide.png");
    await click("打开其他工具");
    await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(b=>b.textContent.includes('终端')).click()");
    await waitFor("document.querySelector('.terminal-viewport .xterm-helper-textarea') && !document.body.textContent.includes('正在启动交互终端')");
    await evaluate("document.querySelector('.xterm-helper-textarea').focus()");
    await window.webContents.insertText(process.platform === "win32" ? "Write-Output ('WB_'+'NATIVE_PTY_OK')" : "printf 'WB_%s\\n' NATIVE_PTY_OK");
    window.webContents.sendInputEvent({ type: "keyDown", keyCode: "Return" }); window.webContents.sendInputEvent({ type: "keyUp", keyCode: "Return" });
    await waitFor("document.querySelector('.xterm-rows')?.textContent.includes('WB_NATIVE_PTY_OK')");
    await screenshot("terminal-wide.png");
    await click("打开其他工具");
    await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(b=>b.textContent.includes('浏览器')).click()");
    await waitFor("document.querySelector('button[aria-label=刷新网页]') && !document.querySelector('button[aria-label=刷新网页]').disabled");
    const url = `http://127.0.0.1:${server.address().port}/`;
    await evaluate(`(() => {const input=document.querySelector('.browser-address input');input.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(url)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await waitFor("!document.querySelector('.browser-address button[type=submit]').disabled");
    await evaluate("document.querySelector('.browser-address').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))");
    const navigationDeadline = Date.now() + 15000;
    while (!native.browser || native.browser.webContents.getURL() !== url) {
      if (Date.now() > navigationDeadline) throw Error("Browser navigation timed out");
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
    const page = native.browser.webContents;
    await waitFor("document.querySelector('#interact')", page);
    const displayDeadline = Date.now() + 10000;
    while (!native.browser.getVisible() || native.browser.getBounds().width <= 0 || native.browser.getBounds().height <= 0) {
      if (Date.now() > displayDeadline) throw Error("Browser view is not visible: " + JSON.stringify(native.browser.getBounds()));
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
    assert.equal(await page.executeJavaScript("typeof window.agent"), "undefined");
    assert.equal(await page.executeJavaScript("typeof require"), "undefined");
    window.show(); window.focus(); page.focus();
    await waitFor("document.visibilityState==='visible'", page);
    const position = await page.executeJavaScript("(() => {const r=document.querySelector('#interact').getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()");
    page.debugger.attach("1.3");
    await page.debugger.sendCommand("Input.dispatchMouseEvent", { type: "mouseMoved", ...position });
    await new Promise((resolve) => setTimeout(resolve, 50));
    await page.debugger.sendCommand("Input.dispatchMouseEvent", { type: "mousePressed", button: "left", clickCount: 1, ...position });
    await page.debugger.sendCommand("Input.dispatchMouseEvent", { type: "mouseReleased", button: "left", clickCount: 1, ...position });
    page.debugger.detach();
    await waitFor("document.querySelector('#result')?.textContent==='Browser interaction works'", page);
    await screenshot("browser-wide.png");
    await fs.writeFile(path.join(output, "browser-page.png"), (await page.capturePage()).toPNG());
    await assert.rejects(native.dispatch("browser.navigate", { workspacePath: project, sessionId: native.scope.sessionId, key: native.browserKey, url: "file:///C:/Windows/win.ini" }));
    await native.dispatch("browser.close", { workspacePath: project, sessionId: native.scope.sessionId, key: "retired-browser" });
    assert.equal(native.browser.webContents, page);
    await click("打开其他工具");
    await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(b=>b.textContent.includes('侧边对话')).click()");
    await assertSideConversationDraft();
    assert.equal(await evaluate("document.querySelectorAll('.workbench-tab').length"), 5);
    window.setSize(820, 680);
    await click("打开其他工具");
    await waitFor("document.querySelector('.workbench-launcher') && innerWidth <= 820");
    await screenshot("launcher-narrow.png");
    assert(await evaluate("document.documentElement.scrollWidth <= innerWidth"));
    await evaluate("[...document.querySelectorAll('.workbench-launcher button')].find(b=>b.textContent.includes('变更')).click()");
    await waitFor("document.querySelector('.changes-panel')?.checkVisibility()");
    await screenshot("changes-narrow.png");
    await click("关闭终端 1");
    const closeDeadline = Date.now() + 5000;
    while (native.terminals.size !== 0 && Date.now() < closeDeadline) await new Promise((resolve) => setTimeout(resolve, 40));
    assert.equal(native.terminals.size, 0);
    await click("关闭工具栏");
    assert(await evaluate("document.querySelector('.workbench').hidden"));
    await click("打开工具栏");
    assert.equal(await evaluate("document.querySelectorAll('.workbench-tab').length"), 4);
    const oldScope = await request("workbench.scope");
    const replacement = await request("session.new");
    native.retainScope(replacement.path, replacement.sessionId);
    assert.equal(native.browser, null);
    await assert.rejects(native.dispatch("terminal.open", { ...oldScope, key: "stale" }), /STALE_WORKSPACE/);
    await waitFor("document.querySelector('.workbench-launcher') && !document.querySelector('.workbench-tab')");
    // A delayed scope response must not roll native ownership back after a
    // session.changed event has already installed the replacement scope.
    let resolveScope;
    const isolated = new NativeWorkbench(window, () => new Promise((resolve) => { resolveScope = resolve; }));
    isolated.retainScope(oldScope.workspacePath, oldScope.sessionId);
    const late = isolated.dispatch("terminal.open", { ...oldScope, key: "late" });
    isolated.retainScope(replacement.path, replacement.sessionId);
    resolveScope(oldScope);
    await assert.rejects(late, /STALE_WORKSPACE/);
    await isolated.dispose();
    const responses = [];
    const browserOwner = new NativeWorkbench(window, () => new Promise((resolve) => responses.push(resolve)));
    const newScope = { workspacePath: replacement.path, sessionId: replacement.sessionId };
    const retiredOpen = browserOwner.dispatch("browser.open", { ...newScope, key: "retired" });
    await browserOwner.dispatch("browser.close", { ...newScope, key: "retired" });
    const liveOpen = browserOwner.dispatch("browser.open", { ...newScope, key: "live" });
    responses[1](newScope);
    await liveOpen;
    responses[0](newScope);
    await retiredOpen;
    assert.equal(browserOwner.browserKey, "live");
    await browserOwner.dispatch("browser.close", { ...newScope, key: "retired" });
    assert.equal(browserOwner.browserKey, "live");
    await browserOwner.dispose();
    console.log(JSON.stringify({ passed: true, output, backendErrors: errors }));
  } finally {
    await native.dispose();
    await request("runtime.dispose").catch(() => undefined);
    child.stdin.end();
    await new Promise((resolve) => server.close(resolve));
    window.destroy();
    for (const pendingRequest of pending.values()) clearTimeout(pendingRequest.timeout);
    child.kill();
    app.quit();
  }
}
main().catch((error) => { console.error(error); app.exit(1); });
