<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Brush, Connection, CopyDocument, Delete, Document, Download, Edit, Files, FolderOpened, Grid, Key, Lock, Menu, MoreFilled, Plus, Refresh, Search, Star, Upload } from '@element-plus/icons-vue'
import { api } from '../api'
import { useOnRefresh } from '../refresh'
import { accountSummary } from '../platform'
import ProfileDialog from '../components/ProfileDialog.vue'
import CookieDialog from '../components/CookieDialog.vue'
import BookmarkDialog from '../components/BookmarkDialog.vue'
import PasswordDialog from '../components/PasswordDialog.vue'
import ProfileLogDialog from '../components/ProfileLogDialog.vue'
import ImportCsvDialog from '../components/ImportCsvDialog.vue'
import ImportIxbrowserDialog from '../components/ImportIxbrowserDialog.vue'
import GroupManager from '../components/GroupManager.vue'

const POOL_LIMIT = 1000
const COLUMN_STORAGE_KEY = 'antidetect.profiles.columns'
const COLUMN_VERSION_KEY = 'antidetect.profiles.columns.version'
const COLUMN_VERSION = 2
const COLUMN_OPTIONS = [
  { key: 'name', label: 'Nama Profil' },
  { key: 'group', label: 'Grup' },
  { key: 'tag', label: 'Tag' },
  { key: 'platform', label: 'Platform' },
  { key: 'proxy', label: 'Proxy' },
  { key: 'account', label: 'Account' },
  { key: 'status', label: 'Status' },
  { key: 'created', label: 'Dibuat' },
  { key: 'last_open', label: 'Terakhir Dibuka' },
  { key: 'note', label: 'Catatan' },
  { key: 'process', label: 'Waktu Proses' },
]
const DEFAULT_COLUMNS = [
  'name',
  'group',
  'tag',
  'platform',
  'proxy',
  'account',
  'status',
  'created',
  'last_open',
]

function orderColumns(keys) {
  return COLUMN_OPTIONS.map((column) => column.key).filter((key) => keys.includes(key))
}

function loadVisibleColumns() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLUMN_STORAGE_KEY) || '')
    if (!Array.isArray(saved)) return [...DEFAULT_COLUMNS]
    const version = Number(localStorage.getItem(COLUMN_VERSION_KEY) || 0)
    if (version < COLUMN_VERSION) {
      // Kolom baru masuk daftar bawaan, pilihan lama tetap dihormati.
      const merged = orderColumns([...saved, ...DEFAULT_COLUMNS])
      localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(merged))
      localStorage.setItem(COLUMN_VERSION_KEY, String(COLUMN_VERSION))
      return merged
    }
    return orderColumns(saved)
  } catch {
    return [...DEFAULT_COLUMNS]
  }
}

const rows = ref([])
const pool = ref([])
const total = ref(0)
const groups = ref([])
const tags = ref([])
const opened = ref(new Set())
const openedSince = ref({})
const nowTick = ref(Date.now())
const cooldowns = ref({})
const now = ref(Date.now())
const visibleColumns = ref(loadVisibleColumns())
const loading = ref(false)
const selected = ref([])
const page = ref(1)
const limit = ref(10)
const search = ref('')
const groupFilter = ref('all')
const tagFilter = ref('all')
const statusFilter = ref('all')
const sortBy = ref('created')
const sortDir = ref('desc')
const busy = ref({})
const bulkBusy = ref(false)
const tableRef = ref(null)
const dialogVisible = ref(false)
const editingProfile = ref(null)
const openAfterSave = ref(false)
const groupManagerVisible = ref(false)
const cookieVisible = ref(false)
const cookieProfile = ref(null)
const bookmarkVisible = ref(false)
const bookmarkProfile = ref(null)
const passwordVisible = ref(false)
const passwordProfile = ref(null)
const logVisible = ref(false)
const logProfile = ref(null)
const logKind = ref('chrome')
const gostLogs = ref({})
const importInput = ref(null)
const importBusy = ref(false)
const csvVisible = ref(false)
const ixbVisible = ref(false)
const bulkKind = ref('')
const bulkVisible = ref(false)
const arrangeBusy = ref(false)

const PLATFORM_VERSIONS = {
  Windows: ['11', '10', '8.1', '7'],
  Macos: ['14', '13', '12'],
}
const BULK_TITLES = { group: 'Ubah Grup', proxy: 'Ubah Proxy', platform: 'Ubah Platform' }

const bulkForm = reactive({
  group_id: null,
  proxy_type: 'direct',
  proxy_ip: '',
  proxy_port: null,
  proxy_user: '',
  proxy_password: '',
  platform: 'Windows',
  system_version: '11',
})
const bulkErrors = reactive({ proxy_ip: '', proxy_port: '' })
const bulkVersionOptions = computed(() => PLATFORM_VERSIONS[bulkForm.platform] || [])
const bulkTitle = computed(() => BULK_TITLES[bulkKind.value] || 'Operasi Massal')

const DATA_ACTIONS = {
  'clear-cache': {
    title: 'Bersihkan Cache',
    message: (name) => `Bersihkan cache profil "${name}"? Cache halaman, Code Cache, GPUCache, dan Service Worker akan dihapus.`,
    confirm: 'Bersihkan',
    verb: 'membersihkan cache',
    run: (id) => api.clearCache(id),
    success: (name) => `Cache profil "${name}" dibersihkan`,
  },
  'clear-cache-cookies': {
    title: 'Bersihkan Cache dan Cookie',
    message: (name) => `Bersihkan cache dan cookie profil "${name}"? Semua sesi login di profil ini akan hilang.`,
    confirm: 'Bersihkan',
    verb: 'membersihkan cache dan cookie',
    run: (id) => api.clearCacheAndCookies(id),
    success: (name) => `Cache dan cookie profil "${name}" dibersihkan`,
  },
  'clear-cache-extensions': {
    title: 'Bersihkan Cache',
    message: (name) => `Bersihkan cache profil "${name}"? Data extension tetap disimpan.`,
    confirm: 'Bersihkan',
    verb: 'membersihkan cache',
    run: (id) => api.clearCacheKeepExtensions(id),
    success: (name) => `Cache profil "${name}" dibersihkan, extension dipertahankan`,
  },
  'clear-password': {
    title: 'Hapus Password Tersimpan',
    message: (name) => `Hapus password tersimpan profil "${name}"? Isian login yang tersimpan di browser akan dihapus.`,
    confirm: 'Hapus',
    verb: 'menghapus password tersimpan',
    run: (id) => api.clearSavedPassword(id),
    success: (name) => `Password tersimpan profil "${name}" dihapus`,
  },
}

