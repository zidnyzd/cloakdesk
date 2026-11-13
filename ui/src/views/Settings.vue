<script setup>
import { computed, inject, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { api, UI_VERSION } from '../api'
import { useOnRefresh, useRefreshSignal } from '../refresh'

const THEME_STORAGE_KEY = 'cloakdesk.theme'
const VALID_THEMES = ['auto', 'dark', 'light']

function readStoredTheme() {
  const raw = localStorage.getItem(THEME_STORAGE_KEY)
  return VALID_THEMES.includes(raw) ? raw : 'auto'
}

const injectedTheme = inject('cloakdeskTheme', null)
const mediaQuery =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : null

const localThemeMode = ref(readStoredTheme())
const systemDark = ref(mediaQuery ? mediaQuery.matches : false)

const health = ref(null)
const healthError = ref('')
const profileTotal = ref(null)
const profilesError = ref('')
const engine = ref(null)
const engineError = ref('')
const kernels = ref([])
const kernelData = ref(null)
const kernelsError = ref('')
const kernelBusyId = ref('')
const installDialogVisible = ref(false)
const installBusy = ref(false)
const installErrors = reactive({ source_path: '' })
const syncInfo = ref(null)
const syncError = ref('')
const conflicts = ref([])
const editingSync = ref(false)
const configureBusy = ref(false)
const triggerBusy = ref(false)
const resolvingConflictId = ref('')
const loading = ref(true)
const refreshTick = useRefreshSignal()

const syncForm = reactive({
  server_url: '',
  enrollment_token: '',
  device_name: '',
  passphrase: '',
})

const installForm = reactive({
  source_type: 'local_archive',
  source_path: '',
  kernel_name: '',
  set_default: false,
})

const apiBase = computed(() => `${window.location.origin}/api/v2/`)
const kernelColumns = computed(() => [
  { key: 'name', label: 'Nama', width: 180 },
  { key: 'version', label: 'Versi', width: 190 },
  { key: 'size', label: 'Ukuran', width: 90 },
  { key: 'profiles', label: 'Profil', width: 90, align: 'right' },
  { key: 'status', label: 'Status', width: 160 },
  { key: 'actions', label: 'Aksi', width: 160, align: 'right' },
])
const enginePath = computed(() => pathDirname(kernelData.value?.engine_path || engine.value?.path || ''))
const extraKernelDir = computed(() => '~/.local/share/antidetect/kernels')
const maxKernelMajor = computed(() => {
  const majors = kernels.value.map((item) => Number(item.major_version)).filter((value) => value > 0)
  return majors.length ? Math.max(...majors) : null
})
const kernelSummary = computed(() => {
  if (!kernels.value.length) return ''
  return maxKernelMajor.value ? `Maks v${maxKernelMajor.value}` : ''
})
const installPlaceholder = computed(() =>
  installForm.source_type === 'local_archive'
    ? '~/Downloads/ungoogled-chromium-152-linux.tar.xz'
    : '~/Downloads/ungoogled-chromium-152-linux'
)
const installSourceLabel = computed(() =>
  installForm.source_type === 'local_archive' ? 'Path Arsip' : 'Path Folder'
)
const isSyncConfigured = computed(() => Boolean(syncInfo.value && syncInfo.value.configured))
const conflictCount = computed(() => Number(syncInfo.value?.conflict_count || conflicts.value.length || 0))

async function loadHealth() {
  try {
    health.value = await api.health()
    healthError.value = ''
  } catch (error) {
    health.value = null
    healthError.value = error.message
  }
}

async function loadProfileTotal() {
  try {
    const result = await api.profiles({ page: 1, limit: 1 })
    profileTotal.value = result.total ?? 0
    profilesError.value = ''
  } catch (error) {
    profileTotal.value = null
    profilesError.value = error.message
  }
}

async function loadEngine() {
  try {
    engine.value = await api.engineInfo()
    engineError.value = ''
  } catch (error) {
    engine.value = null
    engineError.value = error.message
  }
}

async function loadKernels() {
  try {
    const result = await api.kernelList()
    kernelData.value = result && typeof result === 'object' ? result : null
    kernels.value = Array.isArray(result?.data) ? result.data : []
    kernelsError.value = ''
  } catch (error) {
    kernelData.value = null
    kernels.value = Array.isArray(engine.value?.kernels) ? engine.value.kernels : []
    kernelsError.value = error.message
  }
}

function pathDirname(value) {
  const text = String(value || '')
  const cut = text.lastIndexOf('/')
  return cut > 0 ? text.slice(0, cut) : text
}

function kernelMajorLabel(kernel) {
  const major = Number(kernel.major_version)
  return major > 0 ? `v${major}` : ''
}

function isKernelDefault(kernel) {
  return Boolean(kernel.is_default ?? kernel.active)
}

function kernelDeleteBlockReason(kernel) {
  if (kernel.is_builtin) return 'Kernel bawaan tidak dapat dihapus'
  const count = Number(kernel.profile_count || 0)
  if (count > 0) return `Masih digunakan oleh ${count} profil aktif`
  return ''
}

function openKernelInstallDialog() {
  installForm.source_type = 'local_archive'
  installForm.source_path = ''
  installForm.kernel_name = ''
  installForm.set_default = false
  installErrors.source_path = ''
  installDialogVisible.value = true
}

async function makeKernelDefault(kernel) {
  kernelBusyId.value = kernel.id
  try {
    await api.kernelSetDefault(kernel.id)
    ElMessage.success(`Kernel ${kernel.name || kernel.id} dijadikan default`)
    await loadKernels()
    refreshTick.value += 1
  } catch (error) {
    ElMessage.error(`Gagal mengubah kernel default: ${error.message}`)
  } finally {
    kernelBusyId.value = ''
  }
}

async function deleteKernel(kernel) {
  const reason = kernelDeleteBlockReason(kernel)
  if (reason) {
    ElMessage.warning(reason)
    return
  }
  try {
    await ElMessageBox.confirm(
      `Hapus kernel ${kernel.name || kernel.id} beserta seluruh berkasnya dari disk? Tindakan ini tidak dapat dibatalkan.`,
      'Hapus Kernel',
      {
        confirmButtonText: 'Hapus',
        cancelButtonText: 'Batal',
        type: 'warning',
        confirmButtonClass: 'danger-action',
      }
    )
  } catch {
    return
  }
  kernelBusyId.value = kernel.id
  try {
    await api.kernelDelete(kernel.id)
    ElMessage.success(`Kernel ${kernel.name || kernel.id} dihapus`)
    await loadKernels()
    refreshTick.value += 1
  } catch (error) {
    if (error.code === 1105) {
      ElMessage.error(`Kernel masih dipakai profil aktif. Ganti kernel profil tersebut dulu. ${error.message}`)
    } else {
      ElMessage.error(`Gagal menghapus kernel: ${error.message}`)
    }
  } finally {
    kernelBusyId.value = ''
  }
}

async function submitKernelInstall() {
  const sourcePath = installForm.source_path.trim()
  installErrors.source_path = ''
  if (!sourcePath) {
    installErrors.source_path = installSourceLabel.value + ' wajib diisi'
    return
  }
  if (!sourcePath.startsWith('/')) {
    installErrors.source_path = 'Gunakan path absolut, diawali tanda /'
    return
  }
  installBusy.value = true
  try {
    const installed = await api.kernelInstall({
      source_type: installForm.source_type,
      source_path: sourcePath,
      kernel_name: installForm.kernel_name.trim() || undefined,
      set_default: Boolean(installForm.set_default),
    })
    const label = installed?.name || installed?.id || 'Kernel baru'
    ElMessage.success(`${label} berhasil dipasang`)
    installDialogVisible.value = false
    await Promise.all([loadKernels(), loadEngine()])
    refreshTick.value += 1
  } catch (error) {
    ElMessage.error(`Instalasi kernel gagal: ${error.message}`)
  } finally {
    installBusy.value = false
  }
}

async function loadSync() {
  try {
    const status = await api.syncStatus()
    syncInfo.value = status
    syncError.value = ''
    if (!syncForm.device_name && status?.device_name) {
      syncForm.device_name = status.device_name
    }
    if (!syncForm.server_url && status?.server_url) {
      syncForm.server_url = status.server_url
    }
    if (Number(status?.conflict_count || 0) > 0) {
      conflicts.value = (await api.syncConflicts()) || []
    } else {
      conflicts.value = []
    }
  } catch (error) {
    syncInfo.value = null
    syncError.value = error.message
    conflicts.value = []
  }
}

async function submitSyncConfigure() {
  const serverUrl = syncForm.server_url.trim()
  const enrollmentToken = syncForm.enrollment_token.trim()
  const deviceName = syncForm.device_name.trim()
  const passphrase = syncForm.passphrase

  if (!serverUrl) {
    ElMessage.warning('URL Server wajib diisi')
    return
  }
  if (!enrollmentToken) {
    ElMessage.warning('Token Pendaftaran wajib diisi')
    return
  }
  if (!passphrase) {
    ElMessage.warning('Frasa Sandi Enkripsi wajib diisi')
    return
  }

  configureBusy.value = true
  try {
    await api.syncConfigure({
      server_url: serverUrl,
      enrollment_token: enrollmentToken,
      device_name: deviceName || undefined,
      passphrase,
    })
    ElMessage.success('Server sinkronisasi berhasil dihubungkan')
    editingSync.value = false
    syncForm.enrollment_token = ''
    syncForm.passphrase = ''
    await loadSync()
    await loadProfileTotal()
    refreshTick.value += 1
  } catch (error) {
    ElMessage.error(`Gagal menghubungkan server: ${error.message}`)
  } finally {
    configureBusy.value = false
  }
}

async function runSyncNow() {
  triggerBusy.value = true
  try {
    const result = await api.syncTrigger()
    ElMessage.success(
      `Sinkronisasi selesai (${result?.pushed ?? 0} dikirim, ${result?.pulled ?? 0} diterima)`
    )
    await loadSync()
    await loadProfileTotal()
    refreshTick.value += 1
  } catch (error) {
    ElMessage.error(`Sinkronisasi gagal: ${error.message}`)
    await loadSync()
  } finally {
    triggerBusy.value = false
  }
}

function openReconfigure() {
  if (syncInfo.value) {
    syncForm.server_url = syncInfo.value.server_url || syncForm.server_url
    syncForm.device_name = syncInfo.value.device_name || syncForm.device_name
  }
  editingSync.value = true
}

function cancelReconfigure() {
  editingSync.value = false
}

async function resolveConflict(conflictId, resolution) {
  resolvingConflictId.value = conflictId
  try {
    await api.syncConflictResolve({
      conflict_id: conflictId,
      resolution,
    })
    ElMessage.success(
      resolution === 'keep_local'
        ? 'Konflik diselesaikan dengan versi lokal'
        : 'Konflik diselesaikan dengan versi server'
    )
    await loadSync()
    refreshTick.value += 1
  } catch (error) {
    ElMessage.error(`Gagal menyelesaikan konflik: ${error.message}`)
  } finally {
    resolvingConflictId.value = ''
  }
}

function applyLocalTheme(mode) {
  const next = VALID_THEMES.includes(mode) ? mode : 'auto'
  localThemeMode.value = next
  localStorage.setItem(THEME_STORAGE_KEY, next)
  const dark = next === 'dark' || (next === 'auto' && (mediaQuery ? mediaQuery.matches : false))
  document.documentElement.classList.add('theme-transitioning')
  document.documentElement.classList.toggle('dark', dark)
  void document.documentElement.offsetHeight
  requestAnimationFrame(() => {
    document.documentElement.classList.remove('theme-transitioning')
  })
}

function updateThemeMode(mode) {
  const next = VALID_THEMES.includes(mode) ? mode : 'auto'
  applyLocalTheme(next)
  if (injectedTheme?.setThemeMode) {
    injectedTheme.setThemeMode(next)
  } else if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('cloakdesk:theme-change', { detail: next }))
  }
}

