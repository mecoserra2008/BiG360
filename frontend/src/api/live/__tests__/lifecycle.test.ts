import { afterEach, describe, expect, it, vi } from 'vitest'
import { bindPageLifecycle } from '../store'

function setup(hiddenMs = 1000) {
  const win = new EventTarget()
  const doc = Object.assign(new EventTarget(), { hidden: false })
  const h = { suspend: vi.fn(), resume: vi.fn() }
  const unbind = bindPageLifecycle(win, doc, h, hiddenMs)
  return { win, doc, h, unbind }
}

describe('live stream page lifecycle (perf pass: free the HTTP/1.1 connection)', () => {
  afterEach(() => vi.useRealTimers())

  it('suspends at once on pagehide and resumes only on a bfcache restore', () => {
    const { win, h } = setup()
    win.dispatchEvent(new Event('pagehide'))
    expect(h.suspend).toHaveBeenCalledTimes(1)
    win.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: false }))
    expect(h.resume).not.toHaveBeenCalled()
    win.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }))
    expect(h.resume).toHaveBeenCalledTimes(1)
  })

  it('suspends a tab hidden longer than the grace period, resumes when visible', () => {
    vi.useFakeTimers()
    const { doc, h } = setup(1000)
    doc.hidden = true
    doc.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(999)
    expect(h.suspend).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(h.suspend).toHaveBeenCalledTimes(1)
    doc.hidden = false
    doc.dispatchEvent(new Event('visibilitychange'))
    expect(h.resume).toHaveBeenCalledTimes(1)
  })

  it('a short hide does not drop the stream', () => {
    vi.useFakeTimers()
    const { doc, h } = setup(1000)
    doc.hidden = true
    doc.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(500)
    doc.hidden = false
    doc.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(5000)
    expect(h.suspend).not.toHaveBeenCalled()
  })

  it('unbind removes the listeners', () => {
    const { win, h, unbind } = setup()
    unbind()
    win.dispatchEvent(new Event('pagehide'))
    expect(h.suspend).not.toHaveBeenCalled()
  })
})
