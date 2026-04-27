#!/usr/bin/env node
const puppeteer = require('puppeteer');

const postId = process.argv[2];
if (!postId) {
  console.error('Usage: fetch_thread_api.js <postId>');
  process.exit(1);
}

(async () => {
  const browser = await puppeteer.launch({
    headless: false,
    executablePath: '/usr/bin/google-chrome',
    userDataDir: '/root/.openclaw/browser/openclaw/user-data',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
    env: { DISPLAY: ':99' },
  });

  try {
    const page = await browser.newPage();
    await page.goto('https://mm.2gis.one/2gis-rd/channels/sd-team-leads', {
      waitUntil: 'networkidle2',
      timeout: 60000,
    });

    const payload = await page.evaluate(async (id) => {
      const url = `/api/v4/posts/${id}/thread?skipFetchThreads=false&collapsedThreads=true&collapsedThreadsExtended=false&direction=down&perPage=200`;
      const res = await fetch(url, { credentials: 'include' });
      const body = await res.text();
      return { status: res.status, body };
    }, postId);

    if (payload.status !== 200) {
      console.error(`HTTP ${payload.status}`);
      console.error(payload.body);
      process.exit(2);
    }

    console.log(payload.body);
  } finally {
    await browser.close();
  }
})();