const themeMode = computed({
  get() {
    return injectedTheme?.themeMode ? injectedTheme.themeMode.value : localThemeMode.value
  },
  set(val) {
    updateThemeMode(val)
  },
})

const isDarkActive = computed(() => {
  if (injectedTheme?.isDark) return injectedTheme.isDark.value
  return localThemeMode.value === 'dark' || (localThemeMode.value === 'auto' && systemDark.value)
})

const activeThemeLabel = computed(() => {
  if (themeMode.value === 'auto') {
    return `Otomatis · ${isDarkActive.value ? 'Gelap' : 'Terang'}`
  }
  return isDarkActive.value ? 'Mode Gelap Aktif' : 'Mode Terang Aktif'
})

function handleSystemThemeChange(event) {
  systemDark.value = Boolean(event.matches)
  if (!injectedTheme) {
    applyLocalTheme(localThemeMode.value)
  }
}

function handleExternalThemeChange(event) {
  const next = event?.detail || readStoredTheme()
  if (VALID_THEMES.includes(next)) {
    localThemeMode.value = next
  }
}

async function load() {
  loading.value = true
  await Promise.all([loadHealth(), loadProfileTotal(), loadEngine(), loadSync()])
  await loadKernels()
  loading.value = false
}

onMounted(() => {
  if (mediaQuery) {
    systemDark.value = mediaQuery.matches
    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleSystemThemeChange)
    } else if (typeof mediaQuery.addListener === 'function') {
      mediaQuery.addListener(handleSystemThemeChange)
    }
  }
  window.addEventListener('cloakdesk:theme-change', handleExternalThemeChange)
  load()
})