let searchTimer = null
let pollTimer = null
let clockTimer = null
let cooldownTimer = null

const groupMap = computed(() => new Map(groups.value.map((group) => [group.id, group.title])))
const tagMap = computed(() => new Map(tags.value.map((tag) => [tag.id, tag.title])))
const selectedIds = computed(() => selected.value.map((profile) => profile.profile_id))

function isOpen(profile) {
  return opened.value.has(profile.profile_id)
}

const filteredPool = computed(() => {
  if (statusFilter.value === 'open') return pool.value.filter((profile) => isOpen(profile))
  if (statusFilter.value === 'closed') return pool.value.filter((profile) => !isOpen(profile))
  return pool.value
})

const visibleRows = computed(() => {
  if (statusFilter.value === 'all') return rows.value
  const start = (page.value - 1) * limit.value
  return filteredPool.value.slice(start, start + limit.value)
})

const displayTotal = computed(() => (statusFilter.value === 'all' ? total.value : filteredPool.value.length))

const hasFilter = computed(
  () =>
    !!search.value.trim() ||
    groupFilter.value !== 'all' ||
    tagFilter.value !== 'all' ||
    statusFilter.value !== 'all'
)

async function loadProfiles() {
  loading.value = true
  try {
    const all = statusFilter.value === 'all'
    const result = await api.profiles({
      page: all ? page.value : 1,
      limit: all ? limit.value : POOL_LIMIT,
      name: search.value.trim() || undefined,
      group_id: groupFilter.value === 'all' ? undefined : groupFilter.value,
      tab_id: tagFilter.value === 'all' ? undefined : tagFilter.value,
      sort_by: sortBy.value,
      sort_dir: sortDir.value,
    })
    if (all) {
      rows.value = result.data || []
      total.value = result.total || 0
    } else {
      pool.value = result.data || []
    }
  } catch (error) {
    ElMessage.error(`Gagal memuat profil: ${error.message}`)
  } finally {
    loading.value = false
  }
}

async function loadGroups() {
  try {
    const result = await api.groups()
    groups.value = result.data || []
  } catch (error) {
    ElMessage.error(`Gagal memuat grup: ${error.message}`)
  }
}

async function loadTags() {
  try {
    const result = await api.tags()
    tags.value = result.data || []
  } catch (error) {
    ElMessage.error(`Gagal memuat tag: ${error.message}`)
  }
}

async function pollOpened() {
  try {
    const list = await api.openedList()
    opened.value = new Set((list || []).map((item) => item.profile_id))
    const since = {}
    for (const item of list || []) {
      if (item.started_at) since[item.profile_id] = item.started_at
    }
    openedSince.value = since
  } catch {
    // indikator API di topbar yang memberi tahu saat server tidak merespons
  }
}

async function refreshAll() {
  await Promise.all([loadGroups(), loadProfiles(), loadTags(), pollOpened()])
}

function clearSelection() {
  selected.value = []
  tableRef.value?.clearSelection()
}

function onSelectionChange(value) {
  selected.value = value
}

function isColumnVisible(key) {
  return visibleColumns.value.includes(key)
}

function toggleColumn(key) {
  const next = visibleColumns.value.includes(key)
    ? visibleColumns.value.filter((item) => item !== key)
    : [...visibleColumns.value, key]
  visibleColumns.value = orderColumns(next)
  localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(visibleColumns.value))
  localStorage.setItem(COLUMN_VERSION_KEY, String(COLUMN_VERSION))
}

function showAllColumns() {
  visibleColumns.value = [...DEFAULT_COLUMNS]
  localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify(DEFAULT_COLUMNS))
  localStorage.setItem(COLUMN_VERSION_KEY, String(COLUMN_VERSION))
}

function onPageChange(value) {
  page.value = value
  if (statusFilter.value === 'all') loadProfiles()
}

const SORT_FIELDS = { name: 'name', group: 'group', last_open: 'last_open' }

function onSortChange({ prop, order }) {
  if (!order || !SORT_FIELDS[prop]) {
    sortBy.value = 'created'
    sortDir.value = 'desc'
  } else {
    sortBy.value = SORT_FIELDS[prop]
    sortDir.value = order === 'ascending' ? 'asc' : 'desc'
  }
  page.value = 1
  clearSelection()
  loadProfiles()
}

async function checkGostLog(profile) {
  try {
    const result = await api.profileLogs(profile.profile_id, 'gost')
    gostLogs.value = { ...gostLogs.value, [profile.profile_id]: !!result.exists }
  } catch {
    gostLogs.value = { ...gostLogs.value, [profile.profile_id]: false }
  }
}

function onLogMenuVisible(visible, profile) {
  if (visible) checkGostLog(profile)
}

function openLog(profile, kind) {
  logProfile.value = profile
  logKind.value = kind
  logVisible.value = true
}

function onSizeChange(value) {
  limit.value = value
  page.value = 1
  clearSelection()
  if (statusFilter.value === 'all') loadProfiles()
}

watch(search, () => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    clearSelection()
    loadProfiles()
  }, 300)
})

watch(groupFilter, () => {
  page.value = 1
  clearSelection()
  loadProfiles()
})

watch(tagFilter, () => {
  page.value = 1
  clearSelection()
  loadProfiles()
})

watch(statusFilter, () => {
  page.value = 1
  clearSelection()
  loadProfiles()
})

watch(
  () => bulkForm.platform,
  (platform) => {
    const versions = PLATFORM_VERSIONS[platform] || []
    if (!versions.includes(bulkForm.system_version)) bulkForm.system_version = versions[0] || ''
  }
)

watch(displayTotal, (value) => {
  const maxPage = Math.max(1, Math.ceil(value / limit.value))
  if (page.value > maxPage) page.value = maxPage
})

async function openCreate(andOpen = false) {
  editingProfile.value = null
  openAfterSave.value = andOpen
  await loadGroups()
  dialogVisible.value = true
}

function onCreateCommand(command) {
  openCreate(command === 'open')
}

async function openEdit(profile) {
  editingProfile.value = profile
  openAfterSave.value = false
  await loadGroups()
  dialogVisible.value = true
}

function markBusy(profileId, value) {
  const next = { ...busy.value }
  if (value) next[profileId] = true
  else delete next[profileId]
  busy.value = next
}

