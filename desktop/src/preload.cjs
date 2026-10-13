const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('drasilDesktop', {
  connect: (serverUrl) => ipcRenderer.invoke('drasil-desktop:connect', serverUrl),
});
