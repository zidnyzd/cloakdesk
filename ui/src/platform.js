export const CUSTOM_SITE_ID = 22

export const ACCOUNT_PRESETS = [
  { key: 'Facebook', site_id: 1, site_url: 'https://www.facebook.com/' },
  { key: 'TikTok', site_id: 2, site_url: 'https://www.tiktok.com/' },
  { key: 'Instagram', site_id: 3, site_url: 'https://www.instagram.com/' },
  { key: 'Threads', site_id: 8, site_url: 'https://www.threads.net/' },
  { key: 'Shopee', site_id: 4, site_url: 'https://shopee.co.id/' },
  { key: 'Tokopedia', site_id: 5, site_url: 'https://www.tokopedia.com/' },
  { key: 'Google', site_id: 6, site_url: 'https://www.google.com/' },
  { key: 'Lazada', site_id: 7, site_url: 'https://www.lazada.co.id/' },
]

export function hostOf(value) {
  try {
    const parsed = new URL(String(value || ''))
    return parsed.hostname.replace(/^www\./, '').toLowerCase()
  } catch {
    return ''
  }
}

export function accountKey(siteId, siteUrl) {
  const id = Number(siteId)
  const host = hostOf(siteUrl)
  const byId = ACCOUNT_PRESETS.find((preset) => preset.site_id === id)
  if (byId && (!host || host === hostOf(byId.site_url))) return byId.key
  if (host) {
    if (host === 'threads.net' || host === 'threads.com') return 'Threads'
    const byUrl = ACCOUNT_PRESETS.find((preset) => hostOf(preset.site_url) === host)
    if (byUrl) return byUrl.key
  }
  return 'Custom'
}

export function accountSummary(profile) {
  const siteId = profile?.site_id ?? CUSTOM_SITE_ID
  const siteUrl = profile?.site_url || ''
  const key = accountKey(siteId, siteUrl)
  const host = hostOf(siteUrl)
  if (key === 'Custom') return { key, host, label: host || 'Tanpa platform', detail: '' }
  return { key, host, label: key, detail: host }
}