function markOpenedLocally(profileId) {
  const stamp = Math.floor(Date.now() / 1000)
  for (const list of [rows.value, pool.value]) {
    const profile = list.find((item) => item.profile_id === profileId)
    if (profile) profile.last_open_time = stamp
  }
}

function getCooldown(profileId) {
  const target = cooldowns.value[profileId]
  if (!target) return 0
  const rem = Math.ceil((target - now.value) / 1000)
  if (rem <= 0) {
    delete cooldowns.value[profileId]
    return 0
  }
  return rem
}

function startCooldown(profileId, seconds = 30) {
  cooldowns.value[profileId] = Date.now() + seconds * 1000
}

async function openById(profileId, name) {
  markBusy(profileId, true)
  try {
    await api.openProfile(profileId)
    markOpenedLocally(profileId)
    opened.value = new Set([...opened.value, profileId])
    ElMessage.success(`Profil "${name}" dibuka`)
  } catch (error) {
    if (error.code === 1009) {
      ElMessage.warning(`Profil "${name}" terkunci oleh perangkat lain: ${error.message}`)
      await loadProfiles()
    } else {
      ElMessage.error(`Gagal membuka: ${error.message}`)
    }
  } finally {
    markBusy(profileId, false)
    await pollOpened()
  }
}

async function toggleOpen(row) {
  const wasOpen = isOpen(row)
  let forceOpen = false
  if (!wasOpen && row.sync_lease && !row.sync_lease.is_local) {
    try {
      await ElMessageBox.confirm(
        'Profil ini sedang dibuka di perangkat ' + row.sync_lease.holder_device_name + '. Ambil alih kunci dan buka paksa di sini?',
        'Konfirmasi Ambil Alih Kunci',
        {
          confirmButtonText: 'Ambil Alih',
          cancelButtonText: 'Batal',
          type: 'warning',
        }
      )
      forceOpen = true
    } catch {
      return
    }
  }
  if (!wasOpen && getCooldown(row.profile_id) > 0) {
    ElMessage.warning(`Profil "${row.name}" masih dalam cooldown. Harap tunggu ${getCooldown(row.profile_id)} detik lagi.`)
    return
  }
  markBusy(row.profile_id, true)
  try {
    if (wasOpen) {
      await api.closeProfile(row.profile_id)
      startCooldown(row.profile_id, 30)
      const next = new Set(opened.value)
      next.delete(row.profile_id)
      opened.value = next
      ElMessage.success(`Profil "${row.name}" ditutup`)
      await loadProfiles()
    } else {
      if (forceOpen) {
        await api.openProfile(row.profile_id, { force: true })
      } else {
        await api.openProfile(row.profile_id)
      }
      markOpenedLocally(row.profile_id)
      opened.value = new Set([...opened.value, row.profile_id])
      ElMessage.success(`Profil "${row.name}" dibuka`)
      await loadProfiles()
    }
  } catch (error) {
    if (!wasOpen && error.code === 1009) {
      ElMessage.warning(`Profil "${row.name}" terkunci oleh perangkat lain: ${error.message}`)
      await loadProfiles()
    } else if (!wasOpen && error.code === 1010) {
      ElMessage.warning(`Profil "${row.name}" masih dalam cooldown: ${error.message}`)
      await loadProfiles()
    } else {
      ElMessage.error(`${wasOpen ? 'Gagal menutup' : 'Gagal membuka'}: ${error.message}`)
    }
  } finally {
    markBusy(row.profile_id, false)
    await pollOpened()
  }
}

async function duplicateProfile(profile) {
  try {
    await ElMessageBox.confirm(`Duplikat profil "${profile.name}"?`, 'Duplikat Profil', {
      confirmButtonText: 'Duplikat',
      cancelButtonText: 'Batal',
    })
  } catch {
    return
  }
  try {
    await api.copyProfile(profile.profile_id, `${profile.name} copy`)
    ElMessage.success('Profil diduplikasi')
    await loadProfiles()
    await pollOpened()
  } catch (error) {
    ElMessage.error(`Gagal menduplikasi: ${error.message}`)
  }
}

async function removeProfile(profile) {
  try {
    await ElMessageBox.confirm(
      `Profil "${profile.name}" dipindahkan ke Keranjang Sampah. Lanjutkan?`,
      'Pindahkan ke Keranjang Sampah',
      {
        type: 'warning',
        confirmButtonText: 'Pindahkan',
        cancelButtonText: 'Batal',
        confirmButtonClass: 'el-button--danger',
      }
    )
  } catch {
    return
  }
  try {
    await api.deleteProfile(profile.profile_id)
    ElMessage.success(`Profil "${profile.name}" dipindahkan ke keranjang sampah`)
    clearSelection()
    if (visibleRows.value.length === 1 && page.value > 1) page.value -= 1
    await loadProfiles()
    await pollOpened()
  } catch (error) {
    ElMessage.error(`Gagal memindahkan profil: ${error.message}`)
  }
}

function openCookie(profile) {
  cookieProfile.value = profile
  cookieVisible.value = true
}

function openBookmarks(profile) {
  bookmarkProfile.value = profile
  bookmarkVisible.value = true
}

function openPasswords(profile) {
  passwordProfile.value = profile
  passwordVisible.value = true
}

function triggerImport() {
  importInput.value?.click()
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('file tidak bisa dibaca'))
    reader.readAsDataURL(file)
  })
}

async function onImportFile(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  importBusy.value = true
  try {
    const content = await readBase64(file)
    let profile
    try {
      profile = await api.importProfile(content)
    } catch (error) {
      if (!/terenkripsi/i.test(error.message || '')) throw error
      let password
      try {
        const answer = await ElMessageBox.prompt(
          `File "${file.name}" terenkripsi. Masukkan kata sandi untuk membukanya.`,
          'Kata Sandi Impor',
          {
            inputType: 'password',
            inputPlaceholder: 'Kata sandi file',
            confirmButtonText: 'Impor',
            cancelButtonText: 'Batal',
          }
        )
        password = answer.value || ''
      } catch {
        return
      }
      profile = await api.importProfile(content, password)
    }
    page.value = 1
    clearSelection()
    await Promise.all([loadProfiles(), loadGroups(), loadTags()])
    ElMessage.success(`Profil "${profile?.name || file.name}" diimpor`)
  } catch (error) {
    ElMessage.error(`Gagal mengimpor profil: ${error.message}`)
  } finally {
    importBusy.value = false
  }
}

