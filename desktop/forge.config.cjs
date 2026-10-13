module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'Drasil',
    ignore: [/^\/test($|\/)/],
  },
  rebuildConfig: {},
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      config: {
        name: 'Drasil',
        setupExe: 'DrasilSetup.exe',
        noMsi: true,
      },
    },
  ],
};