onUnmounted(() => {
  if (mediaQuery) {
    if (typeof mediaQuery.removeEventListener === 'function') {
      mediaQuery.removeEventListener('change', handleSystemThemeChange)
    } else if (typeof mediaQuery.removeListener === 'function') {
      mediaQuery.removeListener(handleSystemThemeChange)
    }
  }
  window.removeEventListener('cloakdesk:theme-change', handleExternalThemeChange)
})

useOnRefresh(() => load())
</script>

<template>
  <div v-loading="loading" element-loading-text="Memuat informasi..." class="settings-page">
    <div class="settings-grid">
      <section class="info-card theme-card">
        <div class="sync-card-head">
          <div class="info-label">Tema Tampilan</div>
          <span class="sync-pill is-idle">{{ activeThemeLabel }}</span>
        </div>
        <div class="theme-options">
          <el-radio-group
            v-model="themeMode"
            size="small"
            class="status-segment theme-segment"
            aria-label="Tema Tampilan"
          >
            <el-radio-button value="auto">Otomatis (Ikuti Sistem)</el-radio-button>
            <el-radio-button value="dark">Mode Gelap</el-radio-button>
            <el-radio-button value="light">Mode Terang</el-radio-button>
          </el-radio-group>
        </div>
        <p class="info-help">
          Pilih skema warna antarmuka CloakDesk. Mode otomatis menyesuaikan dengan preferensi sistem operasi Anda.
        </p>
      </section>

      <section class="info-card sync-card">
        <div class="sync-card-head">
          <div class="info-label">Sinkronisasi Antar Perangkat</div>
          <span
            v-if="isSyncConfigured && !editingSync"
            :class="['sync-pill', syncInfo.last_sync_error ? 'is-bad' : 'is-ok']"
          >
            {{ syncInfo.last_sync_error ? 'Perlu Perhatian' : 'Terhubung' }}
          </span>
          <span v-else class="sync-pill is-idle">Belum Dikonfigurasi</span>
        </div>

        <template v-if="!isSyncConfigured || editingSync">
          <p class="info-help sync-intro">
            Hubungkan perangkat ini ke server sinkronisasi menggunakan token pendaftaran satu kali dan frasa sandi enkripsi bersama.
          </p>
          <el-form label-position="top" class="sync-form" @submit.prevent="submitSyncConfigure">
            <div class="sync-form-grid">
              <el-form-item label="URL Server" required>
                <el-input
                  v-model="syncForm.server_url"
                  placeholder="https://sync.example.com atau http://127.0.0.1:53210"
                />
              </el-form-item>
              <el-form-item label="Nama Perangkat">
                <el-input
                  v-model="syncForm.device_name"
                  placeholder="Contoh: Laptop-Utama"
                />
              </el-form-item>
              <el-form-item label="Token Pendaftaran (Enrollment Token)" required>
                <el-input
                  v-model="syncForm.enrollment_token"
                  placeholder="ad_enroll_..."
                />
              </el-form-item>
              <el-form-item label="Frasa Sandi Enkripsi (Passphrase)" required>
                <el-input
                  v-model="syncForm.passphrase"
                  type="password"
                  show-password
                  placeholder="Frasa sandi enkripsi cookie dan proxy"
                />
              </el-form-item>
            </div>
            <div class="sync-actions">
              <el-button type="primary" :loading="configureBusy" @click="submitSyncConfigure">
                Hubungkan Server
              </el-button>
              <el-button v-if="isSyncConfigured && editingSync" @click="cancelReconfigure">
                Batal
              </el-button>
            </div>
          </el-form>
        </template>

        <template v-else>
          <div class="sync-details-grid">
            <div class="sync-detail-item">
              <span class="sync-detail-label">URL Server</span>
              <span class="sync-detail-value mono">{{ syncInfo.server_url || '-' }}</span>
            </div>
            <div class="sync-detail-item">
              <span class="sync-detail-label">Nama Perangkat</span>
              <span class="sync-detail-value">{{ syncInfo.device_name || '-' }}</span>
            </div>
            <div class="sync-detail-item">
              <span class="sync-detail-label">ID Perangkat</span>
              <span class="sync-detail-value mono">{{ syncInfo.device_id || '-' }}</span>
            </div>
            <div class="sync-detail-item">
              <span class="sync-detail-label">Terakhir Sinkron</span>
              <span class="sync-detail-value">{{ syncInfo.last_sync_at || 'Belum pernah' }}</span>
            </div>
            <div class="sync-detail-item">
              <span class="sync-detail-label">Antrean Perubahan Belum Terkirim (Outbox)</span>
              <span class="sync-detail-value">{{ syncInfo.outbox_count ?? 0 }} item</span>
            </div>
            <div class="sync-detail-item">
              <span class="sync-detail-label">Status Koneksi</span>
              <span class="sync-detail-value health-line">
                <span :class="['status-dot', syncInfo.last_sync_error ? 'bad' : 'ok']"></span>
                <span>{{ syncInfo.last_sync_error ? `Gangguan: ${syncInfo.last_sync_error}` : 'Tersambung' }}</span>
              </span>
            </div>
          </div>

          <div class="sync-actions">
            <el-button type="primary" :loading="triggerBusy" @click="runSyncNow">
              Sinkronkan Sekarang
            </el-button>
            <el-button @click="openReconfigure">
              Ubah Konfigurasi / Hubungkan Ulang
            </el-button>
          </div>
        </template>

        <p v-if="syncError" class="info-help is-error">Gagal memuat status sinkronisasi: {{ syncError }}</p>
      </section>

      <section v-if="conflictCount > 0" class="info-card sync-conflict-card">
        <div class="sync-card-head">
          <div class="info-label">Konflik Sinkronisasi ({{ conflictCount }})</div>
          <span class="sync-pill is-bad">Perlu Tindakan</span>
        </div>
        <p class="info-help">
          Terdapat perbedaan perubahan antar perangkat. Pilih versi yang ingin dipertahankan untuk setiap item berikut.
        </p>
        <ul class="conflict-list">
          <li v-for="item in conflicts" :key="item.conflict_id" class="conflict-item">
            <div class="conflict-info">
              <div class="conflict-title">
                <span class="conflict-type">{{ item.entity_type }}</span>
                <strong>{{ item.entity_name || item.entity_uuid }}</strong>
                <span class="conflict-reason">{{ item.reason }}</span>
              </div>
              <div class="conflict-meta">
                <span class="mono">{{ item.entity_uuid }}</span>
                <span>·</span>
                <span>{{ item.created_at }}</span>
              </div>
            </div>
            <div class="conflict-actions">
              <el-button
                size="small"
                type="primary"
                :loading="resolvingConflictId === item.conflict_id"
                @click="resolveConflict(item.conflict_id, 'keep_local')"
              >
                Gunakan Versi Lokal
              </el-button>
              <el-button
                size="small"
                :loading="resolvingConflictId === item.conflict_id"
                @click="resolveConflict(item.conflict_id, 'keep_server')"
              >
                Gunakan Versi Server
              </el-button>
            </div>
          </li>
        </ul>
      </section>

      <section class="info-card">
        <div class="info-label">API Base</div>
        <div class="info-value mono">{{ apiBase }}</div>
        <p class="info-help">Permintaan aplikasi memakai origin ini. Dev server mem-proxy /api dan /health ke 127.0.0.1:53200.</p>
      </section>

      <section class="info-card">
        <div class="info-label">Status API</div>
        <div class="info-value health-line">
          <span :class="['status-dot', health ? 'ok' : healthError ? 'bad' : 'idle']"></span>
          <span>{{ health ? 'Tersambung' : healthError ? 'Tidak tersambung' : 'Memeriksa...' }}</span>
        </div>
        <p class="info-help">
          <template v-if="health">
            GET /health mengembalikan status {{ health.status }} dengan {{ health.open }} profil terbuka.
          </template>
          <template v-else-if="healthError">Permintaan /health gagal: {{ healthError }}</template>
          <template v-else>Menunggu respons /health.</template>
        </p>
      </section>

      <section class="info-card engine-card">
        <div class="kernel-head">
          <div class="kernel-head-main">
            <div class="info-label">Kernel Browser</div>
            <p class="kernel-paths">
              <span class="mono">{{ enginePath || '-' }}</span>
              <span class="kernel-paths-sep">tambahan:</span>
              <span class="mono">{{ extraKernelDir }}</span>
            </p>
          </div>
          <div class="kernel-head-actions">
            <span v-if="kernelSummary" class="kernel-summary">{{ kernelSummary }}</span>
            <el-button type="primary" size="small" :icon="Plus" @click="openKernelInstallDialog">
              Pasang Kernel Baru
            </el-button>
          </div>
        </div>

        <el-table
          :data="kernels"
          size="small"
          class="kernel-table"
          :empty-text="kernelsError ? 'Daftar kernel tidak tersedia' : 'Belum ada kernel terpasang'"
        >
          <el-table-column
            v-for="column in kernelColumns"
            :key="column.key"
            :label="column.label"
            :width="column.width"
            :align="column.align"
          >
            <template #default="{ row }">
              <template v-if="column.key === 'name'">
                <div class="kernel-cell-name">{{ row.name || row.id }}</div>
                <div v-if="row.id && row.id !== row.name" class="kernel-cell-id mono">{{ row.id }}</div>
              </template>
              <template v-else-if="column.key === 'version'">
                <span class="kernel-cell-version mono">{{ row.version || 'Tidak terbaca' }}</span>
                <span v-if="kernelMajorLabel(row)" class="kernel-major">{{ kernelMajorLabel(row) }}</span>
              </template>
              <template v-else-if="column.key === 'size'">
                {{ row.disk_size_mb || 0 }} MB
              </template>
              <template v-else-if="column.key === 'profiles'">
                {{ row.profile_count || 0 }} profil
              </template>
              <template v-else-if="column.key === 'status'">
                <span class="kernel-status">
                  <span v-if="row.is_builtin" class="kernel-badge is-builtin">Bawaan</span>
                  <span v-if="isKernelDefault(row)" class="kernel-badge is-default">Default</span>
                </span>
              </template>
              <template v-else-if="column.key === 'actions'">
                <div class="kernel-actions">
                  <el-button
                    size="small"
                    :disabled="isKernelDefault(row)"
                    :loading="kernelBusyId === row.id"
                    @click="makeKernelDefault(row)"
                  >
                    Jadikan Default
                  </el-button>
                  <el-tooltip
                    :content="kernelDeleteBlockReason(row)"
                    :disabled="!kernelDeleteBlockReason(row)"
                    placement="top"
                  >
                    <span class="kernel-delete-wrap">
                      <el-button
                        size="small"
                        type="danger"
                        plain
                        :disabled="Boolean(kernelDeleteBlockReason(row))"
                        :loading="kernelBusyId === row.id"
                        @click="deleteKernel(row)"
                      >
                        Hapus
                      </el-button>
                    </span>
                  </el-tooltip>
                </div>
              </template>
            </template>
          </el-table-column>
        </el-table>

        <p v-if="kernelsError" class="info-help is-error">
          kernel-list gagal: {{ kernelsError }}. Menampilkan data dari engine-info.
        </p>
        <p v-else-if="engineError && !kernels.length" class="info-help is-error">
          engine-info gagal: {{ engineError }}
        </p>
        <p v-else class="info-help">
          Kernel default otomatis dipakai untuk profil baru. Kernel yang masih dipakai profil aktif tidak bisa dihapus.
        </p>
      </section>

      <section class="info-card">
        <div class="info-label">Jumlah Profil</div>
        <div class="info-value">{{ profileTotal === null ? 'Tidak tersedia' : `${profileTotal} profil` }}</div>
        <p class="info-help">
          <template v-if="profilesError">profile-list gagal: {{ profilesError }}</template>
          <template v-else>Total dari profile-list dengan limit 1.</template>
        </p>
      </section>

      <section class="info-card">
        <div class="info-label">Versi UI</div>
        <div class="info-value">{{ UI_VERSION }}</div>
        <p class="info-help">Versi antarmuka di folder ui/.</p>
      </section>
    </div>

    <el-dialog
      v-model="installDialogVisible"
      title="Pasang Kernel Baru"
      width="520px"
      :close-on-click-modal="!installBusy"
      :close-on-press-escape="!installBusy"
      :show-close="!installBusy"
    >
      <el-form label-position="top" class="kernel-install-form" @submit.prevent="submitKernelInstall">
        <el-form-item label="Sumber Kernel">
          <el-radio-group v-model="installForm.source_type" :disabled="installBusy">
            <el-radio value="local_archive">Arsip Lokal (.tar.xz, .tar.gz, .zip)</el-radio>
            <el-radio value="local_dir">Folder Direktori Lokal</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="installSourceLabel" :error="installErrors.source_path" required>
          <el-input
            v-model="installForm.source_path"
            class="mono"
            :placeholder="installPlaceholder"
            :disabled="installBusy"
            spellcheck="false"
          />
        </el-form-item>
        <el-form-item label="Nama Kernel">
          <el-input
            v-model="installForm.kernel_name"
            maxlength="60"
            placeholder="Opsional, cth: Chromium 152"
            :disabled="installBusy"
          />
        </el-form-item>
        <el-checkbox v-model="installForm.set_default" :disabled="installBusy" class="kernel-install-default">
          Jadikan kernel default untuk pembuatan profil baru
        </el-checkbox>
        <p class="info-help">
          Biner chrome dibaca versinya dan divalidasi sebagai ELF x86_64 sebelum dipasang. Proses ini butuh beberapa detik
          untuk arsip berukuran ratusan megabita.
        </p>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button :disabled="installBusy" @click="installDialogVisible = false">Batal</el-button>
          <el-button type="primary" :loading="installBusy" @click="submitKernelInstall">
            {{ installBusy ? 'Memvalidasi & mengekstrak biner...' : 'Pasang' }}
          </el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>
