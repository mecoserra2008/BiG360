/** Save a text file in the browser. */
export function downloadText(name: string, text: string, type = 'text/markdown;charset=utf-8'): void {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const slug = (s: string): string => s.replace(/[^A-Za-z0-9_-]+/g, '_').slice(0, 60) || 'thesis'