async function onCsvImported(result) {
  page.value = 1
  clearSelection()
  await Promise.all([loadProfiles(), loadGroups()])
  const problems = (result?.errors || []).length
  if (problems) {
    ElMessage.warning(
      `Impor CSV selesai dengan ${problems} masalah: ${result.created} profil dibuat, ${result.groups_created} grup baru, ${result.proxies_created} proxy baru`
    )
  } else if (result?.created) {
    ElMessage.success(`${result.created} profil diimpor dari CSV`)
  }
}

async function onIxbrowserImported(result) {
  page.value = 1
  clearSelection()
  await Promise.all([loadProfiles(), loadGroups()])
  const problems = (result?.errors || []).length
  if (problems) {
    ElMessage.warning(
      `Impor ixBrowser selesai dengan ${problems} masalah: ${result.created} dibuat, ${result.updated} diperbarui, ${result.skipped} dilewati`
    )
  } else {
    ElMessage.success(
      `Impor ixBrowser selesai: ${result?.created ?? 0} dibuat, ${result?.updated ?? 0} diperbarui, ${result?.skipped ?? 0} dilewati`
    )
  }
}

function base64ToBlob(base64) {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return new Blob([bytes], { type: 'application/json' })
}

async function exportProfile(profile, encrypted = false) {
  let password = ''
  if (encrypted) {
    try {
      const answer = await ElMessageBox.prompt(
        `Ekspor profil "${profile.name}" dengan kata sandi. Kata sandi minimal 8 karakter.`,
        'Ekspor Terenkripsi',
        {
          inputType: 'password',
          inputPlaceholder: 'Kata sandi ekspor',
          confirmButtonText: 'Ekspor',
          cancelButtonText: 'Batal',
          inputValidator: (value) =>
            String(value || '').length >= 8 || 'Kata sandi minimal 8 karakter',
        }
      )
      password = answer.value || ''
    } catch {
      return
    }
  }
  try {
    const result = await api.exportProfile(profile.profile_id, password)
    const url = URL.createObjectURL(base64ToBlob(result.content_base64))
    const link = document.createElement('a')
    link.href = url
    link.download = result.filename || `profil-${profile.name}.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success(
      result.encrypted ? `Profil "${profile.name}" diekspor terenkripsi` : `Profil "${profile.name}" diekspor`
    )
  } catch (error) {
    ElMessage.error(`Gagal mengekspor: ${error.message}`)
  }
}

async function runDataAction(profile, key) {
  const action = DATA_ACTIONS[key]
  if (!action) return
  try {
    await ElMessageBox.confirm(action.message(profile.name), action.title, {
      type: 'warning',
      confirmButtonText: action.confirm,
      cancelButtonText: 'Batal',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  try {
    await action.run(profile.profile_id)
    ElMessage.success(action.success(profile.name))
  } catch (error) {
    ElMessage.error(`Gagal ${action.verb}: ${error.message}`)
  } finally {
    await pollOpened()
  }
}

function onRowCommand(command, profile) {
  if (command === 'delete') {
    removeProfile(profile)
    return
  }
  if (command === 'cookie') {
    openCookie(profile)
    return
  }
  if (command === 'export') {
    exportProfile(profile)
    return
  }
  if (command === 'export-encrypted') {
    exportProfile(profile, true)
    return
  }
  if (command === 'bookmarks') {
    openBookmarks(profile)
    return
  }
  if (command === 'passwords') {
    openPasswords(profile)
    return
  }
  if (command === 'log-chrome') {
    openLog(profile, 'chrome')
    return
  }
  if (command === 'log-gost') {
    openLog(profile, 'gost')
    return
  }
  if (DATA_ACTIONS[command]) runDataAction(profile, command)
}

async function runBulk(command) {
  const chosen = [...selected.value]
  if (!chosen.length) return

  if (command === 'note') {
    let result
    try {
      result = await ElMessageBox.prompt(
        `Ubah catatan ${chosen.length} profil terpilih. Kosongkan isian untuk menghapus catatan.`,
        'Ubah Catatan',
        {
          confirmButtonText: 'Simpan',
          cancelButtonText: 'Batal',
          inputType: 'textarea',
          inputPlaceholder: 'Catatan baru',
        }
      )
    } catch {
      return
    }
    const note = String(result.value || '').trim()
    bulkBusy.value = true
    try {
      await api.batchUpdate(
        chosen.map((profile) => profile.profile_id),
        { note: note || null }
      )
      await loadProfiles()
      clearSelection()
      ElMessage.success(
        note ? `${chosen.length} catatan profil diperbarui` : `${chosen.length} catatan profil dihapus`
      )
    } catch (error) {
      ElMessage.error(`Gagal mengubah catatan: ${error.message}`)
    } finally {
      bulkBusy.value = false
    }
    return
  }

  if (command === 'group' || command === 'proxy' || command === 'platform') {
    bulkErrors.proxy_ip = ''
    bulkErrors.proxy_port = ''
    bulkForm.group_id = groups.value[0]?.id ?? null
    bulkForm.proxy_type = 'direct'
    bulkForm.proxy_ip = ''
    bulkForm.proxy_port = null
    bulkForm.proxy_user = ''
    bulkForm.proxy_password = ''
    bulkForm.platform = 'Windows'
    bulkForm.system_version = '11'
    bulkKind.value = command
    bulkVisible.value = true
    return
  }

  if (command === 'delete') {
    try {
      await ElMessageBox.confirm(
        `Pindahkan ${chosen.length} profil terpilih ke Keranjang Sampah. Lanjutkan?`,
        'Pindahkan ke Keranjang Sampah',
        {
          type: 'warning',
          confirmButtonText: 'Pindahkan',
          cancelButtonText: 'Batal',
          confirmButtonClass: 'el-button--danger',
        }
      )
    } catch {
      return
    }
    bulkBusy.value = true
    let ok = 0
    let fail = 0
    for (const profile of chosen) {
      try {
        await api.deleteProfile(profile.profile_id)
        ok += 1
      } catch {
        fail += 1
      }
    }
    bulkBusy.value = false
    clearSelection()
    await loadProfiles()
    await pollOpened()
    if (ok) ElMessage.success(`${ok} profil dipindahkan ke keranjang sampah`)
    if (fail) ElMessage.error(`${fail} profil gagal dipindahkan`)
    return
  }

  if (command === 'open') {
    const targets = chosen.filter((profile) => !isOpen(profile))
    if (!targets.length) {
      ElMessage.info('Semua profil terpilih sudah terbuka')
      return
    }
    bulkBusy.value = true
    let ok = 0
    let fail = 0
    const next = new Set(opened.value)
    for (const profile of targets) {
      try {
        await api.openProfile(profile.profile_id)
        next.add(profile.profile_id)
        ok += 1
      } catch {
        fail += 1
      }
    }
    opened.value = next
    bulkBusy.value = false
    clearSelection()
    await pollOpened()
    if (ok) ElMessage.success(`${ok} profil dibuka`)
    if (fail) ElMessage.error(`${fail} profil gagal dibuka`)
    return
  }

  if (command === 'close') {
    const targets = chosen.filter((profile) => isOpen(profile))
    if (!targets.length) {
      ElMessage.info('Tidak ada profil terpilih yang terbuka')
      return
    }
    bulkBusy.value = true
    try {
      await api.closeProfiles(targets.map((profile) => profile.profile_id))
      const next = new Set(opened.value)
      for (const profile of targets) {
        next.delete(profile.profile_id)
        startCooldown(profile.profile_id, 30)
      }
      opened.value = next
      ElMessage.success(`${targets.length} profil ditutup`)
    } catch (error) {
      ElMessage.error(`Gagal menutup profil terpilih: ${error.message}`)
    } finally {
      bulkBusy.value = false
      clearSelection()
      await pollOpened()
    }
  }
}

async function submitBulkEdit() {
  const ids = selectedIds.value
  if (!ids.length) return
  bulkErrors.proxy_ip = ''
  bulkErrors.proxy_port = ''

  let payload = null
  let message = ''
  if (bulkKind.value === 'group') {
    if (!bulkForm.group_id) {
      ElMessage.warning('Pilih grup tujuan')
      return
    }
    payload = () => api.updateGroupsInBatches(ids, bulkForm.group_id)
    message = `${ids.length} profil dipindahkan ke ${groupTitle(bulkForm.group_id)}`
  } else if (bulkKind.value === 'proxy') {
    let proxy = { proxy_type: 'direct' }
    if (bulkForm.proxy_type !== 'direct') {
      if (!bulkForm.proxy_ip.trim()) bulkErrors.proxy_ip = 'Host wajib diisi'
      const port = Number(bulkForm.proxy_port)
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        bulkErrors.proxy_port = 'Port harus 1 sampai 65535'
      }
      if (bulkErrors.proxy_ip || bulkErrors.proxy_port) return
      proxy = {
        proxy_type: bulkForm.proxy_type,
        proxy_ip: bulkForm.proxy_ip.trim(),
        proxy_port: port,
        proxy_user: bulkForm.proxy_user.trim() || null,
        proxy_password: bulkForm.proxy_password || null,
      }
    }
    payload = () => api.batchUpdate(ids, { proxy_config: proxy })
    message = `${ids.length} proxy profil diperbarui`
  } else if (bulkKind.value === 'platform') {
    payload = () =>
      api.batchUpdate(ids, {
        fingerprint_config: {
          platform: bulkForm.platform,
          system_version: bulkForm.system_version,
        },
      })
    message = `${ids.length} platform profil diperbarui`
  }

  if (!payload) return
  bulkBusy.value = true
  try {
    await payload()
    bulkVisible.value = false
    clearSelection()
    await loadProfiles()
    ElMessage.success(message)
  } catch (error) {
    ElMessage.error(`Gagal mengubah profil: ${error.message}`)
  } finally {
    bulkBusy.value = false
  }
}

async function arrangeWindows() {
  arrangeBusy.value = true
  try {
    const result = await api.arrangeTile()
    ElMessage.success(`${result?.arranged ?? 0} jendela diatur`)
  } catch (error) {
    ElMessage.error(`Gagal mengatur jendela: ${error.message}`)
  } finally {
    arrangeBusy.value = false
  }
}

async function onGroupsChanged() {
  await loadGroups()
  if (groupFilter.value !== 'all' && !groups.value.some((group) => group.id === groupFilter.value)) {
    groupFilter.value = 'all'
    return
  }
  await loadProfiles()
}

async function onSaved({ mode, profileId, name, seed }) {
  if (mode === 'seed') {
    if (editingProfile.value) editingProfile.value.seed = seed
    await loadProfiles()
    ElMessage.success(`Seed profil "${name}" diperbarui`)
    return
  }
  dialogVisible.value = false
  clearSelection()
  await Promise.all([loadProfiles(), loadTags()])
  await pollOpened()
  ElMessage.success(mode === 'create' ? `Profil "${name}" dibuat` : `Profil "${name}" diperbarui`)
  if (openAfterSave.value && mode === 'create') {
    openAfterSave.value = false
    await openById(profileId, name)
  }
}

function groupTitle(groupId) {
  return groupMap.value.get(groupId) || `Grup ${groupId}`
}

function profileTags(profile) {
  const ids = Array.isArray(profile.tab_id) ? profile.tab_id : []
  return ids.map((id) => tagMap.value.get(id)).filter(Boolean)
}

function platformLabel(profile) {
  const config = profile.fingerprint_config || {}
  const platform = config.platform === 'Macos' ? 'macOS' : config.platform || 'Windows'
  if (platform === 'Windows' && config.system_version) return `${platform} ${config.system_version}`
  return platform
}

function proxyTypeLabel(profile) {
  const config = profile.proxy_config || {}
  const type = String(config.proxy_type || 'direct').toLowerCase()
  if (!type || type === 'direct') return 'Direct'
  return type.toUpperCase()
}

function accountLabel(profile) {
  return accountSummary(profile).label
}

function accountHost(profile) {
  const summary = accountSummary(profile)
  return summary.detail || ''
}

function proxyHostLabel(profile) {
  const config = profile.proxy_config || {}
  const type = String(config.proxy_type || 'direct').toLowerCase()
  if (!type || type === 'direct' || !config.proxy_ip) return 'Tanpa proxy'
  return `${config.proxy_ip}:${config.proxy_port ?? ''}`
}

function timestampLabel(value) {
  if (!value) return ''
  if (typeof value === 'number') {
    const date = new Date(value * 1000)
    if (Number.isNaN(date.getTime())) return ''
    const pad = (part) => String(part).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  }
  const [date, time] = String(value).split(' ')
  return `${date || ''} ${(time || '').slice(0, 5)}`.trim()
}

function createdLabel(value) {
  return timestampLabel(value) || '-'
}

function dateLabel(value) {
  return timestampLabel(value).split(' ')[0] || ''
}

function timeLabel(value) {
  return timestampLabel(value).split(' ')[1] || ''
}

function lastOpenedLabel(profile) {
  return timestampLabel(profile.last_open_time) || 'Belum dibuka'
}

function processStart(profile) {
  const raw = openedSince.value[profile.profile_id]
  if (!raw) return 0
  const stamp = new Date(String(raw).replace(' ', 'T')).getTime()
  return Number.isNaN(stamp) ? 0 : stamp
}

function processTimeLabel(profile) {
  if (!isOpen(profile)) return '-'
  const start = processStart(profile)
  if (!start) return 'Baru dibuka'
  const seconds = Math.max(0, Math.floor((nowTick.value - start) / 1000))
  if (seconds < 60) return `${seconds} dtk`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} mnt`
  return `${Math.floor(minutes / 60)} j ${minutes % 60} mnt`
}

function processTimeTitle(profile) {
  if (!isOpen(profile)) return 'Profil sedang tertutup'
  const start = processStart(profile)
  return start ? `Dibuka sejak ${timestampLabel(Math.floor(start / 1000))}` : 'Profil terbuka'
}

function lastOpenedTitle(profile) {
  return profile.last_open_time
    ? `Terakhir dibuka: ${lastOpenedLabel(profile)}`
    : 'Belum pernah dibuka'
}

function badgeTextColor(hex) {
  const raw = String(hex || '').replace('#', '')
  const full = raw.length === 3 ? raw.split('').map((char) => char + char).join('') : raw
  const value = Number.parseInt(full, 16)
  if (full.length !== 6 || Number.isNaN(value)) return '#ffffff'
  const [r, g, b] = [16, 8, 0].map((shift) => {
    const channel = ((value >> shift) & 255) / 255
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  const contrastWhite = 1.05 / (luminance + 0.05)
  const contrastDark = (luminance + 0.05) / 0.0637
  return contrastWhite >= contrastDark ? '#ffffff' : '#1d2129'
}

function sortByProfileId(a, b) {
  return a.profile_id - b.profile_id
}

onMounted(() => {
  refreshAll()
  pollTimer = setInterval(pollOpened, 5000)
  clockTimer = setInterval(() => {
    if (opened.value.size) nowTick.value = Date.now()
  }, 1000)
  cooldownTimer = setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  clearInterval(pollTimer)
  clearInterval(clockTimer)
  clearInterval(cooldownTimer)
  clearTimeout(searchTimer)
})

useOnRefresh(() => refreshAll())
</script>

<template>
  <div class="profiles-page">
    <section class="surface profiles-panel">
      <div class="profiles-toolbar">
        <div class="toolbar-row">
          <div class="toolbar-left">
            <div class="split-button">
              <el-button type="primary" :icon="Plus" @click="openCreate(false)">Buat Profil</el-button>
              <el-dropdown trigger="click" @command="onCreateCommand">
                <el-button type="primary" class="split-caret" :icon="ArrowDown" aria-label="Opsi buat profil" />
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="create">Buat Profil</el-dropdown-item>
                    <el-dropdown-item command="open">Buat dan Buka</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <el-button :icon="Upload" :loading="importBusy" @click="triggerImport">Impor Profil</el-button>
            <el-button :icon="Document" @click="csvVisible = true">Impor CSV</el-button>
            <el-button :icon="Files" @click="ixbVisible = true">Impor ixBrowser</el-button>
            <input
              ref="importInput"
              class="profile-file-input"
              type="file"
              accept=".json,application/json"
              @change="onImportFile"
            />
          </div>
          <div class="toolbar-right">
            <el-input v-model="search" class="search-input" placeholder="Cari nama profil" clearable>
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-select v-model="groupFilter" class="group-select" placeholder="Semua Grup">
              <el-option label="Semua Grup" value="all" />
              <el-option v-for="group in groups" :key="group.id" :label="group.title" :value="group.id" />
            </el-select>
            <el-select v-model="tagFilter" class="tag-select" placeholder="Semua Tag">
              <el-option label="Semua Tag" value="all" />
              <el-option v-for="tag in tags" :key="tag.id" :label="tag.title" :value="tag.id" />
            </el-select>
            <el-tooltip content="Kelola grup" placement="top" :show-after="200">
              <el-button
                class="icon-action"
                :icon="FolderOpened"
                aria-label="Kelola grup"
                @click="groupManagerVisible = true"
              />
            </el-tooltip>
            <el-tooltip content="Muat ulang daftar" placement="top" :show-after="200">
              <el-button class="icon-action" :icon="Refresh" aria-label="Muat ulang daftar" @click="refreshAll" />
            </el-tooltip>
            <el-dropdown trigger="click" :hide-on-click="false">
              <el-button :icon="Menu" aria-label="Pilih kolom">Pilih Kolom</el-button>
              <template #dropdown>
                <div class="column-picker">
                  <el-checkbox
                    v-for="column in COLUMN_OPTIONS"
                    :key="column.key"
                    :model-value="isColumnVisible(column.key)"
                    @change="toggleColumn(column.key)"
                  >{{ column.label }}</el-checkbox>
                  <div class="column-picker-footer">
                    <el-button link type="primary" size="small" @click="showAllColumns">Tampilkan semua</el-button>
                  </div>
                </div>
              </template>
            </el-dropdown>
          </div>
        </div>
        <div class="toolbar-row">
          <div class="toolbar-left">
            <el-dropdown :disabled="!selectedIds.length" trigger="click" @command="runBulk">
              <el-button :disabled="!selectedIds.length" :loading="bulkBusy">
                Operasi Massal
                <el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="open">Buka terpilih</el-dropdown-item>
                  <el-dropdown-item command="close">Tutup terpilih</el-dropdown-item>
                  <el-dropdown-item command="group" divided>Ubah Grup</el-dropdown-item>
                  <el-dropdown-item command="note">Ubah Catatan</el-dropdown-item>
                  <el-dropdown-item command="proxy">Ubah Proxy</el-dropdown-item>
                  <el-dropdown-item command="platform">Ubah Platform</el-dropdown-item>
                  <el-dropdown-item command="delete" divided>Hapus terpilih</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-radio-group v-model="statusFilter" size="small" class="status-segment">
              <el-radio-button value="all">Semua</el-radio-button>
              <el-radio-button value="open">Terbuka</el-radio-button>
              <el-radio-button value="closed">Tertutup</el-radio-button>
            </el-radio-group>
          </div>
          <div class="toolbar-right">
            <el-button
              v-if="opened.size"
              :icon="Grid"
              :loading="arrangeBusy"
              @click="arrangeWindows"
            >Atur Jendela</el-button>
            <span v-if="selectedIds.length" class="selection-count">{{ selectedIds.length }} dipilih</span>
          </div>
        </div>
      </div>

      <el-table
        ref="tableRef"
        v-loading="loading"
        element-loading-text="Memuat profil..."
        class="profiles-table"
        :data="visibleRows"
        row-key="profile_id"
        @selection-change="onSelectionChange"
        @sort-change="onSortChange"
      >
        <el-table-column type="selection" width="38" />
        <el-table-column label="No." width="54" sortable :sort-method="sortByProfileId">
          <template #default="{ row }">
            <span
              class="profile-badge"
              :style="{ background: row.color || '#86909c', color: badgeTextColor(row.color || '#86909c') }"
            >{{ row.profile_id }}</span>
          </template>
        </el-table-column>
        <el-table-column
          v-if="isColumnVisible('name')"
          label="Nama Profil"
          prop="name"
          min-width="150"
          sortable="custom"
        >
          <template #default="{ row }">
            <div class="profile-name-wrap">
              <button type="button" class="profile-name" :title="row.name" @click="openEdit(row)">{{ row.name }}</button>
              <el-tag
                v-if="row.sync_lease && !row.sync_lease.is_local"
                type="warning"
                size="small"
                class="lease-badge"
              >
                {{ `Dibuka di ${row.sync_lease.holder_device_name}` }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column
          v-if="isColumnVisible('group')"
          label="Grup"
          prop="group"
          width="106"
          sortable="custom"
        >
          <template #default="{ row }">
            <span class="cell-main" :title="groupTitle(row.group_id)">{{ groupTitle(row.group_id) }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('tag')" label="Tag" width="120">
          <template #default="{ row }">
            <div class="tag-cell" role="button" tabindex="0" @click="openEdit(row)" @keydown.enter="openEdit(row)">
              <template v-if="profileTags(row).length">
                <span
                  v-for="tag in profileTags(row).slice(0, 2)"
                  :key="tag"
                  class="tag-chip"
                  :title="tag"
                >{{ tag }}</span>
                <span v-if="profileTags(row).length > 2" class="tag-more">+{{ profileTags(row).length - 2 }}</span>
              </template>
              <span v-else class="tag-add">Tambah tag</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('platform')" label="Platform" width="100">
          <template #default="{ row }">
            <span class="cell-main" :title="platformLabel(row)">{{ platformLabel(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('proxy')" label="Proxy" width="118">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="cell-main">{{ proxyTypeLabel(row) }}</span>
              <span class="cell-sub" :title="proxyHostLabel(row)">{{ proxyHostLabel(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('account')" label="Account" width="130">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="cell-main" :title="accountLabel(row)">{{ accountLabel(row) }}</span>
              <span v-if="accountHost(row)" class="cell-sub">{{ accountHost(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('status')" label="Status" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="!isOpen(row) && row.sync_lease && !row.sync_lease.is_local"
              type="warning"
              size="small"
              class="lease-badge"
            >
              {{ `Dibuka di ${row.sync_lease.holder_device_name}` }}
            </el-tag>
            <span v-else :class="['status-text', isOpen(row) ? 'is-open' : 'is-closed']">
              <span :class="['status-dot', isOpen(row) ? 'ok' : 'idle']"></span>
              {{ isOpen(row) ? 'Terbuka' : 'Tertutup' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('created')" label="Dibuat" width="100">
          <template #default="{ row }">
            <span v-if="row.created_at" class="time-stack" :title="createdLabel(row.created_at)">
              <span class="time-line is-created">{{ dateLabel(row.created_at) }}</span>
              <span class="time-line is-created">{{ timeLabel(row.created_at) }}</span>
            </span>
            <span v-else class="time-line is-empty">-</span>
          </template>
        </el-table-column>
        <el-table-column
          v-if="isColumnVisible('last_open')"
          label="Terakhir Dibuka"
          prop="last_open"
          width="146"
          sortable="custom"
        >
          <template #default="{ row }">
            <el-tooltip :content="lastOpenedTitle(row)" placement="top" :show-after="200">
              <span v-if="row.last_open_time" class="time-stack">
                <span class="time-line is-last">{{ dateLabel(row.last_open_time) }}</span>
                <span class="time-line is-last">{{ timeLabel(row.last_open_time) }}</span>
              </span>
              <span v-else class="time-line is-empty">Belum dibuka</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('note')" label="Catatan" width="150">
          <template #default="{ row }">
            <span v-if="row.note" class="cell-sub note-cell" :title="row.note">{{ row.note }}</span>
            <span v-else class="cell-sub is-empty">-</span>
          </template>
        </el-table-column>
        <el-table-column v-if="isColumnVisible('process')" label="Waktu Proses" width="110">
          <template #default="{ row }">
            <el-tooltip :content="processTimeTitle(row)" placement="top" :show-after="200">
              <span :class="['time-line', isOpen(row) ? 'is-last' : 'is-empty']">{{ processTimeLabel(row) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="Operasi" width="196" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button
                v-if="isOpen(row)"
                size="small"
                type="default"
                :loading="!!busy[row.profile_id]"
                class="open-button"
                @click="toggleOpen(row)"
              >
                Tutup
              </el-button>
              <el-button
                v-else-if="getCooldown(row.profile_id) > 0"
                size="small"
                type="info"
                :disabled="true"
                class="open-button"
              >
                {{ `Tunggu ${getCooldown(row.profile_id)}s` }}
              </el-button>
              <el-button
                v-else
                size="small"
                type="primary"
                :loading="!!busy[row.profile_id]"
                class="open-button"
                @click="toggleOpen(row)"
              >
                Buka
              </el-button>
              <el-tooltip content="Edit profil" placement="top" :show-after="200">
                <el-button size="small" class="icon-action" :icon="Edit" aria-label="Edit profil" @click="openEdit(row)" />
              </el-tooltip>
              <el-tooltip content="Duplikat profil" placement="top" :show-after="200">
                <el-button
                  size="small"
                  class="icon-action"
                  :icon="CopyDocument"
                  aria-label="Duplikat profil"
                  @click="duplicateProfile(row)"
                />
              </el-tooltip>
              <el-dropdown
                trigger="click"
                @command="(command) => onRowCommand(command, row)"
                @visible-change="(visible) => onLogMenuVisible(visible, row)"
              >
                <el-button size="small" class="icon-action" :icon="MoreFilled" aria-label="Operasi lain" />
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="cookie" :icon="Files">Cookie</el-dropdown-item>
                    <el-dropdown-item command="bookmarks" :icon="Star">Bookmark</el-dropdown-item>
                    <el-dropdown-item command="passwords" :icon="Lock">Password Tersimpan</el-dropdown-item>
                    <el-dropdown-item command="log-chrome" :icon="Document" divided>Log Chrome</el-dropdown-item>
                    <el-dropdown-item v-if="gostLogs[row.profile_id]" command="log-gost" :icon="Connection">
                      Log Proxy
                    </el-dropdown-item>
                    <el-dropdown-item command="export" :icon="Download" divided>Ekspor</el-dropdown-item>
                    <el-dropdown-item command="export-encrypted" :icon="Lock">Ekspor Terenkripsi</el-dropdown-item>
                    <el-dropdown-item command="clear-cache" :icon="Brush" divided>Bersihkan cache</el-dropdown-item>
                    <el-dropdown-item command="clear-cache-cookies" :icon="Brush">Bersihkan cache + cookie</el-dropdown-item>
                    <el-dropdown-item command="clear-cache-extensions" :icon="Brush">
                      Bersihkan cache (simpan extension)
                    </el-dropdown-item>
                    <el-dropdown-item command="clear-password" :icon="Key" divided>Hapus password tersimpan</el-dropdown-item>
                    <el-dropdown-item command="delete" :icon="Delete" divided>Hapus</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <div class="table-empty">
            <div class="empty-title">{{ hasFilter ? 'Tidak ada profil yang cocok' : 'Belum ada profil' }}</div>
            <p class="empty-text">
              {{
                hasFilter
                  ? 'Ubah kata kunci atau filter grup, tag, dan status untuk melihat profil lain.'
                  : 'Profil menyimpan fingerprint dan proxy untuk membuka Chromium terisolasi.'
              }}
            </p>
            <el-button v-if="!hasFilter" type="primary" :icon="Plus" @click="openCreate(false)">Buat Profil</el-button>
          </div>
        </template>
      </el-table>

      <div class="table-footer">
        <el-pagination
          class="pagination"
          background
          :current-page="page"
          :page-size="limit"
          :page-sizes="[10, 20, 50]"
          :total="displayTotal"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </section>

    <ProfileDialog
      v-model="dialogVisible"
      :profile="editingProfile"
      :groups="groups"
      :tags="tags"
      @saved="onSaved"
      @tags-changed="loadTags"
    />

    <ImportCsvDialog v-model="csvVisible" @imported="onCsvImported" />

    <ImportIxbrowserDialog v-model="ixbVisible" @imported="onIxbrowserImported" />

    <GroupManager v-model="groupManagerVisible" @changed="onGroupsChanged" />

    <CookieDialog
      v-model="cookieVisible"
      :profile="cookieProfile"
      :profile-open="cookieProfile ? isOpen(cookieProfile) : false"
    />

    <BookmarkDialog v-model="bookmarkVisible" :profile="bookmarkProfile" />

    <PasswordDialog v-model="passwordVisible" :profile="passwordProfile" />

    <ProfileLogDialog v-model="logVisible" :profile="logProfile" :kind="logKind" />

    <el-dialog
      v-model="bulkVisible"
      :title="bulkTitle"
      width="460px"
      top="12vh"
      :close-on-click-modal="false"
      class="bulk-edit-dialog"
    >
      <el-form label-width="120px" label-position="right" class="dialog-form bulk-form" @submit.prevent>
        <template v-if="bulkKind === 'group'">
          <el-form-item label="Grup Tujuan" required>
            <el-select v-model="bulkForm.group_id" placeholder="Pilih grup">
              <el-option v-for="group in groups" :key="group.id" :label="group.title" :value="group.id" />
            </el-select>
          </el-form-item>
          <p class="form-hint">{{ selectedIds.length }} profil terpilih akan dipindahkan ke grup ini.</p>
        </template>

        <template v-else-if="bulkKind === 'proxy'">
          <el-form-item label="Tipe Proxy">
            <el-radio-group v-model="bulkForm.proxy_type">
              <el-radio value="direct">Direct</el-radio>
              <el-radio value="http">HTTP</el-radio>
              <el-radio value="https">HTTPS</el-radio>
              <el-radio value="socks5">SOCKS5</el-radio>
            </el-radio-group>
          </el-form-item>
          <template v-if="bulkForm.proxy_type !== 'direct'">
            <el-form-item label="Host" required :error="bulkErrors.proxy_ip">
              <el-input v-model="bulkForm.proxy_ip" placeholder="203.0.113.10" @input="bulkErrors.proxy_ip = ''" />
            </el-form-item>
            <el-form-item label="Port" required :error="bulkErrors.proxy_port">
              <el-input
                v-model="bulkForm.proxy_port"
                type="number"
                min="1"
                max="65535"
                class="field-180"
                placeholder="8080"
                @input="bulkErrors.proxy_port = ''"
              />
            </el-form-item>
            <el-form-item label="Username">
              <el-input v-model="bulkForm.proxy_user" placeholder="opsional" />
            </el-form-item>
            <el-form-item label="Password">
              <el-input v-model="bulkForm.proxy_password" type="password" show-password placeholder="opsional" />
            </el-form-item>
          </template>
          <p class="form-hint">Konfigurasi ini menggantikan proxy {{ selectedIds.length }} profil terpilih.</p>
        </template>

        <template v-else-if="bulkKind === 'platform'">
          <el-form-item label="Platform">
            <el-radio-group v-model="bulkForm.platform">
              <el-radio value="Windows">Windows</el-radio>
              <el-radio value="Macos">macOS</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="bulkForm.platform === 'Macos' ? 'Versi macOS' : 'Versi Windows'">
            <el-select v-model="bulkForm.system_version" class="field-180">
              <el-option v-for="version in bulkVersionOptions" :key="version" :label="version" :value="version" />
            </el-select>
          </el-form-item>
          <p class="form-hint">Platform dan versi sistem diterapkan ke {{ selectedIds.length }} profil terpilih.</p>
        </template>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <el-button @click="bulkVisible = false">Batal</el-button>
          <div class="footer-spacer"></div>
          <el-button type="primary" :loading="bulkBusy" @click="submitBulkEdit">Simpan</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>
