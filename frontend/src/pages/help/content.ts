/**
 * Guide content for the Help page, bundled from docs/guide at build time: the Markdown chapters are lazy raw-string
 * chunks (loaded when /help opens) and the screenshots are emitted as hashed assets (only their URLs are in the
 * bundle). Works the same in dev (Vite serves ../docs/guide, see server.fs.allow) and in app mode (frontend/dist_app
 * served by FastAPI), with no backend route.
 */
import { parseGuideMarkdown, type Block } from '@/lib/guide/markdown'
import { buildSections, chapterTitle, sortChapters, type Section } from '@/lib/guide/toc'

const RAW = import.meta.glob('../../../../docs/guide/*.md', { query: '?raw', import: 'default' }) as Record<string, () => Promise<string>>
const IMAGES = import.meta.glob('../../../../docs/guide/img/*.{webp,png,jpg,jpeg,gif,svg}', {
  query: '?url',
  import: 'default',
  eager: true,
}) as Record<string, string>

const fileSlug = (p: string) => p.replace(/^.*\//, '').replace(/\.md$/i, '')

export interface Chapter {
  slug: string
  title: string
  blocks: Block[]
  sections: Section[]
}

export interface Guide {
  chapters: Chapter[]
  bySlug: Map<string, Chapter>
  sections: Section[]
}

/** Image URL for a guide-relative path such as `img/02-cockpit.webp` (null when the file is not bundled). */
export function imageUrl(src: string): string | null {
  if (/^(https?:|data:)/i.test(src)) return null
  const name = src.replace(/^\.?\//, '').replace(/^img\//, '')
  return IMAGES[`../../../../docs/guide/img/${name}`] ?? null
}

export const CHAPTER_SLUGS: string[] = sortChapters(Object.keys(RAW).map((p) => ({ slug: fileSlug(p) }))).map((c) => c.slug)

let cache: Promise<Guide> | null = null

/** Load and parse every chapter once (the search needs all of them). */
export function loadGuide(): Promise<Guide> {
  if (!cache) {
    cache = Promise.all(
      Object.entries(RAW).map(async ([p, load]) => {
        const slug = fileSlug(p)
        const blocks = parseGuideMarkdown(await load())
        const title = chapterTitle(blocks, slug)
        return { slug, title, blocks, sections: buildSections(slug, title, blocks) }
      }),
    ).then((list) => {
      const chapters = sortChapters(list)
      return { chapters, bySlug: new Map(chapters.map((c) => [c.slug, c])), sections: chapters.flatMap((c) => c.sections) }
    })
    cache.catch(() => {
      cache = null
    })
  }
  return cache
}
