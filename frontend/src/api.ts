export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    const detail = error?.detail
    throw new Error(typeof detail === 'string' ? detail : `요청을 처리하지 못했습니다. (HTTP ${response.status})`)
  }
  return response.json() as Promise<T>
}

export function requestId(): string {
  return crypto.randomUUID()
}

export function remember(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(`loreforge.${key}`)
    else localStorage.setItem(`loreforge.${key}`, value)
  } catch { /* 서버 저장 결과는 브라우저 저장 공간과 무관하게 유지된다. */ }
}

export function recall(key: string): string | null {
  try { return localStorage.getItem(`loreforge.${key}`) } catch { return null }
}
