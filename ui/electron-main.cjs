const { app, BrowserWindow } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const http = require('http')
const fs = require('fs')

// Tetapkan identitas aplikasi dan WM_CLASS sebelum inisialisasi jendela
app.setName('CloakDesk')
if (typeof app.setDesktopName === 'function') {
  app.setDesktopName('CloakDesk.desktop')
}
app.commandLine.appendSwitch('class', 'CloakDesk')

// Tentukan direktori root CloakDesk yang valid
let projectRoot = path.resolve(__dirname, '..')
const homeDir = process.env.HOME || require('os').homedir()
if (!fs.existsSync(path.join(projectRoot, 'antidetect')) && fs.existsSync(path.join(homeDir, 'cloakdesk', 'antidetect'))) {
  projectRoot = path.join(homeDir, 'cloakdesk')
}

const pythonBin = path.join(projectRoot, 'venv', 'bin', 'python')
const iconPath = path.join(projectRoot, 'assets', 'cloakdesk-logo.png')

let backendProcess = null
let backendTimer = null
let mainWindow = null

function clearBackendTimer() {
  if (backendTimer) {
    clearInterval(backendTimer)
    backendTimer = null
  }
}

// Cegah instance ganda saling bentrok
const gotTheLock = app.requestSingleInstanceLock()
if (!gotTheLock) {
  app.quit()
  process.exit(0)
}

app.on('second-instance', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) mainWindow.show()
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
  } else if (app.isReady()) {
    ensureBackend(() => {
      createWindow()
    })
  }
})

app.on('activate', () => {
  if (!mainWindow || mainWindow.isDestroyed()) {
    ensureBackend(() => {
      createWindow()
    })
  } else {
    mainWindow.show()
    mainWindow.focus()
  }
})

function checkBackend(callback) {
  let settled = false
  const done = (ok) => {
    if (settled) return
    settled = true
    callback(ok)
  }

  const req = http.get('http://127.0.0.1:53200/health', (res) => {
    res.resume()
    done(res.statusCode === 200)
  })
  req.on('error', () => done(false))
  req.setTimeout(800, () => {
    req.destroy()
    done(false)
  })
}

function ensureBackend(callback) {
  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    clearBackendTimer()
    callback()
  }

  checkBackend((running) => {
    if (running) {
      return finish()
    }
    const py = fs.existsSync(pythonBin) ? pythonBin : '/usr/bin/python3'
    try {
      backendProcess = spawn(py, ['-m', 'antidetect', 'serve'], {
        cwd: projectRoot,
        env: {
          ...process.env,
          PYTHONPATH: `${projectRoot}:${process.env.PYTHONPATH || ''}`,
          DISPLAY: process.env.DISPLAY || ':0',
        },
        stdio: 'ignore',
        detached: true,
      })
      backendProcess.unref()
    } catch (_) {
      // Abaikan kegagalan spawn dan tetap buka jendela
    }

    let attempts = 0
    clearBackendTimer()
    backendTimer = setInterval(() => {
      attempts++
      checkBackend((ok) => {
        if (ok || attempts > 35) {
          finish()
        }
      })
    }, 200)
  })
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) mainWindow.show()
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
    return mainWindow
  }

  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 600,
    title: 'CloakDesk',
    icon: iconPath,
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  })

  mainWindow.loadURL('http://127.0.0.1:53200/#/profiles')

  mainWindow.on('closed', () => {
    clearBackendTimer()
    mainWindow = null
  })

  return mainWindow
}

app.whenReady().then(() => {
  ensureBackend(() => {
    createWindow()
  })
})

app.on('window-all-closed', () => {
  clearBackendTimer()
  if (process.platform !== 'darwin') {
    app.quit()
    process.exit(0)
  }
})
