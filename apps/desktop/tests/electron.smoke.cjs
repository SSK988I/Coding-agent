// Real Chromium rendering with an offline bridge and an isolated profile.
const { app, BrowserWindow, session } = require("electron");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-smoke-"));
  app.setPath("userData", path.join(output, "profile"));
  await app.whenReady();
  session.defaultSession.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*"] }, (_details, callback) => callback({ cancel: true }));
  const window = new BrowserWindow({
    show: false, width: 1280, height: 900,
    webPreferences: { preload: path.join(__dirname, "smoke.preload.cjs"), contextIsolation: true, backgroundThrottling: false, offscreen: true },
  });
  const errors = [];
  window.webContents.on("console-message", (_event, details, message) => {
    if (typeof details === "object" && details.level === "error") errors.push(details.message);
    else if (typeof details === "number" && details >= 3) errors.push(message);
  });
  const evaluate = (source) => window.webContents.executeJavaScript(source);
  const waitFor = async (source) => {
    const deadline = Date.now() + 6000;
    while (Date.now() < deadline) {
      if (await evaluate(source)) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw Error(`UI assertion timed out: ${source}\n${await evaluate("document.body.innerText")}\n${errors.join("\n")}`);
  };
  const click = (text) => evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find(x => x.textContent.includes(${JSON.stringify(text)}));
    if (!button || button.disabled) throw Error('Button unavailable: ' + ${JSON.stringify(text)});
    button.click();
  })()`);
  const mouseClick = async (selector, allowDisabled = false) => {
    const position = await evaluate(`(() => {
      const button = document.querySelector(${JSON.stringify(selector)});
      if (!button || (button.disabled && !${allowDisabled})) throw Error('Button unavailable: ' + ${JSON.stringify(selector)});
      const r = button.getBoundingClientRect();
      return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
    })()`);
    window.webContents.sendInputEvent({ type: "mouseMove", ...position });
    window.webContents.sendInputEvent({ type: "mouseDown", button: "left", clickCount: 1, ...position });
    window.webContents.sendInputEvent({ type: "mouseUp", button: "left", clickCount: 1, ...position });
  };
  const command = (value) => evaluate(`(() => {
    const input = document.querySelector('textarea');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, ${JSON.stringify(value)});
    input.dispatchEvent(new Event('input', { bubbles: true }));
  })()`).then(() => evaluate(`document.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))`));

  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  await waitFor("document.querySelector('textarea') && !document.querySelector('textarea').disabled");
  await click("请求批准");
  await waitFor("document.querySelectorAll('[role=menuitemradio]').length === 3");
  assert(await evaluate("document.querySelector('[data-mode=ask]').getAttribute('aria-checked') === 'true'"));
  await fs.writeFile(path.join(output, "permissions-wide.png"), (await window.webContents.capturePage()).toPNG());
  await click("完全访问权限");
  await waitFor("document.querySelector('.permission-trigger').textContent.includes('完全访问权限')");
  await click("完全访问权限");
  await waitFor("document.querySelector('[data-mode=full_access]').getAttribute('aria-checked') === 'true'");
  await new Promise((resolve) => setTimeout(resolve, 100));
  await fs.writeFile(path.join(output, "permissions-full.png"), (await window.webContents.capturePage()).toPNG());
  window.setSize(820, 680);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"));
  assert(await evaluate("(() => { const r = document.querySelector('.permission-menu').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })()"));
  await fs.writeFile(path.join(output, "permissions-narrow.png"), (await window.webContents.capturePage()).toPNG());
  await click("请求批准");
  window.setSize(1280, 900);
  await command("/plan");
  await waitFor("document.querySelector('.plan-badge')?.textContent.toLowerCase() === 'plan'");
  assert.equal(await evaluate("!!document.querySelector('[data-testid=plan-mode-menu]')"), false);
  await command("/plan");
  await waitFor("!!document.querySelector('[data-testid=plan-mode-menu]')");
  await click("整理并提交");
  await waitFor("document.body.textContent.includes('正在整理并提交计划')");
  await fs.writeFile(path.join(output, "plan-submitting.png"), (await window.webContents.capturePage()).toPNG());
  await click("停止");
  await waitFor("document.body.textContent.includes('尚未提交计划')");
  await evaluate("window.smoke.drafting()");
  await waitFor("!document.querySelector('textarea').disabled");
  await command("/compact 实现已经选定的方案");
  await waitFor("document.body.textContent.includes('正在面向下一阶段整理当前上下文')");
  await click("停止压缩");
  await waitFor("document.body.textContent.includes('上下文压缩已取消')");
  await evaluate("window.smoke.question()");
  await waitFor("!!document.querySelector('[data-testid=plan-question] .plan-cancel-button')");
  const assertQuestionLayout = async () => {
    assert(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Question card overflow");
    assert(await evaluate(`(() => {
      const card = document.querySelector('[data-testid=plan-question]');
      const button = card.querySelector('.plan-cancel-button');
      const custom = card.querySelector('.plan-custom-answer');
      const style = getComputedStyle(button);
      const bounds = button.getBoundingClientRect();
      const cardBounds = card.getBoundingClientRect();
      return style.borderRadius === '8px' && bounds.height >= 30 &&
        bounds.top >= custom.getBoundingClientRect().bottom + 9 &&
        Math.abs(cardBounds.right - bounds.right - 15) < 2 &&
        bounds.left >= cardBounds.left && bounds.right <= innerWidth;
    })()`), "Cancel button must be styled, spaced and right-aligned");
  };
  await assertQuestionLayout();
  await new Promise((resolve) => setTimeout(resolve, 100));
  await fs.writeFile(path.join(output, "plan-question-wide.png"), (await window.webContents.capturePage()).toPNG());
  window.setSize(820, 680);
  await new Promise((resolve) => setTimeout(resolve, 100));
  await assertQuestionLayout();
  await fs.writeFile(path.join(output, "plan-question-narrow.png"), (await window.webContents.capturePage()).toPNG());
  await click("取消规划");
  await waitFor("!document.querySelector('[data-testid=plan-question]') && !document.querySelector('textarea').disabled");
  window.setSize(1280, 900);
  await evaluate("window.smoke.ready()");
  await waitFor("document.querySelector('[data-testid=plan-decision]')?.textContent.includes('新会话复核')");
  await click("新会话复核");
  await waitFor("document.querySelector('[data-testid=plan-decision]')?.textContent.includes('当前已在复核会话中')");
  assert.equal(await evaluate("[...document.querySelectorAll('button')].some(x => x.textContent.includes('新会话复核'))"), false);
  await click("继续修改");
  await waitFor("!document.querySelector('textarea').disabled");
  await command("/plan");
  await waitFor("!!document.querySelector('[data-testid=plan-decision]')");
  assert.equal(await evaluate("[...document.querySelectorAll('button')].some(x => x.textContent.includes('新会话复核'))"), false);
  assert(await evaluate("document.querySelector('textarea').placeholder === '请先选择执行或继续修改'"));
  await new Promise((resolve) => setTimeout(resolve, 100));
  await fs.writeFile(path.join(output, "handoff-review.png"), (await window.webContents.capturePage()).toPNG());
  await waitFor("document.body.textContent.includes('只读复核当前计划')");
  await click("只读复核当前计划");
  await waitFor("!!document.querySelector('.subagent-task')");
  await evaluate("document.querySelector('.subagent-task').open = true");
  await click("停止子代理");
  await waitFor("document.querySelector('.subagent-task').textContent.includes('已停止')");
  await click("只读复核当前计划");
  await waitFor("document.querySelector('.subagent-task').textContent.includes('调查中')");
  await evaluate("window.smoke.report()");
  await waitFor("document.body.textContent.includes('结论仍需主代理核验')");
  await fs.writeFile(path.join(output, "subagent-wide.png"), (await window.webContents.capturePage()).toPNG());
  window.setSize(820, 680);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Unexpected horizontal overflow");
  await fs.writeFile(path.join(output, "subagent-narrow.png"), (await window.webContents.capturePage()).toPNG());
  await evaluate("window.smoke.sessions()");
  await waitFor("document.querySelectorAll('.session-item').length === 2 && document.body.textContent.includes('会话甲的消息')");
  await evaluate("document.querySelector('textarea').focus()");
  await mouseClick(".session-item:nth-child(2)");
  await waitFor("document.body.textContent.includes('会话乙的消息') && document.querySelector('.session-item.active')?.textContent.includes('会话乙')");
  assert.deepEqual(await evaluate("window.smoke.sessionRequests()"), { open: 1, new: 0 });
  await mouseClick(".sidebar-heading button");
  await waitFor("!document.querySelector('.session-item.active') && !document.body.textContent.includes('会话乙的消息')");
  assert.deepEqual(await evaluate("window.smoke.sessionRequests()"), { open: 1, new: 1 });
  await mouseClick(".session-item:nth-child(1)");
  await waitFor("document.body.textContent.includes('会话甲的消息') && document.querySelector('.session-item.active')?.textContent.includes('会话甲')");
  assert.deepEqual(await evaluate("window.smoke.sessionRequests()"), { open: 2, new: 1 });
  await evaluate("window.smoke.delayNavigation()");
  await mouseClick(".session-item:nth-child(2)");
  await waitFor("document.querySelector('[role=status]')?.textContent.includes('正在切换会话')");
  assert(await evaluate("document.querySelector('textarea').disabled && document.querySelector('.conversation').inert"));
  await mouseClick(".session-item:nth-child(2)", true);
  assert.deepEqual(await evaluate("window.smoke.sessionRequests()"), { open: 3, new: 1 });
  await new Promise((resolve) => setTimeout(resolve, 100));
  await fs.writeFile(path.join(output, "session-switch-pending.png"), (await window.webContents.capturePage()).toPNG());
  await evaluate("window.smoke.finishNavigation()");
  await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  assert(await evaluate("document.body.textContent.includes('会话乙的消息') && !document.body.textContent.includes('会话甲的消息')"));
  await mouseClick(".sidebar-heading button");
  await waitFor("document.querySelector('[role=status]')?.textContent.includes('正在新建会话')");
  await mouseClick(".sidebar-heading button", true);
  assert.deepEqual(await evaluate("window.smoke.sessionRequests()"), { open: 3, new: 2 });
  await evaluate("window.smoke.finishNavigation()");
  await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  assert(await evaluate("!document.querySelector('.session-item.active') && !document.body.textContent.includes('会话乙的消息')"));
  await fs.writeFile(path.join(output, "session-new.png"), (await window.webContents.capturePage()).toPNG());
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, output }));
  window.destroy();
  app.quit();
}
main().catch((error) => { console.error(error); app.exit(1); });
