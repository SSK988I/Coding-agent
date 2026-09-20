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
  const command = (value) => evaluate(`(() => {
    const input = document.querySelector('textarea');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, ${JSON.stringify(value)});
    input.dispatchEvent(new Event('input', { bubbles: true }));
  })()`).then(() => evaluate(`document.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))`));

  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  await waitFor("document.querySelector('textarea') && !document.querySelector('textarea').disabled");
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
  await evaluate("window.smoke.ready()");
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
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, output }));
  window.destroy();
  app.quit();
}
main().catch((error) => { console.error(error); app.exit(1); });
