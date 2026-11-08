import * as Dialog from '@radix-ui/react-dialog'
import { Loader2, Rocket, X } from 'lucide-react'
import { useState } from 'react'
import { usePromoteRun } from '@/api/studio/hooks'
import type { ModelStage, MonitorSchedule, StudioRunView } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { Input, Select } from '@/components/ui/inputs'
import { toast } from '@/components/ui/toast'
import { useStudio } from '../store'
import { MONITOR_OPTIONS, STAGE_OPTIONS, numOrNull } from './options'

/** "Promote" a completed run into the Model Registry (next version of a named model). */
export function PromoteButton({ run }: { run: StudioRunView }) {
  const s = run.status
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [owner, setOwner] = useState('')
  const [description, setDescription] = useState('')
  const [stage, setStage] = useState<ModelStage>('candidate')
  const [monitor, setMonitor] = useState<MonitorSchedule>('none')
  const [psi, setPsi] = useState('0.25')
  const [floor, setFloor] = useState('')
  const promote = usePromoteRun()
  const setModelId = useStudio((x) => x.setModelId)
  const psiValue = numOrNull(psi)
  const psiBad = psiValue === null || psiValue <= 0

  const reset = () => {
    setName(s.name ?? run.result?.title ?? s.method)
    setOwner('')
    setDescription('')
    setStage('candidate')
    setMonitor('none')
    setPsi('0.25')
    setFloor('')
    promote.reset()
  }

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        if (o) reset()
        setOpen(o)
      }}
    >
      <Dialog.Trigger asChild>
        <Button size="sm" variant="outline" title="Register this run in the Model Registry (versioned, with owner, stage and drift monitoring)">
          <Rocket /> Promote
        </Button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="fixed top-1/2 left-1/2 z-50 w-[min(480px,95vw)] -translate-x-1/2 -translate-y-1/2 rounded-md border border-border-strong bg-surface p-4 focus-visible:outline-none">
          <div className="mb-3 flex items-center gap-2">
            <Dialog.Title className="flex-1 text-md font-semibold text-fg">Promote to the Model Registry</Dialog.Title>
            <Dialog.Close asChild>
              <Button size="icon-sm" variant="ghost" aria-label="Close">
                <X />
              </Button>
            </Dialog.Close>
          </div>
          <Dialog.Description className="mb-3 text-xs text-muted">
            Run <span className="num">{s.runId}</span> ({s.method}) becomes the next version of the named model, pinned to its spec hash and data snapshot, with the out-of-fold baseline used for drift
            monitoring.
          </Dialog.Description>
          <form
            className="space-y-2.5"
            onSubmit={(e) => {
              e.preventDefault()
              if (psiBad) return
              promote.mutate(
                {
                  runId: s.runId,
                  body: {
                    name: name.trim() || null,
                    owner: owner.trim() || null,
                    description: description.trim() || null,
                    stage,
                    monitor,
                    psi_threshold: psiValue ?? 0.25,
                    metric_floor: numOrNull(floor),
                  },
                },
                {
                  onSuccess: (m) => {
                    setOpen(false)
                    setModelId(m.modelId)
                    toast({ tone: 'success', title: `Registered ${m.name} v${m.version}`, message: `${m.stage}${m.monitor !== 'none' ? ` · monitored ${m.monitor}` : ''} — open the Registry tab` })
                  },
                },
              )
            }}
          >
            <label className="block text-xs text-fg">
              Model name <span className="text-muted">(versions share a name)</span>
              <Input className="mt-0.5 w-full" value={name} onChange={(e) => setName(e.target.value)} required />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block text-xs text-fg">
                Owner
                <Input className="mt-0.5 w-full" placeholder="default: OS user" value={owner} onChange={(e) => setOwner(e.target.value)} />
              </label>
              <div className="text-xs text-fg">
                Stage
                <Select label="Stage" className="mt-0.5 w-full" value={stage} onChange={setStage} options={STAGE_OPTIONS} />
              </div>
            </div>
            <label className="block text-xs text-fg">
              Description
              <textarea
                className="mt-0.5 block min-h-14 w-full rounded-md border border-border bg-surface-2 px-2 py-1 text-sm text-fg placeholder:text-faint hover:border-border-strong focus-visible:outline-2 focus-visible:outline-focus"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <div className="grid grid-cols-3 gap-2">
              <div className="text-xs text-fg">
                Monitoring
                <Select label="Monitoring schedule" className="mt-0.5 w-full" value={monitor} onChange={setMonitor} options={MONITOR_OPTIONS} />
              </div>
              <label className="block text-xs text-fg" title="Population stability index above which a feature / the predictions count as drifted (0.1 watch, 0.25 drift)">
                PSI threshold
                <Input type="number" step="0.01" min="0" className="num mt-0.5 w-full" aria-invalid={psiBad} value={psi} onChange={(e) => setPsi(e.target.value)} />
              </label>
              <label className="block text-xs text-fg" title="Alert when the expanding out-of-sample rank IC (panels) / R² OOS falls below">
                Metric floor
                <Input type="number" step="0.01" className="num mt-0.5 w-full" placeholder="none" value={floor} onChange={(e) => setFloor(e.target.value)} />
              </label>
            </div>
            {stage === 'production' && <p className="text-2xs text-warn">Production demotes the current production version of this name to candidate.</p>}
            {promote.isError && (
              <p role="alert" className="text-xs text-down">
                {promote.error instanceof Error ? promote.error.message : String(promote.error)}
              </p>
            )}
            <div className="flex justify-end gap-1.5 pt-1">
              <Button variant="ghost" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" disabled={promote.isPending || psiBad || !name.trim()}>
                {promote.isPending ? <Loader2 className="animate-spin" /> : <Rocket />} Promote
              </Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
