const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { isSafeExternalUrl, normalizeServerUrl } = require('./server-url.cjs');
const {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  session,
  shell,
} = require('electron');

if (require('electron-squirrel-startup')) {
  app.quit();
}

app.enableSandbox();
app.setName('Drasil');

const SETTINGS_FILE_NAME = 'desktop-settings.json';
const LAUNCHER_FILE = path.join(__dirname, 'launcher.html');
const LAUNCHER_URL = pathToFileURL(LAUNCHER_FILE).toString();
let activeWindow = null;
let activeServerUrl = null;

function settingsFile() {
  return path.join(app.getPath('userData'), SETTINGS_FILE_NAME);
}

function readSavedServerUrl() {
  try {
    const raw = fs.readFileSync(settingsFile(), 'utf8');
    const settings = JSON.parse(raw);
    return normalizeServerUrl(settings.serverUrl);
  } catch {
    return null;
  }
}

function saveServerUrl(serverUrl) {
  const normalized = normalizeServerUrl(serverUrl);
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.writeFileSync(
    settingsFile(),
    `${JSON.stringify({ serverUrl: normalized }, null, 2)}\n`,
    { encoding: 'utf8', mode: 0o600 },
  );
  return normalized;
}

function removeSavedServerUrl() {
  try {
    fs.unlinkSync(settingsFile());
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
}

function commandLineServerUrl() {
  const argument = process.argv.find((value) => value.startsWith('--server='));
  return argument ? argument.slice('--server='.length) : process.env.DRASIL_DESKTOP_URL;
}

function openExternalUrl(value) {
  if (isSafeExternalUrl(value)) {
    void shell.openExternal(value);
  }
}

function setApplicationMenu() {
  const template = [
    {
      label: 'Drasil',
      submenu: [
        {
          label: 'Open in browser',
          enabled: Boolean(activeServerUrl),
          click: () => activeServerUrl && openExternalUrl(activeServerUrl),
        },
        {
          label: 'Change server…',
          click: () => {
            removeSavedServerUrl();
            activeServerUrl = null;
            if (activeWindow && !activeWindow.isDestroyed()) activeWindow.close();
            createLauncherWindow();
          },
        },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function baseWindowOptions() {
  return {
    width: 1480,
    height: 940,
    minWidth: 720,
    minHeight: 560,
    show: false,
    backgroundColor: '#0b1120',
    autoHideMenuBar: true,
  };
}

function showWhenReady(window) {
  window.once('ready-to-show', () => {
    if (!window.isDestroyed()) window.show();
  });
}

function createLauncherWindow(message = '') {
  const launcher = new BrowserWindow({
    ...baseWindowOptions(),
    width: 620,
    height: 700,
    minWidth: 520,
    minHeight: 620,
    resizable: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
    },
  });

  activeWindow = launcher;
  activeServerUrl = null;
  setApplicationMenu();
  showWhenReady(launcher);
  const suffix = message ? `?message=${encodeURIComponent(message)}` : '';
  void launcher.loadURL(`${LAUNCHER_URL}${suffix}`);
  launcher.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  launcher.webContents.on('will-navigate', (event, destination) => {
    if (destination !== LAUNCHER_URL && !destination.startsWith(`${LAUNCHER_URL}?`)) {
      event.preventDefault();
    }
  });
  launcher.on('closed', () => {
    if (activeWindow === launcher) activeWindow = null;
  });
}

async function handleRemoteLoadFailure(window, serverUrl, errorDescription) {
  if (window.isDestroyed()) return;
  const result = await dialog.showMessageBox(window, {
    type: 'warning',
    title: 'Drasil server unavailable',
    message: `Could not reach ${serverUrl}`,
    detail: `${errorDescription}\n\nIf this is a Tailscale address, confirm Tailscale is connected on this PC.`,
    buttons: ['Retry', 'Change server', 'Close'],
    defaultId: 0,
    cancelId: 2,
  });

  if (result.response === 0 && !window.isDestroyed()) {
    window.webContents.reload();
  } else if (result.response === 1) {
    removeSavedServerUrl();
    activeServerUrl = null;
    if (!window.isDestroyed()) window.close();
    createLauncherWindow();
  }
}

function createRemoteWindow(serverUrl) {
  const normalized = normalizeServerUrl(serverUrl);
  const allowedOrigin = new URL(normalized).origin;
  const remote = new BrowserWindow({
    ...baseWindowOptions(),
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      experimentalFeatures: false,
      webviewTag: false,
    },
  });

  activeWindow = remote;
  activeServerUrl = normalized;
  setApplicationMenu();
  showWhenReady(remote);

  remote.webContents.on('will-attach-webview', (event) => event.preventDefault());
  remote.webContents.on('will-navigate', (event, destination) => {
    try {
      if (new URL(destination).origin === allowedOrigin) return;
    } catch {
      // Invalid destinations are denied below.
    }
    event.preventDefault();
    openExternalUrl(destination);
  });
  remote.webContents.setWindowOpenHandler(({ url }) => {
    try {
      if (new URL(url).origin === allowedOrigin) {
        setImmediate(() => {
          if (!remote.isDestroyed()) void remote.loadURL(url);
        });
      } else {
        openExternalUrl(url);
      }
    } catch {
      // Invalid destinations are denied.
    }
    return { action: 'deny' };
  });
  remote.webContents.on(
    'did-fail-load',
    (event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      if (!isMainFrame || errorCode === -3) return;
      void handleRemoteLoadFailure(remote, normalized, errorDescription);
    },
  );
  remote.on('closed', () => {
    if (activeWindow === remote) activeWindow = null;
  });
  void remote.loadURL(normalized);
}

function launcherSenderIsValid(event) {
  return event.senderFrame?.url === LAUNCHER_URL
    || event.senderFrame?.url.startsWith(`${LAUNCHER_URL}?`);
}

ipcMain.handle('drasil-desktop:connect', (event, value) => {
  if (!launcherSenderIsValid(event)) throw new Error('Untrusted connection request.');
  const normalized = saveServerUrl(value);
  const launcher = BrowserWindow.fromWebContents(event.sender);
  createRemoteWindow(normalized);
  if (launcher && !launcher.isDestroyed()) launcher.close();
  return normalized;
});

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (activeWindow) {
      if (activeWindow.isMinimized()) activeWindow.restore();
      activeWindow.focus();
    }
  });

  app.whenReady().then(() => {
    session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
      callback(false);
    });

    let serverUrl = null;
    const commandLineUrl = commandLineServerUrl();
    if (commandLineUrl) {
      try {
        serverUrl = saveServerUrl(commandLineUrl);
      } catch (error) {
        createLauncherWindow(error instanceof Error ? error.message : String(error));
        return;
      }
    } else {
      serverUrl = readSavedServerUrl();
    }

    if (serverUrl) createRemoteWindow(serverUrl);
    else createLauncherWindow();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      const serverUrl = readSavedServerUrl();
      if (serverUrl) createRemoteWindow(serverUrl);
      else createLauncherWindow();
    }
  });
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
