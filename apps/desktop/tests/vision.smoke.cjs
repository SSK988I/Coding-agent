// Real Chromium: file/paste/drop -> FileReader -> CSP image rendering -> RPC/history.
const { app, BrowserWindow, nativeImage, session } = require("electron");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");
const png = nativeImage.createFromBitmap(Buffer.from([40, 80, 120, 255]), { width: 1, height: 1 }).toPNG().toString("base64");

async function main() {
  app.disableHardwareAcceleration();
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "coding-agent-vision-"));
  app.setPath("userData", path.join(output, "profile"));
  await app.whenReady();
  session.defaultSession.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*"] }, (_details, callback) => callback({ cancel: true }));
  const window = new BrowserWindow({ show: false, width: 1280, height: 900,
    webPreferences: { preload: path.join(__dirname, "vision.preload.cjs"), contextIsolation: true, backgroundThrottling: false, offscreen: true } });
  const errors = [];
  window.webContents.on("console-message", (_event, details) => {
    if (typeof details === "object" && details.level === "error") errors.push(details.message);
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
  const captureMixedFiles = async (name) => {
    await waitFor(`(() => {
      const file = document.querySelector('.pending-file');
      const image = document.querySelector('.pending-image img');
      if (!file?.textContent.includes('说明.md') || image?.naturalWidth !== 1) return false;
      return [file, image].every(node => {
        const bounds = node.getBoundingClientRect();
        return bounds.width > 0 && bounds.height > 0 && bounds.top >= 0 && bounds.bottom <= innerHeight;
      });
    })()`);
    // DOM/FileReader completion precedes Chromium's compositor frame. Wait for
    // layout and a following paint opportunity before taking a review artifact.
    await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
    await fs.writeFile(path.join(output, name), (await window.webContents.capturePage()).toPNG());
  };
  const attach = (kind) => evaluate(`(() => {
    const bytes = Uint8Array.from(atob(${JSON.stringify(png)}), value => value.charCodeAt(0));
    const file = new File([bytes], 'screen.png', { type: 'image/png' });
    const transfer = new DataTransfer(); transfer.items.add(file);
    if (${JSON.stringify(kind)} === 'select') {
      const input = document.querySelector('input[type=file]');
      input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (${JSON.stringify(kind)} === 'paste') {
      document.querySelector('textarea').dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: transfer }));
    } else {
      document.querySelector('textarea').dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer }));
    }
  })()`);
  const fileText = "# 附件说明\n来自选择、粘贴或拖入的文件。";
  const attachFile = (kind) => evaluate(`(() => {
    const file = new File([${JSON.stringify(fileText)}], '说明.md', { type: 'text/markdown' });
    const transfer = new DataTransfer(); transfer.items.add(file);
    if (${JSON.stringify(kind)} === 'select') {
      const input = document.querySelector('input[type=file]');
      input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (${JSON.stringify(kind)} === 'paste') {
      document.querySelector('textarea').dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: transfer }));
    } else {
      document.querySelector('textarea').dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer }));
    }
  })()`);

  await window.loadFile(path.join(__dirname, "../dist-renderer/index.html"));
  await waitFor("document.querySelector('.attachment-trigger') && !document.querySelector('.attachment-trigger').disabled");
  for (const kind of ["select", "paste", "drop"]) {
    await attach(kind);
    await waitFor("document.querySelector('.pending-image img')?.naturalWidth === 1 && !document.querySelector('.send-button').disabled");
    assert(await evaluate("!document.querySelector('.pending-images input[type=checkbox]')"));
    assert(await evaluate("!document.querySelector('.pending-images').textContent.includes('图片将上传')"));
    assert(await evaluate("!document.querySelector('.pending-images').textContent.includes('联网核实角色/地点')"));
    assert(await evaluate("!document.querySelector('.pending-images').textContent.includes('这是全局联网设置')"));
    if (kind === "select") {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await fs.writeFile(path.join(output, "images-wide.png"), (await window.webContents.capturePage()).toPNG());
      window.setSize(820, 680);
      await new Promise((resolve) => setTimeout(resolve, 100));
      assert(await evaluate("document.documentElement.scrollWidth <= innerWidth"));
      await fs.writeFile(path.join(output, "images-narrow.png"), (await window.webContents.capturePage()).toPNG());
      window.setSize(1280, 900);
    }
    await evaluate("document.querySelector('.pending-image button').click()");
    await waitFor("!document.querySelector('.pending-image')");
  }
  await attach("select");
  await waitFor("!document.querySelector('.send-button').disabled");
  await evaluate("document.querySelector('.send-button').click()");
  await waitFor("document.querySelector('.message-images img')?.naturalWidth === 1 && !document.querySelector('.pending-image')");
  const calls = await evaluate("window.visionSmoke.calls()");
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { text: "", images: [{ data: png, mime_type: "image/png" }] });
  await evaluate("window.visionSmoke.fail()");
  await waitFor("document.querySelector('.pending-image img')?.naturalWidth === 1 && !document.querySelector('.send-button').disabled");
  assert.equal(await evaluate("document.querySelectorAll('.message.user').length"), 1);
  await evaluate("document.querySelector('.pending-image button').click()");
  await evaluate("window.visionSmoke.history()");
  await waitFor("document.querySelector('.message-images img')?.naturalWidth === 1");
  await evaluate("window.visionSmoke.textModel()");
  await waitFor("document.querySelector('.attachment-hint')?.textContent.includes('当前模型不支持图片') && !document.querySelector('.attachment-trigger').disabled");
  assert(await evaluate("document.querySelector('.attachment-trigger').textContent.includes('添加文件')"));
  await attach("paste");
  await waitFor("document.querySelector('[role=alert]')?.textContent.includes('当前模型不支持图片输入')");
  assert.equal(await evaluate("document.querySelectorAll('.pending-image').length"), 0);
  await evaluate("document.querySelector('[role=alert] button').click()");
  for (const kind of ["select", "paste", "drop"]) {
    await attachFile(kind);
    await waitFor("document.querySelector('.pending-file')?.textContent.includes('说明.md') && !document.querySelector('.send-button').disabled");
    assert.equal(await evaluate("document.querySelectorAll('.pending-file').length"), 1);
    await evaluate("document.querySelector('.pending-file button').click()");
    await waitFor("!document.querySelector('.pending-file')");
  }
  await evaluate("window.visionSmoke.imageModel()");
  await waitFor("!document.querySelector('.attachment-hint')?.textContent.includes('当前模型不支持图片')");
  await attachFile("select");
  await waitFor("document.querySelector('.pending-file') && !document.querySelector('.send-button').disabled");
  await attach("paste");
  await waitFor("document.querySelector('.pending-image img')?.naturalWidth === 1 && !document.querySelector('.send-button').disabled");
  assert.equal(await evaluate("document.querySelectorAll('.pending-file').length"), 1);
  await captureMixedFiles("mixed-files-wide.png");
  window.setSize(820, 680);
  await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  assert(await evaluate("document.documentElement.scrollWidth <= innerWidth"));
  await captureMixedFiles("mixed-files-narrow.png");
  await evaluate("document.querySelector('.send-button').click()");
  await waitFor("window.visionSmoke.calls().length === 2 && !document.querySelector('.pending-file')");
  const fileCall = (await evaluate("window.visionSmoke.calls()"))[1];
  assert.deepEqual(fileCall.images, [{ data: png, mime_type: "image/png" }]);
  assert.deepEqual(fileCall.files, [{ name: "说明.md", mime_type: "text/markdown", data: Buffer.from(fileText).toString("base64") }]);
  await evaluate("window.visionSmoke.fail()");
  await waitFor("document.querySelector('.pending-file')?.textContent.includes('说明.md') && document.querySelector('.pending-image img')?.naturalWidth === 1");
  await evaluate("document.querySelector('.pending-file button').click(); document.querySelector('.pending-image button').click(); window.visionSmoke.history()");
  await waitFor("document.querySelector('.message-files')?.textContent.includes('说明.md')");
  assert.equal(await evaluate("document.querySelector('.message-files').closest('article').textContent.includes('fixture body')"), false);
  assert.equal(errors.length, 0, errors.join("\n"));
  console.log(`PASS: image/file select/paste/drop, text-model file input, mixed RPC, failure retry, restored history, model gate, CSP and narrow layout. Screenshots: ${output}`);
  window.destroy();
  app.quit();
}

main().catch((error) => { console.error(error); app.exit(1); });
