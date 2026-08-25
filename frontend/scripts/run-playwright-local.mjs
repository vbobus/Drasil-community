import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(scriptDir, '..');
const repoRoot = path.resolve(frontendRoot, '..');
const port = process.env.PORT ?? '4173';
const baseURL = process.env.BASE_URL ?? `http://127.0.0.1:${port}`;
const viteArgs = [
  './node_modules/vite/bin/vite.js',
  '--host',
  '127.0.0.1',
  '--port',
  port,
  '--strictPort',
];
const playwrightArgs = ['./node_modules/playwright/cli.js', 'test', ...process.argv.slice(2)];
const python = process.env.DRASIL_PYTHON
  ?? path.resolve(repoRoot, 'backend/venv/Scripts/python.exe');

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const selectedPort = typeof address === 'object' && address ? address.port : null;
      server.close((error) => {
        if (error) reject(error);
        else if (selectedPort === null) reject(new Error('Could not allocate a test backend port.'));
        else resolve(selectedPort);
      });
    });
  });
}

async function assertPortAvailable(portToCheck) {
  await new Promise((resolve, reject) => {
    const socket = net.createConnection({
      host: '127.0.0.1',
      port: Number(portToCheck),
    });
    socket.once('connect', () => {
      socket.destroy();
      reject(
        new Error(
          `Port ${portToCheck} is already occupied; refusing to reuse an ambient test server.`,
        ),
      );
    });
    socket.once('error', (error) => {
      socket.destroy();
      if (error?.code === 'ECONNREFUSED') resolve();
      else reject(error);
    });
  });
}

async function waitForServer(url, timeoutMs = 60_000) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return;
      }
    } catch {
      // Keep polling until the local Vite server accepts connections.
    }

    await sleep(500);
  }

  throw new Error(`Timed out waiting for ${url} to respond.`);
}

async function stopChildProcess(child) {
  if (!child.pid) {
    return;
  }

  if (process.platform === 'win32') {
    await new Promise((resolve) => {
      const killer = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
        stdio: 'ignore',
      });

      killer.once('exit', () => resolve());
      killer.once('error', () => resolve());
    });
    return;
  }

  child.kill('SIGTERM');
  await new Promise((resolve) => {
    child.once('exit', () => resolve());
    child.once('error', () => resolve());
  });
}

const backendPort = process.env.DRASIL_E2E_BACKEND_PORT ?? String(await findFreePort());
const backendOrigin = `http://127.0.0.1:${backendPort}`;
await assertPortAvailable(port);
if (process.env.DRASIL_E2E_BACKEND_PORT) {
  await assertPortAvailable(backendPort);
}
const testRoot = await mkdtemp(path.join(os.tmpdir(), 'drasil-playwright-'));
const databasePath = path.join(testRoot, 'tasks.db').replaceAll('\\', '/');
const checkpointPath = path.join(testRoot, 'strategist-checkpoints.db').replaceAll('\\', '/');
const commonEnv = {
  ...process.env,
  PYTHONDONTWRITEBYTECODE: '1',
  PYTHONPATH: path.resolve(repoRoot, 'backend'),
  DRASIL_BACKEND_ORIGIN: backendOrigin,
  DRASIL_DATABASE_URL: `sqlite:///${databasePath}`,
  DRASIL_TEST_ISOLATED_DB: '1',
  DRASIL_TEST_WORKER_ID: `playwright-${process.pid}`,
  STRATEGIST_CHECKPOINT_DB_PATH: checkpointPath,
};

const backendServer = spawn(
  python,
  ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', backendPort],
  {
    cwd: repoRoot,
    stdio: 'inherit',
    env: commonEnv,
  },
);
const viteServer = spawn(process.execPath, viteArgs, {
  cwd: frontendRoot,
  stdio: 'inherit',
  env: commonEnv,
});

const shutdown = async () => {
  await Promise.all([
    stopChildProcess(viteServer),
    stopChildProcess(backendServer),
  ]);
  if (process.env.DRASIL_KEEP_E2E_DB !== '1') {
    // Windows can hold SQLite handles briefly after taskkill reports success.
    // Cleanup is best-effort and must never turn a passing suite into a failure.
    await sleep(250);
    try {
      await rm(testRoot, {
        recursive: true,
        force: true,
        maxRetries: 5,
        retryDelay: 200,
      });
    } catch (error) {
      process.stderr.write(
        `Could not immediately remove isolated Playwright data at ${testRoot}: ${error}\n`,
      );
    }
  } else {
    process.stdout.write(`Kept isolated Playwright database at ${databasePath}\n`);
  }
};

process.once('SIGINT', async () => {
  await shutdown();
  process.exit(130);
});

process.once('SIGTERM', async () => {
  await shutdown();
  process.exit(143);
});

try {
  await Promise.all([
    waitForServer(baseURL),
    waitForServer(`${backendOrigin}/api/v1/health`),
  ]);

  const testExitCode = await new Promise((resolve, reject) => {
    const testProcess = spawn(process.execPath, playwrightArgs, {
      cwd: frontendRoot,
      stdio: 'inherit',
      env: {
        ...commonEnv,
        BASE_URL: baseURL,
      },
    });

    testProcess.once('exit', (code) => resolve(code ?? 1));
    testProcess.once('error', reject);
  });

  await shutdown();
  process.exit(testExitCode);
} catch (error) {
  await shutdown();
  throw error;
}
