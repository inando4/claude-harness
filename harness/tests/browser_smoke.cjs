// Local harness smoke only; this does not constitute feature QA.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH);
(async () => {
  const url = new URL(process.argv[2]);
  if (!['localhost', '127.0.0.1'].includes(url.hostname)) throw new Error('Local URL required');
  const browser = await chromium.launch({ channel: 'chrome', headless: false });
  try {
    const page = await browser.newPage();
    await page.goto(url.href + 'login');
    await page.locator('#email').waitFor();
    await page.screenshot({path: process.argv[3], fullPage: true});
    console.log('PASS: visible Google Chrome opened local login and captured screenshot.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
