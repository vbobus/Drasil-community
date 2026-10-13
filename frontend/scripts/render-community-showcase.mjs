import { chromium } from '@playwright/test';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const assets = path.join(root, 'docs/assets');
const output = path.join(root, '.tmp/community-demo');
await mkdir(output, { recursive: true });
const data = async name => `data:image/png;base64,${(await readFile(path.join(assets, name))).toString('base64')}`;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 780 }, deviceScaleFactor: 1 });
  await page.route('**/*', route => route.abort());
  await page.setContent(`<!doctype html><html><head><style>
    *{box-sizing:border-box}html,body{margin:0;width:1400px;height:780px;overflow:hidden;background:#f5f1e9}
    .backdrop{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
    .desktop{position:absolute;left:48px;top:52px;width:1080px;height:675px;border-radius:12px;overflow:hidden;box-shadow:0 22px 48px #111a2c30}
    .desktop img{display:block;width:100%;height:100%}
    .phone{position:absolute;left:1100px;top:128px;width:250px;padding:7px;background:#111a2c;border-radius:26px;box-shadow:0 24px 50px #111a2c38}
    .phone img{display:block;width:100%;border-radius:19px}
  </style></head><body>
    <img class="backdrop" alt="" src="${await data('community-background.png')}">
    <div class="desktop"><img alt="Current Drasil planner with fictional tasks" src="${await data('community-workspace.png')}"></div>
    <div class="phone"><img alt="Current mobile Habits interface with fictional history" src="${await data('community-mobile.png')}"></div>
  </body></html>`);
  await page.screenshot({ path: path.join(assets, 'community-cover.png') });

  const markdown = await readFile(path.join(root, 'README.md'), 'utf8');
  let content = renderToStaticMarkup(createElement(Markdown, { remarkPlugins: [remarkGfm], children: markdown }));
  // The preview is offline and embeds only the explicitly referenced showcase assets.
  for (const name of ['community-cover.png', 'community-demo.gif', 'community-habits.png']) {
    const mime = name.endsWith('.gif') ? 'image/gif' : 'image/png';
    const encoded = `data:${mime};base64,${(await readFile(path.join(assets, name))).toString('base64')}`;
    content = content.replaceAll(`docs/assets/${name}`, encoded);
  }
  const html = `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>
    *{box-sizing:border-box}body{margin:0;background:white;color:#1f2328;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    main{max-width:980px;padding:36px 28px;margin:auto}h1,h2{line-height:1.3;border-bottom:1px solid #d1d9e0;padding-bottom:10px}
    h1{font-size:32px;margin:0 0 18px}h2{font-size:24px;margin:34px 0 16px}p,ul,table{margin:0 0 18px}li{margin:8px 0}
    img{display:block;max-width:100%;height:auto}a{color:#0969da;text-decoration:none}pre{background:#f6f8fa;border-radius:6px;padding:16px;overflow-x:auto;font-size:13px}
    code{font-family:ui-monospace,monospace}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #d1d9e0;padding:8px;text-align:left}
    @media(max-width:600px){main{padding:24px 16px}body{font-size:15px}table{display:block;overflow-x:auto}}
  </style></head><body><main>${content}</main></body></html>`;
  await writeFile(path.join(output, 'readme-preview.html'), html);
  await page.setViewportSize({ width: 1200, height: 1000 });
  await page.setContent(html);
  await page.screenshot({ path: path.join(output, 'readme-desktop-top.png') });
  await page.screenshot({ path: path.join(output, 'readme-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(output, 'readme-mobile-top.png') });
  await page.screenshot({ path: path.join(output, 'readme-mobile.png'), fullPage: true });
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Mobile README overflows.');
  console.log('Rendered Community cover and offline desktop/mobile README previews.');
} finally { await browser.close(); }
