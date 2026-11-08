/**
 * Optional alert sound (News Terminal, roadmap v3 N4.3): a short two-tone WebAudio chime, no audio file. Off by
 * default; the preference lives in localStorage (per browser).
 */
const KEY = 'big360.news.sound'

export function soundEnabled(): boolean {
  try {
    return globalThis.localStorage?.getItem(KEY) === '1'
  } catch {
    return false
  }
}

export function setSoundEnabled(on: boolean): void {
  try {
    globalThis.localStorage?.setItem(KEY, on ? '1' : '0')
  } catch {
    /* private mode: preference not kept */
  }
}

let ctx: AudioContext | null = null

export function playChime(): void {
  if (!soundEnabled()) return
  try {
    const AC = (globalThis as unknown as { AudioContext?: typeof AudioContext }).AudioContext
    if (!AC) return
    ctx = ctx ?? new AC()
    const t = ctx.currentTime
    for (const [f, dt] of [
      [880, 0],
      [1320, 0.12],
    ] as const) {
      const o = ctx.createOscillator()
      const g = ctx.createGain()
      o.type = 'sine'
      o.frequency.value = f
      g.gain.setValueAtTime(0.0001, t + dt)
      g.gain.exponentialRampToValueAtTime(0.12, t + dt + 0.02)
      g.gain.exponentialRampToValueAtTime(0.0001, t + dt + 0.18)
      o.connect(g).connect(ctx.destination)
      o.start(t + dt)
      o.stop(t + dt + 0.2)
    }
  } catch {
    /* autoplay blocked until a user gesture: silent */
  }
}
